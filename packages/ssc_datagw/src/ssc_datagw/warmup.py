"""A new instance's first connects may not get through (SSC-051, ``spikes/proofrun`` T6).

A new instance's outbound calls through Cloud NAT may not connect for its first 20 to 37 s. For
:data:`WARMUP_SECONDS` after the process starts, a connect that times out is tried again, each
attempt cut to :data:`WARMUP_CONNECT_SECONDS`, until it connects or the gateway's deadline
cancels the read. Any other failure, and a time-out after warm-up, is
``UpstreamUnavailableError`` at once. Every connector that opens a TCP connection shares this.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Final

from ssc_datagw.connectors import UpstreamUnavailableError

log = logging.getLogger(__name__)

CONNECT_SECONDS: Final = 10.0
WARMUP_SECONDS: Final = 60.0
WARMUP_CONNECT_SECONDS: Final = 5.0
WARMUP_PAUSE_SECONDS: Final = 1.0
PROCESS_STARTED: Final = monotonic()


@dataclass(frozen=True, slots=True)
class Warmup:
    """When the process started, and the clock and pause the warm-up retry uses."""

    started: float = PROCESS_STARTED
    clock: Callable[[], float] = monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def warming(self) -> bool:
        return self.clock() - self.started < WARMUP_SECONDS


async def connect_with_warmup[T](
    connect_once: Callable[[float], Awaitable[T]],
    *,
    connect_seconds: float = CONNECT_SECONDS,
    warmup: Warmup | None = None,
) -> T:
    """``connect_once(seconds)`` until it returns. It raises ``TimeoutError`` when the connect
    timed out and ``UpstreamUnavailableError`` for every other failure."""
    warm = warmup or Warmup()
    while True:
        warming = warm.warming()
        seconds = min(connect_seconds, WARMUP_CONNECT_SECONDS) if warming else connect_seconds
        try:
            return await connect_once(seconds)
        except TimeoutError:
            if not warming:
                raise UpstreamUnavailableError("cannot connect: TimeoutError") from None
            log.info("connect timed out while the instance is new; trying again")
        await warm.sleep(WARMUP_PAUSE_SECONDS)
