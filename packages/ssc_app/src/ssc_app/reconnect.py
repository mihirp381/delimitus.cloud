"""Keep a WebSocket or an event stream going past the gateway's limit (SSC-090).

Cloud Run ends every request at a fixed time, and an open WebSocket or event stream is one
request: after 5 minutes for most apps, 60 for a session app. The gateway tells the app that time
in ``X-SSC-Request-Deadline`` (Unix seconds). The
server cannot reconnect a browser, so these helpers end the stream cleanly a little before that
time and the browser opens a new one, which gets a new deadline:

* An event stream: wrap the events in :func:`end_before_deadline`. It ends the stream with a
  ``retry:`` hint and the browser's ``EventSource`` reconnects by itself, sending
  ``Last-Event-ID`` when the events carry an ``id:``.
* A WebSocket: run :func:`close_before_deadline` beside the socket. It closes the socket with
  :data:`RESTART_CODE`; the browser client (:func:`browser_client`) reconnects on that close and
  on an unclean drop.

    return StreamingResponse(end_before_deadline(events(), request.headers),
                             media_type="text/event-stream")

    await socket.accept()
    restart = asyncio.create_task(close_before_deadline(socket.close, socket.headers))
    try:
        ...  # the socket's own loop
    finally:
        restart.cancel()

Without the header, as on a laptop, nothing is cut. A stream that opens with less than the margin
left is ended after half the time left (:func:`seconds_to_wait`), never at once. Whatever must
outlive one connection belongs in Postgres or in the browser, never in the connection. Same names,
defaults and vectors as the Node helper ``@delimitus/ssc-reconnect``.
"""

import asyncio
import time
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from importlib.resources import files
from typing import Final

DEADLINE_HEADER: Final = "X-SSC-Request-Deadline"
RESTART_CODE: Final = 1012
DEFAULT_MARGIN_SECONDS: Final = 30.0
DEFAULT_RETRY_MS: Final = 1000


def seconds_left(headers: Mapping[str, str], *, now: float | None = None) -> float | None:
    """Seconds until the gateway's limit ends this request, never below 0; None when the request
    carries no readable ``X-SSC-Request-Deadline``. ``now`` (Unix seconds) is for tests."""
    wanted = DEADLINE_HEADER.lower()
    for name, value in headers.items():
        if name.lower() == wanted:
            raw = value.strip()
            if not (raw.isascii() and raw.isdigit()):
                return None
            return max(0.0, int(raw) - (time.time() if now is None else now))
    return None


def seconds_to_wait(left: float | None, margin: float = DEFAULT_MARGIN_SECONDS) -> float | None:
    """How long to keep a stream with ``left`` seconds to its deadline: until ``margin`` seconds
    before it, or half of ``left`` when that is later, so a stream that opens inside the margin is
    not ended at once and reopened in a loop. None, cut nothing, without a deadline or once it has
    passed (this clock is ahead of the gateway's); the platform's limit ends that stream."""
    if left is None or left <= 0:
        return None
    return max(left - margin, left / 2)


async def end_before_deadline(
    events: AsyncIterable[str | bytes],
    headers: Mapping[str, str],
    *,
    margin: float = DEFAULT_MARGIN_SECONDS,
    retry_ms: int = DEFAULT_RETRY_MS,
) -> AsyncIterator[bytes]:
    """``events``, each a whole event ending in a blank line, ended :func:`seconds_to_wait` from
    now with ``retry: <retry_ms>`` so ``EventSource`` reconnects that many milliseconds later.
    Without a deadline, ``events`` unchanged."""
    wait = seconds_to_wait(seconds_left(headers), margin)
    loop = asyncio.get_running_loop()
    stop = None if wait is None else loop.time() + wait
    source = aiter(events)
    while True:
        try:
            wait = None if stop is None else max(0.0, stop - loop.time())
            event = await asyncio.wait_for(anext(source), wait)
        except StopAsyncIteration:
            return
        except TimeoutError:
            yield f"retry: {retry_ms}\n\n".encode()
            return
        yield event.encode() if isinstance(event, str) else event


async def close_before_deadline(
    close: Callable[[int, str], Awaitable[object]],
    headers: Mapping[str, str],
    *,
    margin: float = DEFAULT_MARGIN_SECONDS,
) -> None:
    """Wait :func:`seconds_to_wait`, then ``close(RESTART_CODE, "restart")``. Run it as a task
    beside the socket and cancel it when the socket ends first. Without a deadline it returns at
    once and closes nothing."""
    wait = seconds_to_wait(seconds_left(headers), margin)
    if wait is None:
        return
    await asyncio.sleep(wait)
    await close(RESTART_CODE, "restart")


def browser_client() -> str:
    """The browser side of :func:`close_before_deadline`, dependency-free JavaScript to serve or
    inline in a page. It defines ``sscSocket(url, options)``, a WebSocket that reconnects."""
    return files("ssc_app").joinpath("reconnect.js").read_text(encoding="utf-8")
