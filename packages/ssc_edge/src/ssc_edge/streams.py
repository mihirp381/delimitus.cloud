"""Open streams through the gateway, and cutting them when access goes (SSC-021, decision 023).

Envoy cannot end a stream it has already let through, so a check that allows a WebSocket
upgrade or an event stream (``gate.streaming``) admits it here instead: the answer carries a
one-time ticket in ``X-SSC-Stream``, and Envoy sends that request to this relay on loopback
rather than to the app. The relay redeems the ticket, opens its own connection to the
environment's service, sends the request on without the ticket and copies bytes both ways until
either side closes.

While any stream is open, every ``WATCH_SECONDS`` the relay refreshes the snapshot the way a check
does (``OnDemandView.refresh``) and asks the gate whether each stream would still be admitted
(``Gate.holds``): a removed grant, a revoked session, a stopped app, an expired session or a
snapshot too old to trust closes the stream at both ends. An open stream keeps a request open, so
the instance has CPU for this; with no stream open nothing runs.
"""

import asyncio
import logging
import secrets
import ssl
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

from ssc_edge.gate import STREAM_HEADER, Allow, Gate

log = logging.getLogger(__name__)

WATCH_SECONDS: Final = 1.0
TICKET_SECONDS: Final = 30.0
"""Envoy forwards at once; an unredeemed ticket is dropped after this."""
HEAD_SECONDS: Final = 10.0
HEAD_MAX_BYTES: Final = 64 * 1024
CHUNK: Final = 64 * 1024
CA_BUNDLE: Final = "/etc/ssl/certs/ca-certificates.crt"
REFUSED: Final = b"HTTP/1.1 502 Bad Gateway\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"

type Dial = Callable[[str], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


async def tls_dial(host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """The environment's ``run.app`` service over TLS, the name checked, as Envoy reaches it."""
    context = ssl.create_default_context(cafile=CA_BUNDLE)
    return await asyncio.open_connection(host, 443, ssl=context, server_hostname=host)


@dataclass(eq=False)
class Stream:
    allowed: Allow
    writers: tuple[asyncio.StreamWriter, asyncio.StreamWriter]

    def cut(self) -> None:
        for writer in self.writers:
            writer.close()


class Streams:
    """The tickets, the relay and the watch over open streams. ``gate`` returns None until
    start-up has loaded the keys; ``refresh`` is the snapshot's (None: the gate's view is
    always current, in tests)."""

    def __init__(
        self,
        gate: Callable[[], Gate | None],
        *,
        dial: Dial = tls_dial,
        refresh: Callable[[], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gate = gate
        self._dial = dial
        self._refresh = refresh
        self._monotonic = monotonic
        self._tickets: dict[str, tuple[Allow, float]] = {}
        self.open: set[Stream] = set()
        self._watch: asyncio.Task[None] | None = None

    def admit(self, allowed: Allow) -> str:
        """A one-time ticket for the stream ``allowed`` lets through."""
        now = self._monotonic()
        for key, (_, until) in list(self._tickets.items()):
            if until < now:
                del self._tickets[key]
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = (allowed, now + TICKET_SECONDS)
        return ticket

    def _redeem(self, ticket: str) -> Allow | None:
        found = self._tickets.pop(ticket, None)
        if found is None or found[1] < self._monotonic():
            return None
        return found[0]

    async def serve(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self._relay, host, port, limit=HEAD_MAX_BYTES)

    async def _head(self, reader: asyncio.StreamReader) -> tuple[Allow, bytes] | None:
        """The request head with the ticket taken out, and what the ticket admitted."""
        with suppress(asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_SECONDS)
            first, *lines = raw[:-4].split(b"\r\n")
            ticket, host, kept = None, None, [first]
            for line in lines:
                name, _, value = line.partition(b":")
                key = name.strip().lower()
                if key == STREAM_HEADER.encode():
                    ticket = value.strip().decode("latin-1")
                    continue
                if key == b"host":
                    host = value.strip().decode("latin-1").lower()
                kept.append(line)
            allowed = None if ticket is None else self._redeem(ticket)
            if allowed is None or host != allowed.upstream:
                return None
            return allowed, b"\r\n".join(kept) + b"\r\n\r\n"
        return None

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await self._head(reader)
        upstream: asyncio.StreamWriter | None = None
        try:
            if head is None:
                writer.write(REFUSED)
                return
            allowed, request = head
            try:
                app_reader, upstream = await asyncio.wait_for(
                    self._dial(allowed.upstream), HEAD_SECONDS
                )
            except OSError, TimeoutError:
                log.warning("stream relay could not reach %s", allowed.upstream)
                writer.write(REFUSED)
                return
            upstream.write(request)
            stream = Stream(allowed, (writer, upstream))
            self.open.add(stream)
            self._start_watch()
            try:
                copies = [
                    asyncio.create_task(_copy(reader, upstream)),
                    asyncio.create_task(_copy(app_reader, writer)),
                ]
                await asyncio.wait(copies, return_when=asyncio.FIRST_COMPLETED)
                for copy in copies:
                    copy.cancel()
                await asyncio.gather(*copies, return_exceptions=True)
            finally:
                self.open.discard(stream)
        finally:
            for w in (writer, upstream):
                if w is not None:
                    w.close()
                    with suppress(OSError):
                        await w.wait_closed()

    def _start_watch(self) -> None:
        if self._watch is None or self._watch.done():
            self._watch = asyncio.create_task(self._watching())

    async def _watching(self) -> None:
        while self.open:
            await asyncio.sleep(WATCH_SECONDS)
            await self.sweep()

    async def sweep(self) -> int:
        """One pass of the watch: refresh the snapshot, close every stream that would not be
        admitted now; returns how many."""
        if self._refresh is not None:
            await self._refresh()
        gate = self._gate()
        cut = 0
        for stream in list(self.open):
            if gate is None or not gate.holds(stream.allowed):
                stream.cut()
                self.open.discard(stream)
                cut += 1
        if cut:
            log.info("stream watch closed %s stream(s)", cut)
        return cut

    async def aclose(self) -> None:
        for stream in list(self.open):
            stream.cut()
        if self._watch is not None:
            self._watch.cancel()
            await asyncio.gather(self._watch, return_exceptions=True)


async def _copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    with suppress(OSError):
        while chunk := await src.read(CHUNK):
            dst.write(chunk)
            await dst.drain()
