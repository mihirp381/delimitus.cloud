"""The rendered Envoy config, validated and run in real Envoy 1.39 (SSC-018).

The authorisation service runs in this process; Envoy and an echo app run in Docker, the app
under the Cloud Run host name the gateway computes. Without Docker these tests skip locally and
fail in CI.
"""

import ast
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
import uvicorn
from edge_world import (
    BEN,
    HOST,
    LABEL,
    NOWHERE_HOST,
    PAY_HOST,
    FakeRedeemer,
    World,
    session,
    snapshot,
)

from ssc_app.identity import verify
from ssc_edge.envoy import ENVOY_VERSION, EnvoyConfig, render
from ssc_edge.identity_note import jwks
from ssc_edge.keys import new_keyring, parse_keyring
from ssc_edge.server import create_app
from ssc_shared.access import AccessView

ENVOY_IMAGE = (
    f"envoyproxy/envoy:v{ENVOY_VERSION}"
    "@sha256:d59f7f5fa10cff6d5892b6c5e7df5c9297ddfb2c3683e33fbfb82da24de4fa66"
)
APP_IMAGE = (
    "python:3.11.15-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff"
)
UPSTREAM = "ssc-a-" + "p" * 20 + "-123456789012.us-central1.run.app"
PAY_UPSTREAM = "ssc-a-" + "y" * 20 + "-123456789012.us-central1.run.app"
ECHO_APP = """
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Echo(BaseHTTPRequestHandler):
    def _events(self):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        for n in range(3):
            self.wfile.write(f"data: {n}\\n\\n".encode())
            self.wfile.flush()
            time.sleep(1)

    def _go(self):
        if self.path.startswith("/events"):
            return self._events()
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        seen = [[k.lower(), v] for k, v in self.headers.items()]
        body = json.dumps({"path": self.path, "headers": seen}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("set-cookie", "__Host-ssc-session=evil; Path=/; Secure")
        self.send_header("Set-Cookie", "__secure-SSC-x=1; Secure")
        self.send_header("set-cookie", "app=2; Path=/")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = _go
    def log_message(self, *a): pass

ThreadingHTTPServer(("0.0.0.0", 80), Echo).serve_forever()
"""


def docker() -> str:
    found = shutil.which("docker")
    if (
        found is None
        or subprocess.run([found, "info"], capture_output=True, check=False).returncode
    ):
        if os.environ.get("CI"):
            pytest.fail("Docker is required in CI for the Envoy tests")
        pytest.skip("Docker is not available")
    return found


def run(*args: str) -> str:
    out = subprocess.run([docker(), *args], capture_output=True, text=True, check=False)
    if out.returncode:
        raise AssertionError(f"docker {args[0]} failed: {out.stderr[-2000:]}")
    return out.stdout.strip()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def validate(tmp: Path, cfg: EnvoyConfig) -> str:
    (tmp / "envoy.json").write_text(json.dumps(render(cfg)))
    out = subprocess.run(
        [
            docker(),
            "run",
            "--rm",
            "-v",
            f"{tmp}:/c:ro",
            ENVOY_IMAGE,
            "--mode",
            "validate",
            "-c",
            "/c/envoy.json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout + out.stderr


@pytest.mark.parametrize("tls", [True, False], ids=["production", "plaintext"])
def test_the_rendered_config_validates(tmp_path: Path, tls: bool) -> None:
    out = validate(tmp_path, EnvoyConfig(upstream_tls=tls))
    assert "configuration '/c/envoy.json' OK" in out, out[-2000:]
    assert "deprecated" not in out.lower()


def test_the_check_fails_closed_and_cannot_be_skipped() -> None:
    cfg = render(EnvoyConfig())
    hcm = cfg["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]["typed_config"]
    names = [f["name"] for f in hcm["http_filters"]]
    assert names == [
        "envoy.filters.http.local_ratelimit",
        "ssc.strip",
        "envoy.filters.http.ext_authz",
        "ssc.route",
        "envoy.filters.http.dynamic_forward_proxy",
        "envoy.filters.http.router",
    ]
    authz = hcm["http_filters"][2]["typed_config"]
    assert authz["failure_mode_allow"] is False
    assert authz["status_on_error"] == {"code": "ServiceUnavailable"}
    routes = hcm["route_config"]["virtual_hosts"][0]["routes"]
    assert all("typed_per_filter_config" not in r for r in routes)
    assert "admin" not in cfg


@dataclass
class Stack:
    url: str
    world: World
    server: uvicorn.Server

    def get(self, host: str, path: str = "/", method: str = "GET", **kw: object) -> httpx2.Response:
        headers = {"host": host, **dict(kw.pop("headers", {}) or {})}  # type: ignore[arg-type]
        return httpx2.request(method, self.url + path, headers=headers, timeout=10, **kw)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    docker()
    tmp = tmp_path_factory.mktemp("envoy")
    world = World(
        keyring=parse_keyring(new_keyring()),
        view=AccessView.from_document(snapshot()),
        redeemer=FakeRedeemer(),
    )
    world.now = int(time.time())  # Envoy runs on the real clock
    gate = world.gate(max_body_bytes=1024)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(lambda: gate), host="0.0.0.0", port=port, log_level="warning")  # noqa: S104
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)

    ast.parse(ECHO_APP)
    tag = "ssc018-" + secrets.token_hex(4)
    (tmp / "echo.py").write_text(ECHO_APP)
    cfg = EnvoyConfig(authz_host="host.docker.internal", authz_port=port, upstream_tls=False)
    (tmp / "envoy.json").write_text(json.dumps(render(cfg)))
    run("network", "create", tag)
    try:
        for name, alias in ((f"{tag}-app", UPSTREAM), (f"{tag}-pay", PAY_UPSTREAM)):
            run(
                "run", "-d", "--rm", "--name", name, "--network", tag, "--network-alias", alias,
                "-v", f"{tmp}:/c:ro", APP_IMAGE, "python", "/c/echo.py",
            )  # fmt: skip
        run(
            "run", "-d", "--rm", "--name", f"{tag}-envoy", "--network", tag,
            "--add-host", "host.docker.internal:host-gateway", "-p", "127.0.0.1::8080",
            "-v", f"{tmp}:/c:ro", ENVOY_IMAGE, "-c", "/c/envoy.json", "--log-level", "warn",
        )  # fmt: skip
        host_port = run("port", f"{tag}-envoy", "8080/tcp").splitlines()[0].rsplit(":", 1)[1]
        time.sleep(0.5)
        for name in (f"{tag}-app", f"{tag}-pay"):
            # A dead app drops its alias, and Envoy would resolve the real run.app name.
            assert run("inspect", "-f", "{{.State.Running}}", name) == "true", name
        url = f"http://127.0.0.1:{host_port}"
        deadline = time.monotonic() + 30
        while True:
            try:
                httpx2.get(url, headers={"host": NOWHERE_HOST}, timeout=1)
                break
            except httpx2.HTTPError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        yield Stack(url, world, server)
    finally:
        subprocess.run(
            [docker(), "rm", "-f", f"{tag}-envoy", f"{tag}-app", f"{tag}-pay"],
            capture_output=True,
            check=False,
        )
        subprocess.run([docker(), "network", "rm", tag], capture_output=True, check=False)
        server.should_exit = True
        thread.join(timeout=5)


def echoed(r: httpx2.Response) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for k, v in r.json()["headers"]:
        seen.setdefault(k, []).append(v)
    return seen


def test_an_allowed_request_reaches_the_app_with_only_the_minted_note(stack: Stack) -> None:
    w = stack.world
    r = stack.get(
        HOST,
        "/books?y=1",
        headers={
            "cookie": f"app=1; {w.cookie()}; __Secure-ssc-other=2",
            "x-ssc-identity": "forged",
            "X-SSC-Upstream": "evil.example",
            "x-ssc-content-length": "0",
            "x-envoy-original-path": "/admin",
            "x-forwarded-host": "evil.example",
            "x-serverless-authorization": "Bearer stolen",
        },
    )
    assert r.status_code == 200, r.text
    seen = echoed(r)
    assert r.json()["path"] == "/books?y=1"
    assert seen["host"] == [UPSTREAM]
    assert seen["x-forwarded-host"] == [HOST]
    assert seen["cookie"] == ["app=1"]
    dropped = {"x-ssc-upstream", "x-ssc-content-length", "x-envoy-original-path"}
    assert not (dropped | {"x-serverless-authorization"}) & seen.keys()  # no tokens in tests
    (token,) = seen["x-ssc-identity"]
    keys = jwks((w.keyring.signing_key.public_key(), w.keyring.identity_kid))
    note = verify(
        token, audience=f"https://{HOST}", keys=keys, issuer=f"https://keys.example.test/{LABEL}"
    )
    assert note.app.startswith("app_")
    assert r.headers.get_list("set-cookie") == ["app=2; Path=/"]


def test_a_forbidden_app_answers_exactly_like_no_app(stack: Stack) -> None:
    w = stack.world
    forbidden = stack.get(
        PAY_HOST, headers={"cookie": w.cookie(session(BEN, iat=w.now - 60), PAY_HOST)}
    )
    nowhere = stack.get(
        NOWHERE_HOST, headers={"cookie": w.cookie(session(BEN, iat=w.now - 60), NOWHERE_HOST)}
    )
    foreign = stack.get("payroll.example.com")
    assert forbidden.status_code == 404
    for other in (nowhere, foreign):
        assert other.status_code == forbidden.status_code
        assert other.content == forbidden.content
        drop = {"date", "x-envoy-upstream-service-time"}
        assert sorted((k, v) for k, v in other.headers.multi_items() if k not in drop) == sorted(
            (k, v) for k, v in forbidden.headers.multi_items() if k not in drop
        )


def test_no_session_is_sent_to_login(stack: Stack) -> None:
    r = stack.get(HOST, "/x")
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://auth.example.test/login?return_to=")


def test_request_shape_is_refused_before_the_app(stack: Stack) -> None:
    cookie = {"cookie": stack.world.cookie()}
    post = stack.get(
        HOST,
        method="POST",
        headers={**cookie, "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"},
    )
    assert post.status_code == 403
    ws = stack.get(
        HOST,
        headers={
            **cookie,
            "connection": "upgrade",
            "upgrade": "websocket",
            "sec-websocket-version": "13",
            "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
            "origin": f"https://{PAY_HOST}",
        },
    )
    assert ws.status_code == 403
    big = stack.get(HOST, method="POST", headers=cookie, content=b"x" * 2048)
    assert big.status_code == 413
    small = stack.get(HOST, method="POST", headers=cookie, content=b"x" * 1000)
    assert small.status_code == 200


def test_server_sent_events_stream_through_unbuffered(stack: Stack) -> None:
    started = time.monotonic()
    arrived: list[tuple[str, float]] = []
    with httpx2.stream(
        "GET",
        stack.url + "/events",
        headers={"host": HOST, "cookie": stack.world.cookie(), "accept": "text/event-stream"},
        timeout=10,
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"] == "text/event-stream"
        for line in r.iter_lines():
            if line.startswith("data: "):
                arrived.append((line, time.monotonic() - started))
    assert [line for line, _ in arrived] == ["data: 0", "data: 1", "data: 2"]
    assert arrived[0][1] < 0.9, arrived  # before the app sent the second event
    assert arrived[2][1] - arrived[0][1] > 1.5, arrived


def test_everything_is_refused_when_the_authoriser_stops(stack: Stack) -> None:
    stack.server.should_exit = True
    deadline = time.monotonic() + 10
    while stack.server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    for host, headers in ((HOST, {"cookie": stack.world.cookie()}), (NOWHERE_HOST, {})):
        r = stack.get(host, headers=headers)
        assert r.status_code == 503, (host, r.status_code)
        assert "x-ssc-identity" not in r.text
