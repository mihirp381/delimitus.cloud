"""Where the warm option would help (SSC-092), from the usage events (SSC-028): an app opened on
most working days whose users keep meeting cold starts.

Over the last ``HINT_DAYS`` days, Monday to Friday in UTC, an environment is ``opened`` on a day
with any instance seconds in its usage hours and meets a cold start on a day with any
``cold_start`` event. It is suggested when it was opened on more than half of the working days
and met a cold start on at least half of the days it was opened. The console reads this only to
suggest; nothing is turned on from it, and nothing bills from it (A6).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

HINT_DAYS: Final = 28
SATURDAY: Final = 5

_DAYS = text(
    "select environment_id, "
    "count(distinct day) filter (where kind = 'usage_hour' and seconds > 0), "
    "count(distinct day) filter (where kind = 'cold_start') "
    "from (select environment_id, kind, (at at time zone 'UTC')::date as day, "
    "coalesce((properties->>'instance_seconds')::float8, 0) as seconds "
    "from ssc.metrics_event where org_id = :org and kind in ('usage_hour', 'cold_start') "
    "and environment_id is not null and at >= :lo and at < :hi) e "
    "where extract(isodow from day) < 6 group by environment_id"
)


@dataclass(frozen=True, slots=True)
class WarmHint:
    opened_days: int
    cold_start_days: int
    working_days: int

    @property
    def suggested(self) -> bool:
        most = self.opened_days * 2 > self.working_days
        keeps_meeting = self.cold_start_days > 0 and self.cold_start_days * 2 >= self.opened_days
        return most and keeps_meeting


def working_days(start: date, end: date) -> int:
    """Monday to Friday in ``[start, end)``."""
    return sum((start + timedelta(days=n)).weekday() < SATURDAY for n in range((end - start).days))


def window(now: datetime) -> tuple[datetime, datetime]:
    """``HINT_DAYS`` UTC days ending with today so far."""
    today = datetime.combine(now.astimezone(UTC).date(), time(), UTC)
    return today - timedelta(days=HINT_DAYS - 1), now


def hint_working_days(now: datetime) -> int:
    """The working days in the window ending ``now``."""
    start = window(now)[0].date()
    return working_days(start, start + timedelta(days=HINT_DAYS))


async def warm_hints(conn: AsyncConnection, org_id: str, now: datetime) -> dict[str, WarmHint]:
    """Every environment with usage in the window, by id."""
    lo, hi = window(now)
    days = hint_working_days(now)
    rows = (await conn.execute(_DAYS, {"org": org_id, "lo": lo, "hi": hi})).all()
    return {str(env): WarmHint(int(opened), int(cold), days) for env, opened, cold in rows}
