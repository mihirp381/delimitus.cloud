"""The in-cell probe run: a Cloud Run job that stands where the gateway stands (SSC-017).

It runs as the gateway's service account, in the gateway subnet with the gateway tag, and calls
the probe app the way the gateway calls an app: ``X-Serverless-Authorization`` carries the
gateway's ID token, and ``Authorization`` carries the app's own credential.

Configuration: ``PROBE_URL`` (the probe app), ``PROBE_PEER_URL`` (a second app the first must
not reach), ``PROBE_HEALTH_PATH`` (default ``/health``). For ``cannot_reach_peer_cell`` and
``deny_peer_cell``, all of ``PROBE_PEER_CELL_APP_URL``, ``PROBE_PEER_CELL_GATEWAY_URL`` (another
cell's app and gateway ``run.app`` URLs), ``PROBE_PEER_CELL_RANGE`` (that cell's address range) and
``PROBE_PEER_CELL_PROJECT`` (its project); without them both probes are skipped (``no peer
cell``), never passed. ``PROBE_DATAGW_URL`` (this cell's data gateway) and
``PROBE_DATAGW_CONNECTION`` (a ``con_<20>`` connection id) are for ``datagw_read_only``; without
either the probe is skipped (``waits for the data gateway``), and a connection that is not a
``con_<20>`` id fails it.
``PROBE_EGRESS_HOSTS`` (comma-separated) names hosts that resolve in the cell but must not answer
an app; ``no_direct_egress`` dials each too. Prints one JSON line per probe and a summary line,
and exits 1 when any probe fails.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence

import checks

METADATA = "http://metadata.google.internal/computeMetadata/v1"
TIMEOUT = 60.0
PEER_CELL_TIMEOUT = 240.0
DATAGW_TIMEOUT = 240.0
NO_PEER_CELL = "no peer cell"
WAITS_FOR_DATAGW = "waits for the data gateway"
PEER_CELL_ENV = (
    "PROBE_PEER_CELL_APP_URL",
    "PROBE_PEER_CELL_GATEWAY_URL",
    "PROBE_PEER_CELL_RANGE",
    "PROBE_PEER_CELL_PROJECT",
)
EGRESS_HOSTS_ENV = "PROBE_EGRESS_HOSTS"
DATAGW_URL_ENV = "PROBE_DATAGW_URL"
DATAGW_CONNECTION_ENV = "PROBE_DATAGW_CONNECTION"
CONNECTION_ID = re.compile(r"con_[a-z0-9]{20}")
type PeerCell = tuple[str, str, str, str]


class ProbeSkippedError(Exception):
    pass


class Probe:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.headers = {
            "X-Serverless-Authorization": f"Bearer {token}",
            "Authorization": checks.APP_CREDENTIAL,
        }

    def open(self, path: str, timeout: float = TIMEOUT):  # noqa: ANN201
        request = urllib.request.Request(self.base + path, headers=self.headers)  # noqa: S310
        try:
            return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
        except urllib.error.HTTPError as exc:
            raise checks.ProbeFailedError(f"GET {path}: HTTP {exc.code}") from None
        except OSError as exc:
            raise checks.ProbeFailedError(f"GET {path}: {type(exc).__name__}") from None

    def get(self, path: str, timeout: float = TIMEOUT) -> object:
        with self.open(path, timeout) as response:
            body = response.read()
        try:
            return json.loads(body)
        except ValueError:
            raise checks.ProbeFailedError(f"GET {path}: not JSON") from None

    def body(self, path: str, timeout: float = TIMEOUT) -> checks.Body:
        return checks._map(self.get(path, timeout), path)  # noqa: SLF001

    def sse(self, path: str) -> list[float]:
        started = time.monotonic()
        arrivals: list[float] = []
        with self.open(path) as response:
            for line in response:
                if line.startswith(b"data:"):
                    arrivals.append(time.monotonic() - started)
        return arrivals


def id_token(audience: str) -> str:
    query = urllib.parse.urlencode({"audience": audience, "format": "full"})
    request = urllib.request.Request(  # noqa: S310
        f"{METADATA}/instance/service-accounts/default/identity?{query}",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return response.read().decode()


def peer_cell_from(environ: Mapping[str, str]) -> PeerCell | None:
    """The peer cell's app URL, gateway URL, range and project, or None unless all are set."""
    app_url, gateway_url, cidr, project = (environ.get(name, "") for name in PEER_CELL_ENV)
    if app_url and gateway_url and cidr and project:
        return (app_url, gateway_url, cidr, project)
    return None


def egress_hosts_from(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The hosts ``no_direct_egress`` also dials, from ``PROBE_EGRESS_HOSTS``."""
    return tuple(h for h in (p.strip() for p in environ.get(EGRESS_HOSTS_ENV, "").split(",")) if h)


def _peer_cell(app: Probe, peer_cell: PeerCell | None) -> str:
    if peer_cell is None:
        raise ProbeSkippedError(NO_PEER_CELL)
    query = urllib.parse.urlencode(
        dict(zip(("app", "gateway", "range"), peer_cell[:3], strict=True))
    )
    return checks.cannot_reach_peer_cell(app.body(f"/probe/peer-cell?{query}", PEER_CELL_TIMEOUT))


def _deny_peer_cell(app: Probe, peer_cell: PeerCell | None) -> str:
    """The peer cell's secret and bucket, by the names every cell has (``ssc-a-probe``, and the
    cell bucket ``<project>-cell``)."""
    if peer_cell is None:
        raise ProbeSkippedError(NO_PEER_CELL)
    project = peer_cell[3]
    query = urllib.parse.urlencode(
        {"project": project, "secret": checks.DENY_SECRET, "bucket": f"{project}-cell"}
    )
    return checks.deny_peer_cell(app.body(f"/probe/deny-peer?{query}"))


def _datagw_read_only(app: Probe, datagw_url: str, datagw_connection: str) -> str:
    if not datagw_url or not datagw_connection:
        raise ProbeSkippedError(WAITS_FOR_DATAGW)
    if CONNECTION_ID.fullmatch(datagw_connection) is None:
        raise checks.ProbeFailedError(
            f"{DATAGW_CONNECTION_ENV} is not a con_<20> connection id: {datagw_connection!r}"
        )
    query = urllib.parse.urlencode({"url": datagw_url, "connection": datagw_connection})
    return checks.datagw_read_only(app.body(f"/probe/datagw?{query}", DATAGW_TIMEOUT))


def plan(  # noqa: PLR0913  (keyword-only)
    app: Probe,
    peer_url: str,
    health_path: str,
    peer_cell: PeerCell | None = None,
    *,
    egress_hosts: Sequence[str] = (),
    datagw_url: str = "",
    datagw_connection: str = "",
) -> dict[str, Callable[[], str]]:
    peer = urllib.parse.urlencode({"url": peer_url})
    egress = urllib.parse.urlencode([("host", h) for h in egress_hosts])
    return {
        "non_root_10001": lambda: checks.non_root(app.body("/probe/uid")),
        "listens_on_PORT": lambda: (app.get("/"), "answers on $PORT")[1],
        "health_path": lambda: (app.get(health_path), f"{health_path} answers 200")[1],
        "no_write_outside_memory": lambda: checks.no_write_outside_memory(
            app.body("/probe/mounts")
        ),
        "no_direct_egress": lambda: checks.no_direct_egress(app.body(f"/probe/egress?{egress}")),
        "no_dns_exfil": lambda: checks.no_dns_exfil(app.body("/probe/dns")),
        "metadata_token_no_roles": lambda: checks.metadata_token_no_roles(
            app.body("/probe/identity")
        ),
        "metadata_identity_is_own": lambda: checks.metadata_identity_is_own(
            app.body("/probe/identity")
        ),
        "cannot_reach_peer_app": lambda: checks.cannot_reach_peer_app(
            app.body(f"/probe/peer?{peer}")
        ),
        "cannot_reach_peer_cell": lambda: _peer_cell(app, peer_cell),
        "deny_peer_cell": lambda: _deny_peer_cell(app, peer_cell),
        "header_echo_no_google_jwt": lambda: checks.header_echo_no_google_jwt(
            app.body("/probe/headers")
        ),
        "authorization_passthrough": lambda: checks.authorization_passthrough(
            app.body("/probe/headers")
        ),
        "cannot_read_secrets": lambda: checks.cannot_read_secrets(app.body("/probe/identity")),
        "no_platform_credentials_in_env": lambda: checks.no_platform_credentials_in_env(
            app.body("/probe/env")
        ),
        "sse_passthrough": lambda: checks.sse_passthrough(app.sse("/probe/sse")),
        "datagw_read_only": lambda: _datagw_read_only(app, datagw_url, datagw_connection),
    }


def run(  # noqa: PLR0913  (keyword-only)
    app: Probe,
    peer_url: str,
    health_path: str,
    peer_cell: PeerCell | None = None,
    *,
    egress_hosts: Sequence[str] = (),
    datagw_url: str = "",
    datagw_connection: str = "",
) -> list[dict[str, str]]:
    steps = plan(
        app,
        peer_url,
        health_path,
        peer_cell,
        egress_hosts=egress_hosts,
        datagw_url=datagw_url,
        datagw_connection=datagw_connection,
    )
    results: list[dict[str, str]] = []
    for name in checks.PROBES:
        try:
            results.append({"probe": name, "status": "passed", "reason": steps[name]()})
        except checks.ProbeFailedError as exc:
            results.append({"probe": name, "status": "failed", "reason": str(exc)})
        except ProbeSkippedError as exc:
            results.append({"probe": name, "status": "skipped", "reason": str(exc)})
    return results


def main() -> int:
    url, peer_url = os.environ["PROBE_URL"], os.environ["PROBE_PEER_URL"]
    health_path = os.environ.get("PROBE_HEALTH_PATH", "/health")
    results = run(
        Probe(url, id_token(url)),
        peer_url,
        health_path,
        peer_cell_from(os.environ),
        egress_hosts=egress_hosts_from(os.environ),
        datagw_url=os.environ.get(DATAGW_URL_ENV, ""),
        datagw_connection=os.environ.get(DATAGW_CONNECTION_ENV, ""),
    )
    for result in results:
        print(json.dumps({"ssc_probe": result}), flush=True)  # noqa: T201
    failed = [r["probe"] for r in results if r["status"] == "failed"]
    skipped = [r["probe"] for r in results if r["status"] == "skipped"]
    summary = {
        "passed": len(results) - len(failed) - len(skipped),
        "failed": failed,
        "skipped": skipped,
        "total": len(results),
    }
    print(json.dumps({"ssc_probe_summary": summary}), flush=True)  # noqa: T201
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
