"""The gateway container's entry point: ``python -m ssc_edge.gateway`` (SSC-018, decision 023).

Starts the authoriser (``ssc_edge.server``, with the stream relay) on loopback and waits until
it answers, which is after the keyring is loaded, then starts Envoy on ``PORT``. Cloud Run's
startup probe opens ``PORT``, so an instance takes traffic only once both run. When either
process exits, the other is stopped and the container exits non-zero, so Cloud Run replaces the
instance rather than running Envoy without its authoriser.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import FrameType
from typing import Final

import httpx2

from ssc_edge.envoy import EnvoyConfig, render

log = logging.getLogger(__name__)

READY_SECONDS: Final = 60.0
STOP_SECONDS: Final = 10.0


def _wait_ready(authz: subprocess.Popen[bytes], port: int, deadline: float) -> bool:
    url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() < deadline:
        if authz.poll() is not None:
            return False
        try:
            if httpx2.get(url, timeout=1).status_code == 200:  # noqa: PLR2004
                return True
        except httpx2.HTTPError:
            pass
        time.sleep(0.1)
    return False


def _stop(*procs: subprocess.Popen[bytes]) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    deadline = time.monotonic() + STOP_SECONDS
    for p in procs:
        try:
            p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", "8080"))
    authz_port = int(os.environ.get("SSC_AUTHZ_PORT", "9001"))
    stream_port = int(os.environ.get("SSC_STREAM_PORT", "9002"))
    envoy_bin = os.environ.get("SSC_ENVOY_BIN", "envoy")
    config = Path(tempfile.mkdtemp(prefix="ssc-envoy-")) / "envoy.json"
    cfg = EnvoyConfig(port=port, authz_port=authz_port, stream_port=stream_port)
    config.write_text(json.dumps(render(cfg)))

    authz = subprocess.Popen([sys.executable, "-m", "ssc_edge.server"])  # noqa: S603
    stopping = False

    def on_signal(signum: int, _: FrameType | None) -> None:
        nonlocal stopping
        stopping = True
        log.info("gateway stopping on signal %s", signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    if not _wait_ready(authz, authz_port, time.monotonic() + READY_SECONDS):
        log.error("authoriser did not start")
        _stop(authz)
        return 1
    envoy = subprocess.Popen(  # noqa: S603
        [envoy_bin, "-c", str(config), "--log-level", "warn", "--disable-hot-restart"]
    )
    log.info("gateway serving on port %s", port)
    while not stopping and authz.poll() is None and envoy.poll() is None:
        time.sleep(0.2)
    if not stopping:
        log.error(
            "a gateway process exited: authz %s, envoy %s", authz.returncode, envoy.returncode
        )
    _stop(envoy, authz)
    return 0 if stopping else 1


if __name__ == "__main__":
    sys.exit(main())
