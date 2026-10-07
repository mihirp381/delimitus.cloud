"""How much one query may take (SSC-050, C19 steps 4 and 5).

Limits compose by taking the minimum: the platform's ceiling, the connection's caps, the grant's
caps and what the request asks for (or the default when it asks for nothing). A cap a layer
leaves out puts no cap at that layer; 0 is a cap of zero, so a layer can only narrow, never widen
(mined from Delimitus ``spec.limit-composition``). The daily budget and the concurrency slots
are counted per grant, in this instance: with the service's ten instances at most, a grant can
get up to ten times them (``docs/contracts/data-gateway.md``).
"""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime
from typing import Final

from ssc_contracts.snapshot import SnapshotLimits

MB: Final = 1024 * 1024
DEFAULT_MAX_ROWS: Final = 5_000
DEFAULT_MAX_BYTES: Final = 10 * MB
SLOT_WAIT_SECONDS: Final = 2.0


@dataclass(frozen=True, slots=True, kw_only=True)
class Limits:
    max_rows: int
    max_bytes: int
    timeout_ms: int
    concurrency: int
    daily_rows: int
    daily_bytes: int


PLATFORM: Final = Limits(
    max_rows=50_000,
    max_bytes=50 * MB,
    timeout_ms=30_000,
    concurrency=4,
    daily_rows=1_000_000,
    daily_bytes=1024 * MB,
)
NAMES: Final = tuple(f.name for f in fields(Limits))


def _narrow(limits: Limits, layer: SnapshotLimits | None) -> Limits:
    if layer is None:
        return limits
    values = {
        name: min(getattr(limits, name), cap)
        for name in NAMES
        if (cap := getattr(layer, name)) is not None
    }
    return Limits(**{**{n: getattr(limits, n) for n in NAMES}, **values})


def compose(
    *layers: SnapshotLimits | None,
    max_rows: int | None = None,
    max_bytes: int | None = None,
    timeout_ms: int | None = None,
) -> Limits:
    """``PLATFORM`` narrowed by each layer in turn, then by the request's three asks; an ask left
    out is the default for rows and bytes and no narrowing for time."""
    limits = PLATFORM
    for layer in layers:
        limits = _narrow(limits, layer)
    ask = SnapshotLimits(
        max_rows=DEFAULT_MAX_ROWS if max_rows is None else max_rows,
        max_bytes=DEFAULT_MAX_BYTES if max_bytes is None else max_bytes,
        timeout_ms=timeout_ms,
    )
    return _narrow(limits, ask)


class BudgetSpentError(Exception):
    """The grant has served its daily rows or bytes; it may query again after 00:00 UTC."""


class DailyBudget:
    """Rows and bytes each grant has been served today (UTC), in this instance."""

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock
        self._day: date | None = None
        self._used: dict[str, tuple[int, int]] = {}

    def _today(self) -> dict[str, tuple[int, int]]:
        day = self._clock().astimezone(UTC).date()
        if day != self._day:
            self._day, self._used = day, {}
        return self._used

    def remaining(self, grant: str, limits: Limits) -> tuple[int, int]:
        """Rows and bytes the grant may still be served today; :class:`BudgetSpentError` at 0."""
        rows, size = self._today().get(grant, (0, 0))
        left = (limits.daily_rows - rows, limits.daily_bytes - size)
        if min(left) <= 0:
            raise BudgetSpentError(grant)
        return left

    def spend(self, grant: str, rows: int, size: int) -> None:
        used = self._today()
        had_rows, had_size = used.get(grant, (0, 0))
        used[grant] = (had_rows + rows, had_size + size)


class SlotsBusyError(Exception):
    """Every slot of the grant stayed taken for ``SLOT_WAIT_SECONDS``."""


class Slots:
    """At most ``limit`` queries at once per grant; a query waits up to ``wait`` for a slot."""

    def __init__(self, wait: float = SLOT_WAIT_SECONDS) -> None:
        self._wait = wait
        self._used: dict[str, int] = {}
        self._freed = asyncio.Condition()

    def used(self, grant: str) -> int:
        return self._used.get(grant, 0)

    @asynccontextmanager
    async def hold(self, grant: str, limit: int) -> AsyncGenerator[None]:
        if limit <= 0:
            raise SlotsBusyError(grant)
        async with self._freed:
            try:
                async with asyncio.timeout(self._wait):
                    await self._freed.wait_for(lambda: self.used(grant) < limit)
            except TimeoutError as exc:
                raise SlotsBusyError(grant) from exc
            self._used[grant] = self.used(grant) + 1
        try:
            yield
        finally:
            left = self.used(grant) - 1
            if left:
                self._used[grant] = left
            else:
                self._used.pop(grant, None)
            async with self._freed:
                self._freed.notify_all()
