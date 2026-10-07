"""The rendered Envoy config, validated and run in real Envoy 1.39 (SSC-018).

The authorisation service and the stream relay run in this process; Envoy and an echo app run
in Docker, the app under the Cloud Run host name the gateway computes. Without Docker these tests
skip locally and fail in CI.
"""

import ast
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
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
    PROD,
    SCH,
    FakeRedeemer,
    Signer,
    World,
    session,
    snapshot,
)
from fastapi import FastAPI

from ssc_app.identity import verify
from ssc_app.reconnect import seconds_left
from ssc_contracts.schedule_token import SCHEDULE_TOKEN_HEADER
from ssc_edge import pages
from ssc_edge.envoy import ENVOY_VERSION, WAKE_SECONDS, EnvoyConfig, render
from ssc_edge.gate import DEADLINE_HEADER, SCHEDULE_HEADER, STREAM_HEADER, WAKE_HEADER
from ssc_edge.identity_note import jwks
from ssc_edge.keys import new_keyring, parse_keyring
from ssc_edge.server import RECHECK_SECONDS, create_app
from ssc_edge.session import WAKE_COOKIE, wake_cookie
from ssc_edge.streams import WATCH_SECONDS, Streams
from ssc_shared.access import AccessView
from ssc_shared.runtime import REQUEST_TIMEOUT_SECONDS, SESSION_TIMEOUT_SECONDS

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
import base64
import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Echo(BaseHTTPRequestHandler):
    def _events(self):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        if self.path.startswith("/events/seen"):
            seen = [[k.lower(), v] for k, v in self.headers.items()]
            self.wfile.write(b"data: " + json.dumps(seen).encode() + b"\\n\\n")
            return
        for n in range(3):
            self.wfile.write(f"data: {n}\\n\\n".encode())
            self.wfile.flush()
            time.sleep(1)

    def _socket(self):
        key = self.headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        self.send_response(101)
        self.send_header("upgrade", "websocket")
        self.send_header("connection", "Upgrade")
        accept = base64.b64encode(hashlib.sha1(key.encode()).digest()).decode()
        self.send_header("sec-websocket-accept", accept)
        self.end_headers()
        seen = [[k.lower(), v] for k, v in self.headers.items()]
        self.wfile.write(json.dumps(seen).encode() + b"\\n")
        self.wfile.flush()
        while data := self.rfile.read1(4096):
            self.wfile.write(data)
            self.wfile.flush()
        self.close_connection = True

    def _go(self):
        if self.headers.get("upgrade", "").lower() == "websocket":
            return self._socket()
        if self.path.startswith("/events"):
            return self._events()
        if self.path.startswith("/slow/"):
            time.sleep(3)
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
        if self.path.startswith("/slow-body/"):
            self.wfile.flush()
            time.sleep(3)
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


def readable(folder: Path) -> None:
    """Let Envoy's own user (uid 101) read ``folder``: pytest makes it 0700 for the CI user, and
    Linux Docker, unlike Docker Desktop, keeps that in the mount (`Invalid path`)."""
    folder.chmod(0o755)
    for f in folder.iterdir():
        f.chmod(0o644)


def validate(tmp: Path, cfg: EnvoyConfig) -> str:
    (tmp / "envoy.json").write_text(json.dumps(render(cfg)))
    readable(tmp)
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


def test_only_a_marked_page_load_is_cut_at_two_seconds() -> None:
    hcm = render(EnvoyConfig())["static_resources"]["listeners"][0]["filter_chains"][0]
    hcm = hcm["filters"][0]["typed_config"]
    stream, wake, rest = hcm["route_config"]["virtual_hosts"][0]["routes"]
    assert stream["match"]["headers"] == [{"name": STREAM_HEADER, "present_match": True}]
    assert stream["route"] == {"cluster": "streams", "timeout": "0s"}
    assert wake["match"]["headers"] == [{"name": WAKE_HEADER, "present_match": True}]
    assert wake["route"]["retry_policy"] == {"num_retries": 0, "per_try_timeout": "2s"}
    assert wake["route"]["timeout"] == rest["route"]["timeout"] == "0s"
    assert wake["request_headers_to_remove"] == [WAKE_HEADER]
    assert rest == {"match": {"prefix": "/"}, "route": {"cluster": "apps", "timeout": "0s"}}
    (mapper,) = hcm["local_reply_config"]["mappers"]
    assert mapper["filter"] == {"response_flag_filter": {"flags": ["UT"]}}
    assert (mapper["status_code"], mapper["body"]["inline_string"]) == (
        503,
        pages.WAKING.decode(),
    )
    authz = hcm["http_filters"][2]["typed_config"]
    assert authz["clear_route_cache"] is True
    on_success = authz["http_service"]["authorization_response"]
    assert on_success["allowed_client_headers_on_success"] == {
        "patterns": [{"exact": "set-cookie"}]
    }
    assert {"exact": STREAM_HEADER} in on_success["allowed_upstream_headers"]["patterns"]


def test_the_relay_gets_one_request_per_connection() -> None:
    clusters = {c["name"]: c for c in render(EnvoyConfig())["static_resources"]["clusters"]}
    options = clusters["streams"]["typed_extension_protocol_options"]
    (http,) = options.values()
    assert http["common_http_protocol_options"] == {"max_requests_per_connection": 1}


@dataclass
class Stack:
    url: str
    world: World
    server: uvicorn.Server
    streams: Streams
    relayed: list[str]
    timer: Signer

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
    timer = Signer()
    gate = world.gate(max_body_bytes=1024, timer=timer)
    port, relay_port = free_port(), free_port()
    published: dict[str, int] = {}
    relayed: list[str] = []

    async def dial(host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        relayed.append(host)
        return await asyncio.open_connection("127.0.0.1", published[host])

    streams = Streams(lambda: gate, dial=dial)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        relay = await streams.serve("0.0.0.0", relay_port)  # noqa: S104
        try:
            yield
        finally:
            relay.close()
            await streams.aclose()

    app = create_app(lambda: gate, lifespan=lifespan, streams=streams)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")  # noqa: S104
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)

    ast.parse(ECHO_APP)
    tag = "ssc018-" + secrets.token_hex(4)
    (tmp / "echo.py").write_text(ECHO_APP)
    cfg = EnvoyConfig(
        authz_host="host.docker.internal",
        authz_port=port,
        stream_host="host.docker.internal",
        stream_port=relay_port,
        upstream_tls=False,
    )
    (tmp / "envoy.json").write_text(json.dumps(render(cfg)))
    readable(tmp)
    run("network", "create", tag)
    try:
        for name, alias in ((f"{tag}-app", UPSTREAM), (f"{tag}-pay", PAY_UPSTREAM)):
            run(
                "run", "-d", "--rm", "--name", name, "--network", tag, "--network-alias", alias,
                "-p", "127.0.0.1::80", "-v", f"{tmp}:/c:ro", APP_IMAGE, "python", "/c/echo.py",
            )  # fmt: skip
            published[alias] = int(run("port", name, "80/tcp").splitlines()[0].rsplit(":", 1)[1])
        # No --rm: an Envoy that exits at once keeps its log for the error below.
        run(
            "run", "-d", "--name", f"{tag}-envoy", "--network", tag,
            "--add-host", "host.docker.internal:host-gateway", "-p", "127.0.0.1::8080",
            "-v", f"{tmp}:/c:ro", ENVOY_IMAGE, "-c", "/c/envoy.json", "--log-level", "warn",
        )  # fmt: skip
        try:
            host_port = run("port", f"{tag}-envoy", "8080/tcp").splitlines()[0].rsplit(":", 1)[1]
        except AssertionError as e:
            logs = subprocess.run(
                [docker(), "logs", f"{tag}-envoy"], capture_output=True, text=True, check=False
            )
            said = (logs.stdout + logs.stderr)[-3000:]
            raise AssertionError(f"Envoy did not start: {said}") from e
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
        yield Stack(url, world, server, streams, relayed, timer)
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


def test_a_timer_call_reaches_the_app_with_a_schedule_note_and_without_its_token(
    stack: Stack,
) -> None:
    """SSC-041: no session, no redirect; the app never sees the schedule token, and a replay
    is the wrong-address page."""
    w = stack.world
    token = stack.timer.token(now=int(time.time()), htu="/tasks/tick?full=1")
    r = stack.get(
        HOST,
        "/tasks/tick?full=1",
        method="POST",
        headers={SCHEDULE_TOKEN_HEADER: token, "x-ssc-identity": "forged"},
    )
    assert r.status_code == 200, r.text
    seen = echoed(r)
    assert r.json()["path"] == "/tasks/tick?full=1"
    assert SCHEDULE_HEADER not in seen and WAKE_HEADER not in seen
    (note_token,) = seen["x-ssc-identity"]
    keys = jwks((w.keyring.signing_key.public_key(), w.keyring.identity_kid))
    note = verify(
        note_token,
        audience=f"https://{HOST}",
        keys=keys,
        issuer=f"https://keys.example.test/{LABEL}",
    )
    assert (note.sub, note.role, note.groups) == (SCH, "schedule", ())
    replay = stack.get(
        HOST, "/tasks/tick?full=1", method="POST", headers={SCHEDULE_TOKEN_HEADER: token}
    )
    assert (replay.status_code, replay.content) == (404, pages.NOT_FOUND)
    assert "location" not in replay.headers


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
    assert r.headers["location"].startswith("https://auth.example.test/login?org=")
    assert r.headers["set-cookie"].startswith("__Host-ssc-login=")


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
    assert stack.relayed[-1] == UPSTREAM


def open_socket(stack: Stack, cookie: str) -> tuple[socket.socket, list[list[str]]]:
    """A WebSocket upgrade through Envoy; returns the socket and the headers the app saw."""
    sock = socket.create_connection(("127.0.0.1", int(stack.url.rsplit(":", 1)[1])), timeout=5)
    sock.sendall(
        f"GET /ws HTTP/1.1\r\nhost: {HOST}\r\ncookie: {cookie}\r\nconnection: Upgrade\r\n"
        "upgrade: websocket\r\nsec-websocket-version: 13\r\n"
        f"sec-websocket-key: dGhlIHNhbXBsZSBub25jZQ==\r\norigin: https://{HOST}\r\n\r\n".encode()
    )
    got = b""
    while b"\n" not in got.partition(b"\r\n\r\n")[2]:
        chunk = sock.recv(4096)
        assert chunk, got
        got += chunk
    head, _, body = got.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 101 "), head
    return sock, json.loads(body.partition(b"\n")[0])


def test_a_websocket_is_closed_within_one_watch_of_its_grant_going(stack: Stack) -> None:
    w = stack.world
    cookie = w.cookie(session(BEN, iat=w.now - 60))
    before = w.view
    sock, seen = open_socket(stack, cookie)
    try:
        names = {k for k, _ in seen}
        assert STREAM_HEADER not in names and "x-ssc-identity" in names
        assert ["host", UPSTREAM] in seen
        sock.sendall(b"ping")
        assert sock.recv(4) == b"ping"
        grants = snapshot()["grants"]
        w.view = AccessView.from_document(snapshot(2, grants={**grants, PROD: grants[PROD][1:]}))
        started = time.monotonic()
        sock.settimeout(WATCH_SECONDS + 3)
        try:
            rest = sock.recv(4096)
        except ConnectionResetError:
            rest = b""
        took = time.monotonic() - started
        assert rest == b""
        assert took <= RECHECK_SECONDS + WATCH_SECONDS + 0.5, took
        assert stack.get(HOST, headers={"cookie": cookie}).status_code == 404
    finally:
        sock.close()
        w.view = before


PAGE_LOAD = {
    "sec-fetch-site": "none",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
    "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


def timed(stack: Stack, path: str, headers: dict[str, str]) -> tuple[httpx2.Response, float]:
    started = time.monotonic()
    r = stack.get(HOST, path, headers={"cookie": stack.world.cookie(), **headers})
    return r, time.monotonic() - started


def test_a_slow_app_shows_a_page_load_the_waking_page(stack: Stack) -> None:
    r, took = timed(stack, "/slow/a", PAGE_LOAD)
    assert r.status_code == 503
    assert r.content == pages.WAKING
    assert WAKE_SECONDS <= took < WAKE_SECONDS + 0.8
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers.get_list("set-cookie") == [wake_cookie()]


def test_the_retry_with_the_wake_cookie_waits_for_the_app(stack: Stack) -> None:
    w = stack.world
    cookie = f"{w.cookie()}; {WAKE_COOKIE}=1"
    r, took = timed(stack, "/slow/b", {**PAGE_LOAD, "cookie": cookie})
    assert r.status_code == 200, r.text
    assert took >= 3
    seen = echoed(r)
    assert WAKE_HEADER not in seen
    assert "cookie" not in seen
    assert r.headers.get_list("set-cookie") == ["app=2; Path=/"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"accept": "application/json", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"},
        {**PAGE_LOAD, "sec-fetch-dest": "iframe"},
        {**PAGE_LOAD, "accept": "*/*"},
    ],
    ids=["script", "fetch", "frame", "no-html"],
)
def test_anything_but_a_page_load_waits_for_a_slow_app(
    stack: Stack, headers: dict[str, str]
) -> None:
    r, took = timed(stack, "/slow/c", headers)
    assert r.status_code == 200, r.text
    assert took >= 3
    assert r.headers.get_list("set-cookie") == ["app=2; Path=/"]


def test_an_answer_that_has_started_is_never_cut(stack: Stack) -> None:
    r, took = timed(stack, "/slow-body/d", PAGE_LOAD)
    assert r.status_code == 200, r.text
    assert took >= 3
    assert r.json()["path"] == "/slow-body/d"
    assert sorted(r.headers.get_list("set-cookie")) == sorted(["app=2; Path=/", wake_cookie()])


@pytest.mark.parametrize(
    ("host", "seconds"),
    [(HOST, REQUEST_TIMEOUT_SECONDS), (PAY_HOST, SESSION_TIMEOUT_SECONDS)],
    ids=["request-billed", "session"],
)
def test_the_app_learns_when_cloud_run_will_end_the_request(
    stack: Stack, host: str, seconds: int
) -> None:
    w = stack.world
    r = stack.get(
        host, "/limit", headers={"cookie": w.cookie(session(iat=w.now - 60), host), **PAGE_LOAD}
    )
    assert r.status_code == 200, r.text
    seen = echoed(r)
    assert seen[DEADLINE_HEADER] == [str(w.now + seconds)]
    assert WAKE_HEADER not in seen


def test_a_relayed_stream_still_tells_the_app_its_deadline(stack: Stack) -> None:
    w = stack.world
    sock, seen = open_socket(stack, w.cookie())
    sock.close()
    assert [DEADLINE_HEADER, str(w.now + REQUEST_TIMEOUT_SECONDS)] in seen
    assert seconds_left(dict(seen), now=w.now) == REQUEST_TIMEOUT_SECONDS
    assert stack.relayed[-1] == UPSTREAM


def test_a_relayed_event_stream_tells_the_app_its_deadline(stack: Stack) -> None:
    w = stack.world
    with httpx2.stream(
        "GET",
        stack.url + "/events/seen",
        headers={
            "host": PAY_HOST,
            "cookie": w.cookie(session(iat=w.now - 60), PAY_HOST),
            "accept": "text/event-stream",
        },
        timeout=10,
    ) as r:
        assert r.status_code == 200
        lines = [line for line in r.iter_lines() if line.startswith("data: ")]
    seen = json.loads(lines[0].removeprefix("data: "))
    names = {k for k, _ in seen}
    assert STREAM_HEADER not in names and "x-ssc-identity" in names
    assert [DEADLINE_HEADER, str(w.now + SESSION_TIMEOUT_SECONDS)] in seen
    assert seconds_left(dict(seen), now=w.now) == SESSION_TIMEOUT_SECONDS
    assert stack.relayed[-1] == PAY_UPSTREAM


def test_every_request_but_a_page_or_a_static_file_goes_through_the_relay(stack: Stack) -> None:
    """SSC-021: a fetch(), a plain call and a POST reach the app through the relay, which can cut
    them; a page load and a script do not. Either way the app's answer is the same."""
    cookie = {"host": HOST, "cookie": stack.world.cookie()}
    cases = [
        ("GET", {"sec-fetch-dest": "empty", "sec-fetch-mode": "cors"}, True),
        ("GET", {}, True),
        ("POST", {}, True),
        ("GET", {"sec-fetch-dest": "script"}, False),
        ("GET", {"sec-fetch-dest": "document", "sec-fetch-mode": "navigate"}, False),
    ]
    for method, extra, through in cases:
        before = len(stack.relayed)
        r = httpx2.request(method, stack.url + "/x", headers={**cookie, **extra}, timeout=10)
        assert r.status_code == 200, (method, extra)
        assert r.json()["path"] == "/x"
        assert r.headers.get_list("set-cookie") == ["app=2; Path=/"]
        assert (len(stack.relayed) > before) == through, (method, extra)


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
