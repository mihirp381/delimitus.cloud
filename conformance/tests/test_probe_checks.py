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
    assert len(checks.PROBES) == len(set(checks.PROBES)) == 15


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


GOOGLE_404 = {
    "status": 404,
    "headers": {"content-type": "text/html; charset=UTF-8", "referrer-policy": "no-referrer"},
    "body": "<!DOCTYPE html><html lang=en><title>Error 404 (Not Found)!!1</title>",
}
GATEWAY_404_HEADERS = {
    "content-type": "text/html; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


def test_peer_cell_must_refuse_before_iam() -> None:
    refused = {
        "app by name": {"error": "URLError"},
        "app by Google VIP with ID token": GOOGLE_404,
        "gateway by name": {"error": "TimeoutError"},
        "gateway by name with ID token": {"status": 404},
        "tcp 10.30.0.2:443": {"blocked": True, "detail": "TimeoutError"},
    }
    body = {"range": "10.30.0.0/22", "own": "10.20.0.5", "own_in_range": False}
    passed = checks.cannot_reach_peer_cell(body | {"attempts": refused})
    assert "app by Google VIP with ID token: HTTP 404" in passed
    assert checks.SAME_RANGE not in passed
    for name, attempt in (
        ("app by name with ID token", {"status": 403}),
        ("gateway by name", {"status": 401}),
        ("gateway by name", {"status": 302}),
        ("app by name", {"status": 200, "body": '"ok"'}),
        ("tcp 10.30.0.2:8080", {"blocked": False, "detail": "connected"}),
        ("app by name", {}),
    ):
        with pytest.raises(checks.ProbeFailedError, match="let calls through"):
            checks.cannot_reach_peer_cell(body | {"attempts": refused | {name: attempt}})
    with pytest.raises(checks.ProbeFailedError, match="own address"):
        checks.cannot_reach_peer_cell(body | {"own_in_range": None, "attempts": refused})
    with pytest.raises(checks.ProbeFailedError, match="no attempts"):
        checks.cannot_reach_peer_cell(body | {"attempts": {}})
    with pytest.raises(checks.ProbeFailedError, match="not an address range"):
        checks.cannot_reach_peer_cell({"error": "not an address range: 'x'"})


@pytest.mark.parametrize(
    ("answer", "marker"),
    [
        ({"headers": {"server": "BaseHTTP/0.6 Python/3.14.0"}}, "probe app server header"),
        ({"headers": {"content-type": "application/json"}}, "probe app JSON"),
        ({"body": "null"}, "probe app body"),
        ({"headers": {"server": "envoy"}}, "Envoy server header"),
        ({"headers": GATEWAY_404_HEADERS}, "gateway page headers"),
        ({"body": "<h1>Not found</h1><p>There is no app at this address, or"}, "gateway page"),
    ],
)
def test_a_404_from_the_peer_itself_is_a_failure(answer: dict[str, Any], marker: str) -> None:
    body = {"range": "10.30.0.0/22", "own": "10.20.0.5", "own_in_range": False}
    attempts = {"gateway by name": {"status": 404} | answer}
    with pytest.raises(checks.ProbeFailedError, match=f"from the peer itself \\({marker}"):
        checks.cannot_reach_peer_cell(body | {"attempts": attempts})


def test_the_same_range_leg_is_not_applicable_and_said_so() -> None:
    body = {"range": "10.20.0.0/22", "own": "10.20.0.5", "own_in_range": True}
    attempts = {
        "app by name": GOOGLE_404,
        "gateway by name": {"error": "TimeoutError"},
        "tcp 10.20.0.2:443": {"blocked": False, "detail": "connected"},
    }
    passed = checks.cannot_reach_peer_cell(body | {"attempts": attempts})
    assert passed.endswith("; range leg not applicable: same range, separate networks")
    assert "tcp" not in passed
    with pytest.raises(checks.ProbeFailedError, match="gateway by name: HTTP 403"):
        checks.cannot_reach_peer_cell(
            body | {"attempts": attempts | {"gateway by name": {"status": 403}}}
        )
    with pytest.raises(checks.ProbeFailedError, match="no attempts"):
        checks.cannot_reach_peer_cell(body | {"attempts": {"tcp 10.20.0.2:443": {"blocked": True}}})


def test_the_probe_app_answers_with_its_own_marks(local_app: str) -> None:
    for path in ("/health", "/no-such-path"):
        answer = app._answer(*app._exchange(local_app + path), full=True)
        assert answer["headers"]["server"].startswith("BaseHTTP/")
        assert checks._peer_marker(answer)
    assert app._answer(*app._exchange(local_app + "/health"), full=True)["body"] == '"ok"'
    assert app._answer(404, {"server": "x"}, b"", full=False) == {"status": 404}
    raw = (
        b"HTTP/1.1 404 Not Found\r\nServer: envoy\r\nContent-Type: text/html; charset=utf-8\r\n"
        b"Set-Cookie: s=1\r\n\r\n<h1>Not found</h1>"
    )
    status, headers, data = app._parse_response(raw)
    assert (status, headers["server"], data) == (404, "envoy", b"<h1>Not found</h1>")
    answer = app._answer(status, headers, data, full=True)
    assert "set-cookie" not in answer["headers"]
    assert checks._peer_marker(answer) == "Envoy server header"


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
        "mounts": [["/", "overlay"], ["/tmp", "tmpfs"], ["/var/log", "fuse.loggingfs"]],
        "home": "/tmp",
        "home_writable": True,
    }
    assert checks.no_write_outside_memory(memory)
    for fs in ("nfs4", "fuse.gcsfuse", "ext4"):
        with pytest.raises(checks.ProbeFailedError, match=fs):
            checks.no_write_outside_memory(memory | {"mounts": [["/data", fs]]})
    with pytest.raises(checks.ProbeFailedError, match="loggingfs"):
        checks.no_write_outside_memory(memory | {"mounts": [["/data", "fuse.loggingfs"]]})
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
    results = runner.run(runner.Probe(local_app, "t"), local_app + "/", "/health")
    by_name = {r["probe"]: r for r in results}
    assert list(by_name) == list(checks.PROBES)
    for name in ("listens_on_PORT", "health_path", "sse_passthrough", "authorization_passthrough"):
        assert by_name[name]["status"] == "passed", by_name[name]
    for name in ("metadata_token_no_roles", "metadata_identity_is_own", "cannot_reach_peer_app"):
        assert by_name[name]["status"] == "failed", by_name[name]
    assert by_name["cannot_reach_peer_cell"] == {
        "probe": "cannot_reach_peer_cell",
        "status": "skipped",
        "reason": "no peer cell",
    }


def test_the_peer_cell_comes_from_three_variables() -> None:
    full = dict(zip(runner.PEER_CELL_ENV, ("https://a", "https://g", "10.30.0.0/22"), strict=True))
    assert runner.peer_cell_from(full) == ("https://a", "https://g", "10.30.0.0/22")
    assert runner.peer_cell_from({}) is None
    for name in runner.PEER_CELL_ENV:
        assert runner.peer_cell_from(full | {name: ""}) is None


@pytest.mark.parametrize(
    ("own", "status", "legs"),
    [
        ("10.20.0.5", "passed", ("tcp 10.30.0.2:443", "tcp 10.30.0.3:8080")),
        ("10.30.1.7", "passed", (checks.SAME_RANGE,)),
        (None, "failed", ()),
    ],
)
def test_runner_drives_the_peer_cell_probe_through_the_app(  # noqa: PLR0913  (fixtures)
    local_app: str,
    monkeypatch: pytest.MonkeyPatch,
    own: str | None,
    status: str,
    legs: tuple[str, ...],
) -> None:
    called: list[tuple[str, str, bool]] = []
    tcp: list[str] = []

    def peer(url: str, path: str = "/", *, full: bool = False) -> dict[str, object]:
        called.append((url, path, full))
        return {"url": url, "attempts": {"by name": GOOGLE_404}}

    def connect(host: str, port: int, family: int = 0) -> dict[str, object]:
        tcp.extend([f"{host}:{port}"] if host.startswith("10.30.") else [])
        return {"blocked": True, "detail": f"Timeout {family}"}

    monkeypatch.setattr(app, "peer", peer)
    monkeypatch.setattr(app, "_tcp", connect)
    monkeypatch.setattr(app, "_own_address", lambda toward: own)
    peer_cell = ("https://ssc-a-x.run.app", "https://ssc-gateway-x.run.app", "10.30.0.0/22")
    results = runner.run(runner.Probe(local_app, "t"), local_app + "/", "/health", peer_cell)
    (result,) = [r for r in results if r["probe"] == "cannot_reach_peer_cell"]
    assert result["status"] == status, result
    assert [c for c in called if c[2]] == [
        ("https://ssc-a-x.run.app", "/health", True),
        ("https://ssc-gateway-x.run.app", "/.ssc/logout", True),
    ]
    assert bool(tcp) is (own != "10.30.1.7")
    for leg in ("app by name", "gateway by name", *legs) if status == "passed" else ():
        assert leg in result["reason"]
