"""SSC-041: when a schedule runs next, across clock changes, from the pinned zone data.

Ticket "done when" checks:
  * DST: fall back fires a fixed time once, intervals in both hours
                                        -> test_fall_back_fires_a_fixed_time_once,
                                           test_fall_back_fires_intervals_in_both_hours
  * DST: spring forward fires once when the gap ends
                                        -> test_spring_forward_fires_once_when_the_gap_ends
  * missed runs coalesce                -> test_missed_instants_coalesce
"""

from datetime import UTC, datetime, timedelta

import pytest

from ssc_control.domain.schedule_time import UnknownZoneError, next_after, zone


def z(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def fires(cron: str, tz: str, start: datetime, end: datetime) -> list[datetime]:
    out: list[datetime] = []
    at = start
    while (at := next_after(cron, tz, at)) < end:
        out.append(at)
    return out


def test_next_after_is_strictly_later_and_in_utc() -> None:
    at = next_after("0 9 * * *", "Europe/London", z(2026, 7, 1, 8, 0))  # 09:00 BST
    assert at == z(2026, 7, 2, 8, 0) and at.tzinfo is UTC
    assert next_after("0 9 * * *", "Europe/London", z(2026, 7, 1, 7, 59)) == z(2026, 7, 1, 8, 0)
    assert next_after("*/5 * * * *", "UTC", z(2026, 7, 1, 8, 0, 30)) == z(2026, 7, 1, 8, 5)


def test_fall_back_fires_a_fixed_time_once() -> None:
    # 2026-11-01: New York's 01:00-02:00 happens twice (EDT 05:00Z-06:00Z, then EST 06:00Z-07:00Z).
    day = fires("30 1 * * *", "America/New_York", z(2026, 10, 31, 12), z(2026, 11, 2, 12))
    assert day == [z(2026, 11, 1, 5, 30), z(2026, 11, 2, 6, 30)]
    # Resuming inside the repeated hour does not fire the second 01:30.
    assert next_after("30 1 * * *", "America/New_York", z(2026, 11, 1, 6, 10)) == z(
        2026, 11, 2, 6, 30
    )
    london = fires("30 1 * * *", "Europe/London", z(2026, 10, 24, 12), z(2026, 10, 26, 12))
    assert london == [z(2026, 10, 25, 0, 30), z(2026, 10, 26, 1, 30)]


def test_fall_back_fires_intervals_in_both_hours() -> None:
    hourly = fires("0 * * * *", "America/New_York", z(2026, 11, 1, 4, 30), z(2026, 11, 1, 6, 30))
    assert hourly == [z(2026, 11, 1, 5), z(2026, 11, 1, 6)]
    quarter = fires("*/15 1 * * *", "America/New_York", z(2026, 11, 1), z(2026, 11, 1, 12))
    assert quarter == [z(2026, 11, 1, 5) + timedelta(minutes=15 * i) for i in range(8)]


def test_spring_forward_fires_once_when_the_gap_ends() -> None:
    # 2026-03-08: New York skips 02:00-03:00; 02:30 does not exist that day.
    day = fires("30 2 * * *", "America/New_York", z(2026, 3, 7, 12), z(2026, 3, 9, 12))
    assert day == [z(2026, 3, 8, 7, 0), z(2026, 3, 9, 6, 30)]


def test_missed_instants_coalesce() -> None:
    # A worker down from 00:00 to 03:07 claims the 01:00 instant once, then arms 04:00.
    last, now = z(2026, 7, 1, 1), z(2026, 7, 1, 3, 7)
    assert next_after("0 * * * *", "UTC", max(last, now)) == z(2026, 7, 1, 4)


def test_cron_forms() -> None:
    assert next_after("0 0 * * 7", "UTC", z(2026, 9, 29)) == z(2026, 10, 4)  # 7 is Sunday
    assert next_after("0 9 * * MON-FRI", "UTC", z(2026, 10, 2, 10)) == z(2026, 10, 5, 9)
    assert next_after("0 0 29 2 *", "UTC", z(2026, 9, 29)) == z(2028, 2, 29)


def test_zones_come_from_the_pinned_data() -> None:
    assert zone("UTC").key == "Etc/UTC"
    assert zone("Asia/Kolkata") is zone("Asia/Kolkata")
    with pytest.raises(UnknownZoneError):
        zone("Mars/Olympus_Mons")
    with pytest.raises(UnknownZoneError):
        zone("../../etc/passwd")


def test_after_must_be_aware() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        next_after("* * * * *", "UTC", datetime(2026, 1, 1))
