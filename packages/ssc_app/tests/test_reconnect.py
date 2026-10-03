"""The reconnect helper (SSC-090) against the shared vectors, and a test app that holds an event
stream and a WebSocket across the limit without the user acting.

The limit is shortened to two seconds: each connection carries the deadline the gateway would
stamp, ``LIMIT`` seconds ahead. The client loops stand in for the browser: they reconnect the way
``EventSource`` does after a ``retry:`` hint, and the way the browser client does after code 1012.
"""

import asyncio
import itertools
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ssc_app.reconnect import (
    DEADLINE_HEADER,
    DEFAULT_MARGIN_SECONDS,
    DEFAULT_RETRY_MS,
    RESTART_CODE,
    browser_client,
    close_before_deadline,
    end_before_deadline,
    seconds_left,
    seconds_to_wait,
)

ROOT = Path(__file__).resolve().parents[3]
VECTORS = json.loads((ROOT / "conformance" / "reconnect" / "vectors.json").read_text())
LIMIT = 2
MARGIN = 0.5
RETRY_MS = 50
TICK = 0.1

app = FastAPI()


@app.get("/events")
async def events(request: Request) -> StreamingResponse:
    start = int(request.headers.get("last-event-id", "-1")) + 1

    async def counter() -> AsyncIterator[str]:
        for n in itertools.count(start):
            yield f"id: {n}\ndata: {n}\n\n"
            await asyncio.sleep(TICK)

    body = end_before_deadline(counter(), request.headers, margin=MARGIN, retry_ms=RETRY_MS)
    return StreamingResponse(body, media_type="text/event-stream")


@app.websocket("/ws")
async def socket(ws: WebSocket, after: int = -1) -> None:
    await ws.accept()
    restart = asyncio.create_task(close_before_deadline(ws.close, ws.headers, margin=MARGIN))
    try:
        for n in itertools.count(after + 1):
            if ws.application_state != WebSocketState.CONNECTED:
                break
            await ws.send_text(str(n))
            await asyncio.sleep(TICK)
    finally:
        restart.cancel()


def stamp() -> dict[str, str]:
    return {DEADLINE_HEADER: str(int(time.time()) + LIMIT)}


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_vector(case: dict[str, object]) -> None:
    headers = case["headers"]
    assert isinstance(headers, dict)
    assert seconds_left(headers, now=VECTORS["now"]) == case["expect"]


@pytest.mark.parametrize("case", VECTORS["waits"], ids=lambda c: c["name"])
def test_wait_vector(case: dict[str, object]) -> None:
    left, margin = case["left"], case["margin"]
    assert left is None or isinstance(left, int)
    assert isinstance(margin, int)
    assert seconds_to_wait(left, margin) == case["expect"]


def test_names_and_defaults_match_the_node_helper() -> None:
    assert {
        "deadline_header": DEADLINE_HEADER,
        "restart_code": RESTART_CODE,
        "margin_seconds": DEFAULT_MARGIN_SECONDS,
        "retry_ms": DEFAULT_RETRY_MS,
    } == VECTORS["constants"]


def test_the_browser_client_is_the_node_helper_s_file() -> None:
    node = ROOT / "helpers" / "node" / "ssc-reconnect" / "browser.js"
    assert browser_client() == node.read_text(encoding="utf-8")


async def test_without_a_deadline_nothing_is_cut() -> None:
    async def three() -> AsyncIterator[str]:
        for n in range(3):
            yield f"data: {n}\n\n"

    got = [chunk async for chunk in end_before_deadline(three(), {})]
    assert got == [b"data: 0\n\n", b"data: 1\n\n", b"data: 2\n\n"]
    closed: list[tuple[int, str]] = []

    async def close(code: int, reason: str) -> None:
        closed.append((code, reason))

    await asyncio.wait_for(close_before_deadline(close, {}), 1)
    passed = {DEADLINE_HEADER: str(int(time.time()) - 5)}
    await asyncio.wait_for(close_before_deadline(close, passed), 1)
    assert closed == []
    got = [chunk async for chunk in end_before_deadline(three(), passed, margin=0)]
    assert got == [b"data: 0\n\n", b"data: 1\n\n", b"data: 2\n\n"]


async def test_inside_the_margin_a_stream_gets_half_the_time_left() -> None:
    headers = stamp()
    left = seconds_left(headers)
    assert left is not None
    closed: list[float] = []

    async def close(code: int, reason: str) -> None:
        assert (code, reason) == (RESTART_CODE, "restart")
        closed.append(time.monotonic())

    start = time.monotonic()
    await asyncio.wait_for(close_before_deadline(close, headers, margin=LIMIT * 10), LIMIT)
    assert len(closed) == 1
    assert left / 2 - TICK <= closed[0] - start < left

    async def ticks() -> AsyncIterator[str]:
        for n in itertools.count():
            yield f"data: {n}\n\n"
            await asyncio.sleep(TICK)

    start = time.monotonic()
    stream = end_before_deadline(ticks(), stamp(), margin=LIMIT * 10, retry_ms=RETRY_MS)
    got = [chunk async for chunk in stream]
    took = time.monotonic() - start
    assert got[-1] == f"retry: {RETRY_MS}\n\n".encode()
    assert len(got) > 2
    assert LIMIT / 4 - TICK <= took < LIMIT


def test_an_event_stream_is_held_across_the_limit() -> None:
    got: list[tuple[int, float]] = []
    ends: list[tuple[float, int, str]] = []
    first: int | None = None
    with TestClient(app) as client:
        while first is None or got[-1][1] <= first + 0.2:
            headers = stamp()
            first = first or int(headers[DEADLINE_HEADER])
            if got:
                headers["last-event-id"] = str(got[-1][0])
            r = client.get("/events", headers=headers)
            at = time.time()
            blocks = r.text.split("\n\n")
            assert blocks[-1] == ""
            for block in blocks[:-2]:
                lines = dict(line.split(": ", 1) for line in block.split("\n"))
                assert lines["id"] == lines["data"]
                got.append((int(lines["data"]), at))
            ends.append((at, int(headers[DEADLINE_HEADER]), blocks[-2]))
            time.sleep(RETRY_MS / 1000)
    assert [n for n, _ in got] == list(range(len(got)))
    assert len(ends) >= 2
    for at, deadline, last in ends:
        assert last == f"retry: {RETRY_MS}"
        assert at < deadline


def test_a_websocket_is_held_across_the_limit() -> None:
    got: list[tuple[int, float]] = []
    closes: list[tuple[int, float, int]] = []
    first: int | None = None
    with TestClient(app) as client:
        while first is None or got[-1][1] <= first + 0.2:
            headers = stamp()
            first = first or int(headers[DEADLINE_HEADER])
            after = got[-1][0] if got else -1
            with client.websocket_connect(f"/ws?after={after}", headers=headers) as ws:
                try:
                    while True:
                        got.append((int(ws.receive_text()), time.time()))
                except WebSocketDisconnect as e:
                    closes.append((e.code, time.time(), int(headers[DEADLINE_HEADER])))
    assert [n for n, _ in got] == list(range(len(got)))
    assert len(closes) >= 2
    for code, at, deadline in closes:
        assert code == RESTART_CODE
        assert at < deadline
