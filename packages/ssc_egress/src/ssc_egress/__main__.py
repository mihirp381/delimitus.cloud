"""The egress proxy container's entry point: ``python -m ssc_egress`` (SSC-053).

Writes a listener that refuses every request, starts Envoy on it at once, so the machine's
health check passes as soon as the proxy listens, then follows the org's snapshot
(``ssc_egress.runner``). When Envoy exits, the container exits non-zero and its unit restarts
it; on ``SIGTERM`` Envoy is stopped and the container exits 0. The loop closes
without waiting for its worker threads and the process ends with ``os._exit``, so a snapshot
read still retrying against an unreachable bucket never holds the container past its stop
timeout.

A stop that arrives while the imports below still load (about a second and a half in the image)
is recorded by ``_early_stop``, installed before them: the container's first process ignores a
signal it has no handler for, so without it that stop waited out the stop timeout and ended in
``SIGKILL``. ``main`` then exits 0 without starting Envoy, or ``_serve`` stops at once.

Settings: ``SSC_ORG_ID`` and ``SSC_CELL_BUCKET`` (required), ``SSC_PROXY_PORT`` (default
``PROXY_PORT``), ``SSC_SNAPSHOT_MAX_AGE`` (seconds without a confirmed snapshot before the proxy
refuses every tunnel, default ``MAX_AGE_SECONDS``, 300, as the gateway's), ``SSC_ENVOY_BIN``
(default ``envoy``) and ``STORAGE_EMULATOR_HOST`` (tests).
"""

import signal
from types import FrameType

_stopped_early: list[int] = []


def _early_stop(signum: int, _frame: FrameType | None) -> None:
    _stopped_early.append(signum)


signal.signal(signal.SIGTERM, _early_stop)
signal.signal(signal.SIGINT, _early_stop)

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from collections.abc import Mapping  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Final, NoReturn  # noqa: E402

from ssc_contracts.egress import PROXY_PORT  # noqa: E402
from ssc_egress.envoy import EgressConfig, Policy, bootstrap, envoy_args  # noqa: E402
from ssc_egress.runner import MAX_AGE_SECONDS, ListenerWriter, follow  # noqa: E402
from ssc_shared import redaction  # noqa: E402
from ssc_shared.access import ViewHolder  # noqa: E402
from ssc_shared.blobstore_gcs import GcsBlobStore, bucket_of  # noqa: E402
from ssc_shared.snapshot_feed import SnapshotFeed  # noqa: E402

log = logging.getLogger("ssc_egress")

STOP_SECONDS: Final = 10.0


class SettingsError(ValueError):
    pass


def _need(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise SettingsError(f"{key} is required")
    return value


def _max_age(env: Mapping[str, str]) -> float:
    raw = env.get("SSC_SNAPSHOT_MAX_AGE", "").strip()
    if not raw:
        return MAX_AGE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        raise SettingsError("SSC_SNAPSHOT_MAX_AGE is a number of seconds above 0")
    return value


def _start(env: Mapping[str, str]) -> tuple[EgressConfig, ListenerWriter, subprocess.Popen[bytes]]:
    work = Path(tempfile.mkdtemp(prefix="ssc-egress-"))
    cfg = EgressConfig(port=int(env.get("SSC_PROXY_PORT", str(PROXY_PORT))), lds_dir=str(work))
    writer = ListenerWriter(cfg)
    writer.write(Policy())
    config = work / "envoy.json"
    config.write_text(json.dumps(bootstrap(cfg)), encoding="utf-8")
    envoy = subprocess.Popen(  # noqa: S603  (our own binary and arguments)
        [env.get("SSC_ENVOY_BIN", "envoy"), *envoy_args(str(config))]
    )
    return cfg, writer, envoy


async def _serve(
    org_id: str,
    bucket: str,
    writer: ListenerWriter,
    envoy: subprocess.Popen[bytes],
    max_age: float,
) -> bool:
    """Follows the snapshot until a signal (True) or Envoy's exit (False)."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    if _stopped_early:  # a stop while Envoy was starting, before the handlers above
        stop.set()
    holder = ViewHolder(org_id)
    feed = SnapshotFeed(GcsBlobStore(bucket_of(bucket)), holder)
    await follow(feed, holder, writer, stop=stop, envoy=envoy, max_age=max_age)
    return stop.is_set()


def _stop(envoy: subprocess.Popen[bytes]) -> None:
    if envoy.poll() is None:
        envoy.terminate()
        try:
            envoy.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            envoy.kill()


def _exit(code: int) -> NoReturn:
    logging.shutdown()
    os._exit(code)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    redaction.install()
    try:
        org_id = _need(os.environ, "SSC_ORG_ID")
        bucket = _need(os.environ, "SSC_CELL_BUCKET")
        max_age = _max_age(os.environ)
    except SettingsError as exc:
        log.error("%s", exc)
        return 2
    if _stopped_early:
        log.info("stopped before the proxy started")
        return 0
    cfg, writer, envoy = _start(os.environ)
    log.info("egress proxy serving on port %s", cfg.port)
    loop = asyncio.new_event_loop()
    try:
        stopped = loop.run_until_complete(_serve(org_id, bucket, writer, envoy, max_age))
    finally:
        _stop(envoy)
        loop.close()
    if stopped:
        return 0
    log.error("envoy exited with %s", envoy.returncode)
    return 1


if __name__ == "__main__":
    _exit(main())
