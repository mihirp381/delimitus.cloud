"""Usage per environment per month, and the cell's fixed resources, from the usage events
(SSC-028). Read by the API (``ssc status``, the console's cost view SSC-057, the warm-option hint
SSC-092 and the monthly reconciliation SSC-096) and by the metrics report.

Months are UTC calendar months. Cold-start percentiles over fewer than ``NO_RATE_BELOW_N`` cold
starts are not given: ``small_sample`` is true instead. Each ``cold_start`` event counts as
``count`` starts of its mean duration.

The usage type is read after the fact from one month's events, never declared and never used to
provision anything (architecture §10.1: "Rare, daily and heavy are not provisioned
differently"). Thresholds, from the §10.1 bands at one vCPU per instance:

- ``session``: any session hour in the month (a session app someone held open);
- ``heavy``: at least ``HEAVY_MIN_SECONDS`` instance seconds, a tenth of a 730-hour month
  (262,800 vCPU-seconds, the bottom of the heavy band);
- ``rare``: at most ``RARE_MAX_SECONDS`` instance seconds (19,500 vCPU-seconds, the top of the
  rare band: 50 opens a day of 13 seconds for 30 days);
- ``daily``: anything between;
- None: no usage recorded that month.

These numbers are for metrics and the cost view only. Nothing bills from them (A6).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_control.domain.stats import is_reportable, percentile

UsageType = Literal["rare", "daily", "session", "heavy"]
RARE_MAX_SECONDS: Final = 19_500.0
HEAVY_MIN_SECONDS: Final = 262_800.0

_HOURS = text(
    "select environment_id, max(app_id), "
    "coalesce(sum((properties->>'session_seconds')::float8), 0), "
    "coalesce(sum((properties->>'instance_seconds')::float8), 0), "
    "count(distinct (at at time zone 'UTC')::date) "
    "filter (where (properties->>'instance_seconds')::float8 > 0) "
    "from ssc.metrics_event where org_id = :org and kind = 'usage_hour' "
    "and environment_id is not null and at >= :lo and at < :hi "
    "and (cast(:envs as text[]) is null or environment_id = any(cast(:envs as text[]))) "
    "group by environment_id order by environment_id"
)
_COLD = text(
    "select environment_id, max(app_id), "
    "array_agg((properties->>'count')::int order by at), "
    "array_agg((properties->>'duration_ms')::float8 order by at) "
    "from ssc.metrics_event where org_id = :org and kind = 'cold_start' "
    "and environment_id is not null and at >= :lo and at < :hi "
    "and (cast(:envs as text[]) is null or environment_id = any(cast(:envs as text[]))) "
    "group by environment_id"
)
_FIXED = text(
    "select properties->>'resource', at from ssc.metrics_event "
    "where org_id = :org and kind = 'fixed_resource' order by at"
)


@dataclass(frozen=True, slots=True)
class EnvironmentUsage:
    environment_id: str
    app_id: str | None
    session_hours: float
    instance_hours: float
    cold_starts: int
    cold_start_p50_seconds: float | None
    cold_start_p95_seconds: float | None
    small_sample: bool
    active_days: int
    usage_type: UsageType | None


@dataclass(frozen=True, slots=True)
class FixedResource:
    resource: str
    created_at: datetime


def parse_month(value: str) -> date:
    """``YYYY-MM`` as the first day of that month; ``ValueError`` otherwise."""
    if len(value) != len("2026-10") or value[4] != "-":
        raise ValueError("a month is YYYY-MM")
    return date(int(value[:4]), int(value[5:]), 1)


def month_of(moment: datetime) -> date:
    return moment.astimezone(UTC).date().replace(day=1)


def month_span(month: date) -> tuple[datetime, datetime]:
    """The month as UTC instants ``[first day, first day of the next month)``."""
    start = month.replace(day=1)
    end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    return datetime.combine(start, time(), UTC), datetime.combine(end, time(), UTC)


def usage_type(session_seconds: float, instance_seconds: float) -> UsageType | None:
    """The after-the-fact usage type of one environment's month (see the module docstring)."""
    if session_seconds > 0:
        return "session"
    if instance_seconds >= HEAVY_MIN_SECONDS:
        return "heavy"
    if instance_seconds > RARE_MAX_SECONDS:
        return "daily"
    if instance_seconds > 0:
        return "rare"
    return None


async def environment_usage(
    conn: AsyncConnection,
    org_id: str,
    month: date,
    environment_ids: Sequence[str] | None = None,
) -> list[EnvironmentUsage]:
    """Every environment with usage events in ``month``, or only ``environment_ids``."""
    lo, hi = month_span(month)
    params = {
        "org": org_id,
        "lo": lo,
        "hi": hi,
        "envs": None if environment_ids is None else list(environment_ids),
    }
    hours: dict[str, tuple[str | None, float, float, int]] = {
        str(env): (app, float(session), float(instance), int(days))
        for env, app, session, instance, days in (await conn.execute(_HOURS, params)).all()
    }
    cold: dict[str, tuple[str | None, list[float]]] = {}
    for env, app, counts, durations in (await conn.execute(_COLD, params)).all():
        pairs: list[tuple[int, float]] = list(zip(counts, durations, strict=True))
        cold[str(env)] = (app, [float(ms) / 1000 for n, ms in pairs for _ in range(int(n))])
    out: list[EnvironmentUsage] = []
    for env_id in sorted(set(hours) | set(cold)):
        app_h, session, instance, days = hours.get(env_id, (None, 0.0, 0.0, 0))
        app_c, samples = cold.get(env_id, (None, []))
        shown = is_reportable(len(samples))
        out.append(
            EnvironmentUsage(
                environment_id=env_id,
                app_id=app_h or app_c,
                session_hours=round(session / 3600, 2),
                instance_hours=round(instance / 3600, 2),
                cold_starts=len(samples),
                cold_start_p50_seconds=round(percentile(samples, 0.5), 2) if shown else None,
                cold_start_p95_seconds=round(percentile(samples, 0.95), 2) if shown else None,
                small_sample=not shown,
                active_days=days,
                usage_type=usage_type(session, instance),
            )
        )
    return out


async def fixed_resources(conn: AsyncConnection, org_id: str) -> list[FixedResource]:
    """When each of the cell's fixed resources was created, oldest first."""
    rows = (await conn.execute(_FIXED, {"org": org_id})).all()
    return [FixedResource(resource=str(r), created_at=at) for r, at in rows]
