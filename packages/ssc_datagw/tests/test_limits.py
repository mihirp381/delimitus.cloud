"""Limit composition, the daily budget and the concurrency slots (SSC-050)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ssc_contracts.snapshot import SnapshotLimits
from ssc_datagw.limits import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ROWS,
    NAMES,
    PLATFORM,
    BudgetSpentError,
    DailyBudget,
    Limits,
    Slots,
    SlotsBusyError,
    compose,
)

caps = st.one_of(st.none(), st.integers(min_value=0, max_value=2 * PLATFORM.daily_bytes))
layers = st.builds(SnapshotLimits, **dict.fromkeys(NAMES, caps))
asks = st.one_of(st.none(), st.integers(min_value=0, max_value=10**9))


def test_nothing_set_is_the_platform_with_the_default_asks() -> None:
    assert compose(None, SnapshotLimits()) == Limits(
        max_rows=DEFAULT_MAX_ROWS,
        max_bytes=DEFAULT_MAX_BYTES,
        timeout_ms=PLATFORM.timeout_ms,
        concurrency=PLATFORM.concurrency,
        daily_rows=PLATFORM.daily_rows,
        daily_bytes=PLATFORM.daily_bytes,
    )


def test_a_request_asks_up_to_the_platform_ceiling_and_no_further() -> None:
    assert compose(max_rows=20_000).max_rows == 20_000
    assert compose(max_rows=10**9, max_bytes=10**12).max_rows == PLATFORM.max_rows
    assert compose(max_bytes=10**12).max_bytes == PLATFORM.max_bytes


def test_absent_is_no_cap_and_zero_is_a_cap_of_zero() -> None:
    assert compose(SnapshotLimits(max_rows=None), max_rows=100).max_rows == 100
    assert compose(SnapshotLimits(max_rows=0), max_rows=100).max_rows == 0
    assert compose(SnapshotLimits(concurrency=0)).concurrency == 0


def test_the_connection_and_the_grant_each_narrow() -> None:
    limits = compose(
        SnapshotLimits(max_rows=1000, timeout_ms=5000),
        SnapshotLimits(max_rows=2000, daily_rows=10),
        max_rows=50_000,
    )
    assert (limits.max_rows, limits.timeout_ms, limits.daily_rows) == (1000, 5000, 10)


@given(connection=layers, grant=layers, rows=asks, size=asks, timeout=asks)
def test_composition_never_widens(
    connection: SnapshotLimits,
    grant: SnapshotLimits,
    rows: int | None,
    size: int | None,
    timeout: int | None,
) -> None:
    got = compose(connection, grant, max_rows=rows, max_bytes=size, timeout_ms=timeout)
    for name in NAMES:
        value = getattr(got, name)
        assert value <= getattr(PLATFORM, name)
        for layer in (connection, grant):
            cap = getattr(layer, name)
            assert cap is None or value <= cap
    assert got.max_rows <= (DEFAULT_MAX_ROWS if rows is None else rows)
    assert got.max_bytes <= (DEFAULT_MAX_BYTES if size is None else size)
    assert timeout is None or got.timeout_ms <= timeout


def test_the_budget_refuses_at_its_limit_and_resets_at_midnight_utc() -> None:
    now = [datetime(2026, 10, 3, 23, 59, tzinfo=UTC)]
    budget = DailyBudget(lambda: now[0])
    limits = compose(SnapshotLimits(daily_rows=10, daily_bytes=1000))
    assert budget.remaining("g", limits) == (10, 1000)
    budget.spend("g", 4, 100)
    assert budget.remaining("g", limits) == (6, 900)
    assert budget.remaining("other", limits) == (10, 1000)
    budget.spend("g", 6, 100)
    with pytest.raises(BudgetSpentError):
        budget.remaining("g", limits)
    now[0] += timedelta(minutes=2)
    assert budget.remaining("g", limits) == (10, 1000)


async def test_a_grant_runs_at_most_its_concurrency_and_a_waiter_gets_a_freed_slot() -> None:
    slots = Slots(wait=1.0)
    release = asyncio.Event()

    async def hold() -> None:
        async with slots.hold("g", 2):
            await release.wait()

    holders = [asyncio.create_task(hold()) for _ in range(2)]
    await asyncio.sleep(0)
    assert slots.used("g") == 2
    async with slots.hold("other", 2):
        pass
    waiter = asyncio.create_task(hold())
    await asyncio.sleep(0.05)
    assert not waiter.done()
    release.set()
    await asyncio.gather(*holders, waiter)
    assert slots.used("g") == 0


async def test_no_free_slot_within_the_wait_is_busy() -> None:
    slots = Slots(wait=0.05)
    async with slots.hold("g", 1):
        with pytest.raises(SlotsBusyError):
            async with slots.hold("g", 1):
                pass
    with pytest.raises(SlotsBusyError):
        async with slots.hold("g", 0):
            pass


async def test_a_cancelled_holder_frees_its_slot() -> None:
    slots = Slots(wait=0.05)

    async def hold() -> None:
        async with slots.hold("g", 1):
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert slots.used("g") == 0
