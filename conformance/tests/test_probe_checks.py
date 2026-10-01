"""The in-cell probe checks decide correctly on what an app could report, and the runner drives
the probe app end to end on this machine (where the cell probes must fail, not pass)."""

import base64
import importlib
import json
import sys
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ssc_conformance import runtime_probes

PROBE_APP = Path(__file__).resolve().parents[1] / "runtime" / "probe_app"


def _load(name: str) -> ModuleType:
    if str(PROBE_APP) not in sys.path:
        sys.path.insert(0, str(PROBE_APP))
    return importlib.import_module(name)


checks: Any = _load("checks")
app: Any = _load("app")
runner: Any = _load("runner")


def _jwt(claims: dict[str, object], signature: str) -> str:
    def enc(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.{signature}"


def test_probe_lists_agree() -> None:
    assert checks.LOCAL_PROBES == runtime_probes.LOCAL_PROBES
    assert checks.CELL_PROBES == runtime_probes.CELL_PROBES
    assert len(checks.PROBES) == len(set(checks.PROBES)) == 14


def test_egress_and_dns() -> None:
    blocked = {"blocked": True, "detail": "TimeoutError"}
    assert checks.no_direct_egress({"tcp 1.1.1.1:443": blocked})
    with pytest.raises(checks.ProbeFailedError, match="1.1.1.1:80"):
        checks.no_direct_egress({"a": blocked, "tcp 1.1.1.1:80": {"blocked": False}})
    quiet = {"answers": {"example.com": [], "x.example.com": ["192.0.2.1"]}, "direct": blocked}
    assert checks.no_dns_exfil(quiet)
    with pytest.raises(checks.ProbeFailedError, match="public names"):
        checks.no_dns_exfil({"answers": {"example.com": ["93.184.215.14"]}, "direct": blocked})
    with pytest.raises(checks.ProbeFailedError, match="resolver"):
        checks.no_dns_exfil({"answers": {}, "direct": {"blocked": False}})


def test_identity_checks() -> None:
    own = {
        "k_service": "ssc-a-probe00000000000000a",
        "project": "ssc-c-x",
        "email": "ssc-a-probe00000000000000a@ssc-c-x.iam.gserviceaccount.com",
        "granted": [],
        "secret_access_status": 403,
        "secret_list_status": 403,
    }
    assert checks.metadata_identity_is_own(own)
    assert checks.metadata_token_no_roles(own)
    assert checks.cannot_read_secrets(own)
    with pytest.raises(checks.ProbeFailedError):
        checks.metadata_identity_is_own(
            own | {"email": "123-compute@developer.gserviceaccount.com"}
        )
    with pytest.raises(checks.ProbeFailedError):
        checks.metadata_token_no_roles(own | {"granted": ["storage.objects.get"]})
    with pytest.raises(checks.ProbeFailedError):
        checks.metadata_token_no_roles(own | {"granted": None, "granted_status": 403})
    with pytest.raises(checks.ProbeFailedError):
        checks.cannot_read_secrets(own | {"secret_access_status": 200})
    with pytest.raises(checks.ProbeFailedError):
        checks.cannot_read_secrets(own | {"secret_list_status": "TimeoutError"})


def test_peer_must_never_answer() -> None:
    refused = {"by name": {"error": "TimeoutError"}, "by Google VIP with ID token": {"status": 403}}
    assert checks.cannot_reach_peer_app({"attempts": refused})
    with pytest.raises(checks.ProbeFailedError, match="HTTP 200"):
        checks.cannot_reach_peer_app({"attempts": refused | {"by name": {"status": 200}}})
    with pytest.raises(checks.ProbeFailedError):
        checks.cannot_reach_peer_app({"attempts": {}, "error": "no url"})


def test_google_tokens_must_arrive_unsigned() -> None:
    google = {"iss": "https://accounts.google.com", "email": "ssc-gateway@p"}
    stripped = {
        "x-serverless-authorization": "Bearer " + _jwt(google, "SIGNATURE_REMOVED_BY_GOOGLE")
    }
    assert checks.header_echo_no_google_jwt(stripped)
    assert checks.header_echo_no_google_jwt({"x-other": _jwt({"iss": "me"}, "abc")})
    with pytest.raises(checks.ProbeFailedError, match="x-serverless-authorization"):
        checks.header_echo_no_google_jwt(
            {"x-serverless-authorization": "Bearer " + _jwt(google, "c2lnbmF0dXJl")}
        )
    assert checks.authorization_passthrough({"authorization": checks.APP_CREDENTIAL})
    with pytest.raises(checks.ProbeFailedError):
        checks.authorization_passthrough({"authorization": stripped["x-serverless-authorization"]})


def test_env_mounts_and_sse() -> None:
    assert checks.no_platform_credentials_in_env({"names": ["HOME", "PORT", "K_SERVICE"]})
    for name in ("GOOGLE_APPLICATION_CREDENTIALS", "SSC_CELL_PROJECT", "DB_PASSWORD", "GH_TOKEN"):
        with pytest.raises(checks.ProbeFailedError, match=name):
            checks.no_platform_credentials_in_env({"names": ["HOME", name]})
    memory = {
        "mounts": [["/", "overlay"], ["/tmp", "tmpfs"]],
        "home": "/tmp",
        "home_writable": True,
    }
    assert checks.no_write_outside_memory(memory)
    for fs in ("nfs4", "fuse.gcsfuse", "ext4"):
        with pytest.raises(checks.ProbeFailedError, match=fs):
            checks.no_write_outside_memory(memory | {"mounts": [["/data", fs]]})
    with pytest.raises(checks.ProbeFailedError, match="HOME"):
        checks.no_write_outside_memory(memory | {"home_writable": False})
    assert checks.sse_passthrough([0.1, 1.1, 2.1])
    with pytest.raises(checks.ProbeFailedError, match="buffered"):
        checks.sse_passthrough([2.0, 2.0, 2.01])
    with pytest.raises(checks.ProbeFailedError, match="2 of 3"):
        checks.sse_passthrough([0.1, 1.1])


@pytest.fixture(scope="module")
def local_app() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_runner_streams_and_echoes_through_the_app(local_app: str) -> None:
    probe = runner.Probe(local_app, "gateway-id-token")
    assert checks.sse_passthrough(probe.sse("/probe/sse"))
    headers = probe.body("/probe/headers")
    assert headers["x-serverless-authorization"] == "Bearer gateway-id-token"
    assert checks.authorization_passthrough(headers)


def test_runner_reports_every_probe_and_never_passes_off_cell(
    local_app: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "identity", lambda: {"error": "no metadata server"})
    monkeypatch.setattr(
        app, "peer", lambda url: {"url": url, "attempts": {"by name": {"status": 200}}}
    )
    results = runner.run(runner.Probe(local_app, "t"), local_app + "/", "/healthz")
    by_name = {r["probe"]: r for r in results}
    assert list(by_name) == list(checks.PROBES)
    for name in ("listens_on_PORT", "health_path", "sse_passthrough", "authorization_passthrough"):
        assert by_name[name]["status"] == "passed", by_name[name]
    for name in ("metadata_token_no_roles", "metadata_identity_is_own", "cannot_reach_peer_app"):
        assert by_name[name]["status"] == "failed", by_name[name]
