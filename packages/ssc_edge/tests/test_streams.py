"""The stream relay (SSC-021, decision 023): an open WebSocket, event stream or long plain
response is closed once the person it was admitted for would no longer be admitted, and only theirs.

The relay and a stand-in app run in this process on the real clock; the snapshot is swapped the
way a newly read one would be. Envoy in front of the relay is ``test_envoy.py``.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import pytest
from edge_world import (
    ADA,
    BEN,
    FIN,
    HOST,
    NOW,
    PAY,
    PAY_HOST,
    PROD,
    World,
    gnt,
    session,
    snapshot,
)
from fastapi.testclient import TestClient

from ssc_edge.gate import STREAM_HEADER, Allow, Deny, Facts, Gate
from ssc_edge.server import create_app
from ssc_edge.session import Session
from ssc_edge.streams import WATCH_SECONDS, Streams
from ssc_shared.access import AccessView

SWITCH = b"HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\nconnection: upgrade\r\n\r\n"


@dataclass
class App:
    """Answers an upgrade with a WebSocket switch, then echoes; ``/slow`` with a chunked answer
    that never ends; anything else with ``ok``, closing only when asked. Records each head."""

    heads: list[bytes]
    port: int = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        self.heads.append(head)
        if b"upgrade: websocket" in head.lower():
            writer.write(SWITCH)
            while chunk := await reader.read(1024):
                writer.write(chunk)
                await writer.drain()
        elif head.startswith((b"GET /slow ", b"POST /slow ")):
            writer.write(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n")
            with suppress(ConnectionError):
                while True:
                    writer.write(b"4\r\ntick\r\n")
                    await writer.drain()
                    await asyncio.sleep(0.1)
        else:
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
            await writer.drain()
            if b"connection: close" not in head.lower():
                await reader.read()
        writer.close()


@dataclass
class Rig:
    world: World
    gate: Gate
    streams: Streams
    app: App
    port: int
    dialled: list[str]

    def facts(self, s: Session, host: str = HOST, **headers: str) -> Facts:
        cookie = self.world.cookie(s, host)
        ws = {"upgrade": "websocket", "connection": "upgrade", "origin": f"https://{host}"}
        return Facts(
            method="GET", host=host, path="/ws", headers={"cookie": cookie, **ws, **headers}
        )

    async def admitted(self, s: Session, host: str = HOST) -> Allow:
        out = await self.gate.check(self.facts(s, host))
        assert isinstance(out, Allow), out
        return out

    async def send(
        self, allowed: Allow, ticket: str, host: str | None = None
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(
            f"GET /ws HTTP/1.1\r\nhost: {host or allowed.upstream}\r\n{STREAM_HEADER}: {ticket}"
            "\r\nupgrade: websocket\r\nconnection: upgrade\r\n\r\n".encode()
        )
        answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        return reader, writer, answer

    async def plain(
        self, s: Session, line: str, *headers: str, body: bytes = b""
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
        """A plain request through the relay, as Envoy sends one: admitted, with its ticket."""
        allowed = await self.admitted(s)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        extra = "".join(f"{h}\r\n" for h in headers)
        writer.write(
            f"{line} HTTP/1.1\r\nhost: {allowed.upstream}\r\n{STREAM_HEADER}: "
            f"{self.streams.admit(allowed)}\r\n{extra}\r\n".encode()
            + body
        )
        answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        return reader, writer, answer

    async def open(
        self, s: Session, host: str = HOST
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        allowed = await self.admitted(s, host)
        reader, writer, answer = await self.send(allowed, self.streams.admit(allowed))
        assert answer == SWITCH
        assert await echoes(reader, writer)
        return reader, writer


async def echoes(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
    writer.write(b"ping")
    await writer.drain()
    return await asyncio.wait_for(reader.readexactly(4), 2) == b"ping"


async def closed_after(reader: asyncio.StreamReader, seconds: float) -> float:
    """How long until the relay closes the stream, waiting at most ``seconds``."""
    started = time.monotonic()
    try:
        rest = await asyncio.wait_for(reader.read(), seconds)
    except ConnectionError:
        rest = b""
    assert rest == b""
    return time.monotonic() - started


@pytest.fixture
async def rig(world: World) -> AsyncIterator[Rig]:
    app = App(heads=[])
    app_server = await asyncio.start_server(app.handle, "127.0.0.1", 0)
    app.port = app_server.sockets[0].getsockname()[1]
    dialled: list[str] = []

    async def dial(host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        dialled.append(host)
        return await asyncio.open_connection("127.0.0.1", app.port)

    world.now = int(time.time())
    gate = world.gate()
    streams = Streams(lambda: gate, dial=dial)
    relay = await streams.serve("127.0.0.1", 0)
    try:
        yield Rig(world, gate, streams, app, relay.sockets[0].getsockname()[1], dialled)
    finally:
        await streams.aclose()
        relay.close()
        app_server.close()


def swap(world: World, version: int, **changes: Any) -> None:
    world.view = AccessView.from_document(snapshot(version, **changes))


async def test_a_removed_grant_closes_that_person_s_stream_within_one_watch(rig: Rig) -> None:
    w = rig.world
    ada = session(ADA, iat=w.now - 60)
    ben = session(BEN, iat=w.now - 60)
    ada_reader, ada_writer = await rig.open(ada)
    ben_reader, _ = await rig.open(ben)
    swap(w, 2, grants={**snapshot()["grants"], PROD: [snapshot()["grants"][PROD][1]]})
    took = await closed_after(ben_reader, WATCH_SECONDS + 1)
    assert took <= WATCH_SECONDS + 0.5
    assert await echoes(ada_reader, ada_writer)
    refused = await rig.gate.check(rig.facts(ben))
    assert isinstance(refused, Deny) and refused.reason == "not_granted"
    assert rig.streams.open and all(s.allowed.user == ADA for s in rig.streams.open)


async def test_removal_from_a_group_closes_the_stream(rig: Rig) -> None:
    w = rig.world
    fin = {"grant_id": gnt(2), "role": "builder", "subject_kind": "group", "subject_id": FIN}
    by_group = {PROD: [fin]}
    swap(w, 2, grants={**snapshot()["grants"], **by_group})
    reader, _ = await rig.open(session(ADA, iat=w.now - 60))
    swap(w, 3, grants={**snapshot()["grants"], **by_group}, groups_by_user={})
    assert await closed_after(reader, WATCH_SECONDS + 1) <= WATCH_SECONDS + 0.5
    assert not rig.streams.open


@pytest.mark.parametrize("case", ["revoked", "app_stopped", "no_snapshot", "session_over"])
async def test_every_reason_a_check_would_refuse_closes_the_stream(rig: Rig, case: str) -> None:
    w = rig.world
    reader, _ = await rig.open(session(ADA, iat=w.now - 60, life=3600), PAY_HOST)
    if case == "revoked":
        users = {**snapshot()["users"], ADA: {"status": "active", "sessions_not_before": w.now}}
        swap(w, 2, users=users)
    elif case == "app_stopped":
        envs = snapshot()["environments"]
        swap(w, 2, environments={**envs, PAY: {**envs[PAY], "status": "disabled"}})
    elif case == "no_snapshot":
        w.view = None
    else:
        w.now += 3600
    assert await closed_after(reader, WATCH_SECONDS + 1) <= WATCH_SECONDS + 0.5


async def test_a_ticket_opens_one_stream_to_its_own_service_only(rig: Rig) -> None:
    w = rig.world
    allowed = await rig.admitted(session(ADA, iat=w.now - 60))
    for ticket, host in (("made-up", None), (rig.streams.admit(allowed), "evil.example")):
        _, _, answer = await rig.send(allowed, ticket, host)
        assert answer.startswith(b"HTTP/1.1 502 ")
    ticket = rig.streams.admit(allowed)
    reader, writer, answer = await rig.send(allowed, ticket)
    assert answer == SWITCH
    _, _, again = await rig.send(allowed, ticket)
    assert again.startswith(b"HTTP/1.1 502 ")
    assert rig.dialled == [allowed.upstream]
    (head,) = rig.app.heads
    assert STREAM_HEADER.encode() not in head.lower()
    assert head.startswith(b"GET /ws HTTP/1.1\r\n")
    writer.close()


async def test_the_watch_reads_the_snapshot_only_while_a_stream_is_open(world: World) -> None:
    refreshed: list[float] = []

    async def refresh() -> None:
        refreshed.append(time.monotonic())

    app = App(heads=[])
    app_server = await asyncio.start_server(app.handle, "127.0.0.1", 0)
    port = app_server.sockets[0].getsockname()[1]
    world.now = int(time.time())
    gate = world.gate()

    async def dial(_: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", port)

    streams = Streams(lambda: gate, dial=dial, refresh=refresh)
    relay = await streams.serve("127.0.0.1", 0)
    r = Rig(world, gate, streams, app, relay.sockets[0].getsockname()[1], [])
    try:
        _, writer = await r.open(session(ADA, iat=world.now - 60))
        await asyncio.sleep(WATCH_SECONDS * 2.5)
        assert len(refreshed) == 2
        writer.close()
        await asyncio.sleep(WATCH_SECONDS * 2.5)
        assert len(refreshed) <= 3
        assert not streams.open
    finally:
        await streams.aclose()
        relay.close()
        app_server.close()


LONG = {
    "a streaming fetch()": ("GET /slow", ("sec-fetch-dest: empty", "sec-fetch-mode: cors")),
    "a long plain GET": ("GET /slow", ()),
    "a slow POST": ("POST /slow", ("content-length: 2",)),
}


@pytest.mark.parametrize("kind", LONG)
async def test_a_removed_grant_cuts_a_long_plain_response_within_one_watch(
    rig: Rig, kind: str
) -> None:
    w = rig.world
    line, headers = LONG[kind]
    body = b"{}" if line.startswith("POST") else b""
    reader, _, answer = await rig.plain(session(BEN, iat=w.now - 60), line, *headers, body=body)
    assert answer.startswith(b"HTTP/1.1 200 ")
    assert b"tick" in await asyncio.wait_for(reader.read(64), 2)
    swap(w, 2, grants={**snapshot()["grants"], PROD: [snapshot()["grants"][PROD][1]]})
    started = time.monotonic()
    with suppress(ConnectionError):
        while await asyncio.wait_for(reader.read(1024), WATCH_SECONDS + 1):
            pass
    assert time.monotonic() - started <= WATCH_SECONDS + 0.5
    assert not rig.streams.open


async def test_a_short_answer_ends_its_relayed_connection(rig: Rig) -> None:
    reader, _, answer = await rig.plain(
        session(ADA, iat=rig.world.now - 60), "GET /api", "connection: keep-alive"
    )
    assert answer.startswith(b"HTTP/1.1 200 ")
    assert await asyncio.wait_for(reader.read(), 2) == b"ok"
    (head,) = rig.app.heads
    assert b"connection: close" in head.lower() and b"keep-alive" not in head.lower()
    await asyncio.sleep(0.05)
    assert not rig.streams.open


def test_the_check_hands_a_ticket_to_everything_but_pages_and_static_files(world: World) -> None:
    world.now = NOW
    streams = Streams(lambda: None)
    client = TestClient(create_app(world.gate, streams=streams), raise_server_exceptions=False)
    cookie = {"host": HOST, "cookie": world.cookie()}
    ws = {"upgrade": "websocket", "connection": "upgrade", "origin": f"https://{HOST}"}
    for dest in ("document", "iframe", "script", "style", "image", "font", "manifest"):
        page = client.get("/authz/", headers={**cookie, "sec-fetch-dest": dest})
        assert page.status_code == 200 and STREAM_HEADER not in page.headers, dest
    fetch = {"sec-fetch-dest": "empty", "sec-fetch-mode": "cors"}
    for extra in (ws, {"accept": "text/event-stream"}, fetch, {}):
        r = client.get("/authz/ws", headers={**cookie, **extra})
        assert r.status_code == 200 and r.headers[STREAM_HEADER]
    refused = client.get("/authz/ws", headers={**ws, "host": PAY_HOST})
    assert refused.status_code == 403 and STREAM_HEADER not in refused.headers
