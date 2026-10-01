"""Runtime probes: what an app container on the platform can and cannot do (SSC-017).

``run(base_url)`` asks the probe app (``conformance/runtime/probe_app``) running behind
``base_url`` and returns one ``ProbeResult`` per entry of ``PROBES``. Four probes run anywhere a
container runs. The other ten need the real network path of a cell: here they report
``skipped``, never ``passed``. In a staging cell the probe image's runner job runs all fourteen
(``conformance/runtime/probe_app/runner.py``, started by ``python -m ssc_conformance.nightly``).

A local run starts the image with ``--read-only --tmpfs /tmp``, ``HOME=/tmp`` and a ``PORT`` other
than 8080, and publishes only that port, so ``listens_on_PORT`` fails for an app that ignores it.
"""

import json
from dataclasses import dataclass
from typing import Final, Literal, cast

import httpx2

Status = Literal["passed", "failed", "skipped"]

EXPECTED_UID: Final = 10001
STAGING_CELL: Final = "runs in a staging cell only (ssc_conformance.nightly)"
LOCAL_PROBES: Final = (
    "non_root_10001",
    "listens_on_PORT",
    "health_path",
    "no_write_outside_memory",
)
CELL_PROBES: Final = (
    "no_direct_egress",
    "no_dns_exfil",
    "metadata_token_no_roles",
    "metadata_identity_is_own",
    "cannot_reach_peer_app",
    "header_echo_no_google_jwt",
    "authorization_passthrough",
    "cannot_read_secrets",
    "no_platform_credentials_in_env",
    "sse_passthrough",
)
PROBES: Final = LOCAL_PROBES + CELL_PROBES


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    status: Status
    reason: str = ""


class _ProbeFailedError(Exception):
    pass


def _get(client: httpx2.Client, path: str) -> bytes:
    try:
        response = client.get(path)
    except httpx2.HTTPError as exc:
        raise _ProbeFailedError(f"GET {path}: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise _ProbeFailedError(f"GET {path}: HTTP {response.status_code}")
    return response.content


def _get_object(client: httpx2.Client, path: str) -> dict[str, object]:
    try:
        body: object = json.loads(_get(client, path))
    except ValueError:
        raise _ProbeFailedError(f"GET {path}: not JSON") from None
    if not isinstance(body, dict):
        raise _ProbeFailedError(f"GET {path}: not a JSON object")
    return cast("dict[str, object]", body)


def _non_root(client: httpx2.Client, health_path: str) -> str:
    uid = _get_object(client, "/probe/uid").get("uid")
    if uid != EXPECTED_UID:
        raise _ProbeFailedError(f"runs as uid {uid}, not {EXPECTED_UID}")
    return f"uid {uid}"


def _listens_on_port(client: httpx2.Client, health_path: str) -> str:
    _get(client, "/")
    return "answers on the published PORT"


def _health_path(client: httpx2.Client, health_path: str) -> str:
    _get(client, health_path)
    return f"{health_path} answers 200"


def _no_write_outside_memory(client: httpx2.Client, health_path: str) -> str:
    body = _get_object(client, "/probe/write")
    home, writable = body.get("home"), body.get("writable")
    if not isinstance(writable, dict) or not isinstance(home, str):
        raise _ProbeFailedError("unexpected /probe/write body")
    results = cast("dict[str, object]", writable)
    persistent = [d for d in ("/", "/app") if results.get(d) is not False]
    if persistent:
        raise _ProbeFailedError(f"writable outside memory: {', '.join(persistent)}")
    if results.get(home) is not True:
        raise _ProbeFailedError(f"HOME ({home}) is not writable")
    return f"/ and /app refuse writes; HOME={home} accepts them"


_LOCAL = {
    "non_root_10001": _non_root,
    "listens_on_PORT": _listens_on_port,
    "health_path": _health_path,
    "no_write_outside_memory": _no_write_outside_memory,
}


def run(
    base_url: str, *, health_path: str = "/health", client: httpx2.Client | None = None
) -> list[ProbeResult]:
    """Every probe in ``PROBES`` order."""
    http = client or httpx2.Client(base_url=base_url, timeout=5.0)
    try:
        results: list[ProbeResult] = []
        for name in PROBES:
            check = _LOCAL.get(name)
            if check is None:
                results.append(ProbeResult(name, "skipped", STAGING_CELL))
                continue
            try:
                results.append(ProbeResult(name, "passed", check(http, health_path)))
            except _ProbeFailedError as exc:
                results.append(ProbeResult(name, "failed", str(exc)))
        return results
    finally:
        if client is None:
            http.close()
