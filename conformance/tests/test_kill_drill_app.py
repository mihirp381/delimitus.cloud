"""The drill app of SSC-054, with the data gateway, the proxy and the metadata server faked."""

import importlib.util
import json
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

APP_PATH = Path(__file__).parents[1] / "kill_drill_app" / "main.py"
PASSWORD = "proxy-password-value"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kill_drill_app_main", APP_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app() -> ModuleType:
    return load()


def lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


class FakeProxy:
    """A proxy that answers one CONNECT with ``status`` and then closes after the first request
    sent through the tunnel."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.heard: list[bytes] = []
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        conn, _ = self._server.accept()
        with conn:
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(1024)
            self.heard.append(head)
            conn.sendall(f"HTTP/1.1 {self.status} X\r\n\r\n".encode())
            if self.status == 200:
                self.heard.append(conn.recv(1024))

    def close(self) -> None:
        self._server.close()


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    made: list[FakeProxy] = []

    def make(status: int) -> FakeProxy:
        fake = FakeProxy(status)
        made.append(fake)
        monkeypatch.setenv("HTTPS_PROXY", f"http://app-env:{PASSWORD}@127.0.0.1:{fake.port}")
        return fake

    yield make
    for fake in made:
        fake.close()


def test_emit_writes_one_structured_line(
    app: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    app.emit("r1", "query", "end", outcome="x", running=True)
    [line] = lines(capsys)
    assert line["severity"] == "INFO"
    assert line["drill"]["run"] == "r1"
    assert (line["drill"]["leg"], line["drill"]["event"]) == ("query", "end")
    assert line["drill"]["outcome"] == "x"
    assert line["drill"]["running"] is True
    assert isinstance(line["drill"]["at"], float)


def test_the_app_logs_ready_once_per_process(capsys: pytest.CaptureFixture[str]) -> None:
    load()
    [line] = lines(capsys)
    assert (line["drill"]["leg"], line["drill"]["event"]) == ("app", "ready")


def test_the_data_gateway_url_is_given_or_read_from_the_metadata_server(
    app: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSC_DATAGW_URL", "https://datagw.test")
    assert app.datagw_url() == "https://datagw.test"
    monkeypatch.delenv("SSC_DATAGW_URL")
    monkeypatch.setattr(app, "_get", lambda url, headers: "projects/123456/regions/us-central1")
    assert app.datagw_url() == "https://ssc-datagw-123456.us-central1.run.app"


def test_a_query_is_served_refused_or_unreachable(
    app: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: dict[str, Any] = {}
    answers = iter(
        [
            (200, b"{}"),
            (403, json.dumps({"error": {"code": "APP_NOT_ACTIVE", "stage": "execute"}}).encode()),
            (502, b"not json"),
        ]
    )

    def post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
        sent.update(url=url, headers=headers, body=json.loads(body), timeout=timeout)
        return next(answers)

    monkeypatch.setattr(app, "datagw_url", lambda: "https://datagw.test")
    monkeypatch.setattr(app, "workload_token", lambda audience: "id-token")
    monkeypatch.setattr(app, "post", post)
    assert app.ask_query("SELECT 1") == ("served", None)
    assert sent["url"] == "https://datagw.test/v1/connections/drill-db/query"
    assert sent["headers"]["Authorization"] == "Bearer id-token"
    assert sent["body"] == {"sql": "SELECT 1", "timeout_ms": 30_000}
    assert app.ask_query("SELECT 1") == ("APP_NOT_ACTIVE", "execute")
    assert app.ask_query("SELECT 1") == ("HTTP_502", None)

    def unreachable(*_: object) -> tuple[int, bytes]:
        raise OSError

    monkeypatch.setattr(app, "post", unreachable)
    assert app.ask_query("SELECT 1") == ("UNREACHABLE", None)


def test_the_query_leg_ends_on_the_first_refusal_and_says_if_it_was_running(
    app: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    script = iter([("served", None), ("served", None), ("APP_NOT_ACTIVE", "execute")])
    monkeypatch.setattr(app, "ask_query", lambda sql: next(script))
    leg = app.Leg()
    app.query_leg("r1", leg)
    [line] = lines(capsys)
    assert leg.up.is_set()
    assert leg.done.is_set()
    assert line["drill"]["outcome"] == "APP_NOT_ACTIVE"
    assert line["drill"]["running"] is True

    monkeypatch.setattr(app, "ask_query", lambda sql: ("APP_NOT_ACTIVE", "admission"))
    leg = app.Leg()
    app.query_leg("r2", leg)
    [line] = lines(capsys)
    assert not leg.up.is_set()
    assert leg.done.is_set()
    assert line["drill"]["running"] is False


def test_the_tunnel_is_held_until_the_far_side_closes_it(
    app: ModuleType, proxy: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    fake = proxy(200)
    leg = app.Leg()
    app.tunnel_leg("r1", leg, keepalive=2.0, wrap=lambda sock, host: sock)
    out = capsys.readouterr().out
    [line] = [json.loads(x) for x in out.splitlines()]
    assert leg.up.is_set()
    assert line["drill"]["outcome"] == "closed"
    assert line["drill"]["running"] is True
    assert fake.heard[0].startswith(b"CONNECT api.github.com:443 HTTP/1.1")
    assert b"Proxy-Authorization: Basic " in fake.heard[0]
    assert fake.heard[1].startswith(b"GET /rate_limit")
    assert PASSWORD not in out


def test_a_refused_tunnel_never_ran(
    app: ModuleType, proxy: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    proxy(407)
    leg = app.Leg()
    app.tunnel_leg("r1", leg, wrap=lambda sock, host: sock)
    out = capsys.readouterr().out
    [line] = [json.loads(x) for x in out.splitlines()]
    assert not leg.up.is_set()
    assert leg.done.is_set()
    assert line["drill"]["outcome"] == "proxy_407"
    assert line["drill"]["running"] is False
    assert PASSWORD not in out


def test_a_missing_proxy_is_an_end_not_a_crash(
    app: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    leg = app.Leg()
    app.tunnel_leg("r1", leg)
    [line] = lines(capsys)
    assert leg.done.is_set()
    assert line["drill"]["running"] is False


def test_start_answers_200_once_both_legs_run_and_503_naming_the_one_that_does_not(
    app: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def up(run: str, leg: Any) -> None:
        leg.up.set()

    def fails(run: str, leg: Any) -> None:
        leg.done.set()

    monkeypatch.setattr(app, "query_leg", up)
    monkeypatch.setattr(app, "tunnel_leg", up)
    ok = app.start("r1")
    assert ok.status_code == 200
    assert json.loads(ok.body) == {"query": True, "tunnel": True}

    monkeypatch.setattr(app, "tunnel_leg", fails)
    refused = app.start("r2")
    assert refused.status_code == 503
    assert json.loads(refused.body) == {"query": True, "tunnel": False}


class FakeSocket:
    def __init__(self, fail_after: int) -> None:
        self.fail_after = fail_after
        self.sent: list[str] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        if len(self.sent) == self.fail_after:
            raise WebSocketDisconnect
        self.sent.append(text)


async def test_the_websocket_ticks_and_logs_how_it_ended(
    app: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    monkeypatch.setattr(app, "TICK_SECONDS", 0.0)
    ws = FakeSocket(fail_after=3)
    await app.ticks(ws, run="r1")
    assert ws.accepted
    assert [json.loads(t)["tick"] for t in ws.sent] == [1, 2, 3]
    start, end = lines(capsys)
    assert start["drill"]["event"] == "start"
    assert end["drill"]["event"] == "end"
    assert end["drill"]["outcome"] == "disconnected"
    assert end["drill"]["ticks"] == 4


def test_health_answers_ok(app: ModuleType) -> None:
    assert app.health() == {"ok": True}


async def test_the_plain_answer_sends_a_line_a_second_and_logs_its_end(
    app: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "TICK_SECONDS", 0.0)
    response = await app.drip(run="r1")
    assert response.media_type == "text/plain"
    body = response.body_iterator
    assert [await anext(body) for _ in range(3)] == [b"1\n", b"2\n", b"3\n"]
    await body.aclose()
    capsys.readouterr()
    ends = [ln for ln in lines(capsys) if ln["drill"]["leg"] == "drip"]
    assert ends == [] or ends[-1]["drill"]["event"] == "end"
