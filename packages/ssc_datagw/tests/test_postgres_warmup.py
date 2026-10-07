"""A new instance tries a timed-out connect again for its first minute (``postgres._connect``)."""

from typing import Any

import pytest

from ssc_datagw import postgres
from ssc_datagw.connectors import UpstreamUnavailableError
from ssc_datagw.postgres import (
    WARMUP_CONNECT_SECONDS,
    WARMUP_PAUSE_SECONDS,
    WARMUP_SECONDS,
    PostgresConnector,
    PostgresTarget,
    Warmup,
)

TARGET = PostgresTarget(
    host="db.example.com",
    database="sales",
    user="ssc_read",
    password="x",  # pyright: ignore[reportArgumentType]
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def connector(clock: Clock, monkeypatch: pytest.MonkeyPatch, *outcomes: Any) -> list[float]:
    timeouts: list[float] = []
    left = list(outcomes)

    async def connect(**kwargs: Any) -> Any:
        timeouts.append(kwargs["timeout"])
        clock.now += kwargs["timeout"] if left[0] is TimeoutError else 0.1
        outcome = left.pop(0)
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(postgres, "_CONNECT", connect)
    return timeouts


async def test_a_new_instance_tries_a_timed_out_connect_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    timeouts = connector(clock, monkeypatch, TimeoutError, TimeoutError, TimeoutError, "conn")
    c = PostgresConnector(TARGET, warmup=Warmup(0.0, clock, clock.sleep))
    assert await c._connect() == "conn"  # pyright: ignore[reportPrivateUsage]
    assert timeouts == [WARMUP_CONNECT_SECONDS] * 4
    assert clock.now == pytest.approx(3 * (WARMUP_CONNECT_SECONDS + WARMUP_PAUSE_SECONDS) + 0.1)


async def test_after_warm_up_a_time_out_is_unavailable_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    clock.now = WARMUP_SECONDS
    timeouts = connector(clock, monkeypatch, TimeoutError, "conn")
    c = PostgresConnector(TARGET, warmup=Warmup(0.0, clock, clock.sleep))
    with pytest.raises(UpstreamUnavailableError, match="TimeoutError"):
        await c._connect()  # pyright: ignore[reportPrivateUsage]
    assert timeouts == [postgres.CONNECT_SECONDS]


async def test_warm_up_stops_retrying_once_it_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    timeouts = connector(clock, monkeypatch, *[TimeoutError] * 12)
    c = PostgresConnector(TARGET, warmup=Warmup(0.0, clock, clock.sleep))
    with pytest.raises(UpstreamUnavailableError):
        await c._connect()  # pyright: ignore[reportPrivateUsage]
    assert timeouts[-1] == postgres.CONNECT_SECONDS
    assert all(t == WARMUP_CONNECT_SECONDS for t in timeouts[:-1])


@pytest.mark.parametrize("error", [ConnectionRefusedError, OSError])
async def test_a_new_instance_does_not_retry_other_failures(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    clock = Clock()
    timeouts = connector(clock, monkeypatch, error, "conn")
    c = PostgresConnector(TARGET, warmup=Warmup(0.0, clock, clock.sleep))
    with pytest.raises(UpstreamUnavailableError, match=error.__name__):
        await c._connect()  # pyright: ignore[reportPrivateUsage]
    assert len(timeouts) == 1
