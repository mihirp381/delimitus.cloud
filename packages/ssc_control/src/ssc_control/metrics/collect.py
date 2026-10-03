"""Turn the cell's usage counts into usage events, one whole hour at a time (SSC-028).

For each org (one org is one cell) the collector reads from its cursor, ``usage_collection``,
up to the last whole hour that ended ``LAG`` ago (Cloud Monitoring can take a few minutes to make
a point readable), at most ``MAX_WINDOW_HOURS`` per run, with one call to the cell agent.
Then, in one transaction, it writes:

- ``usage_hour``: per environment and hour, ``instance_seconds``, and ``session_seconds`` for an
  environment whose current release is instance-billed (a session app), else 0;
- ``cold_start``: per environment and minute, ``count`` and mean ``duration_ms``;

and moves the cursor to the end of the window. Each event's ``dedup_key`` is its environment and
window start, so a run repeated, or overlapping another, writes nothing twice. Without a usage
source, or when the cell may not read Cloud Monitoring, it writes nothing and says so in the log.
These numbers are for metrics and the cost view only. Nothing bills from them (A6).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_control.db.bind import bound_org
from ssc_control.db.orgs import all_org_ids
from ssc_control.metrics.events import record_once
from ssc_control.ports import MetricKind
from ssc_control.runtime.specs import release_spec
from ssc_shared.runtime import Billing, billing_for, service_name
from ssc_shared.usage import (
    HOUR,
    MAX_WINDOW_HOURS,
    CellUsage,
    UsageError,
    UsageNotConfiguredError,
    UsageReport,
    UsageWindow,
)

log = logging.getLogger(__name__)

LAG: Final = timedelta(minutes=15)

_CURSOR = text("select collected_until from ssc.usage_collection where org_id = :org")
_ENVIRONMENTS = text(
    "select e.id, e.app_id, d.release_id from ssc.environment e "
    "left join ssc.deployment d on d.org_id = e.org_id and d.id = e.current_deployment_id "
    "where e.org_id = :org order by e.id"
)
_ADVANCE = text(
    "insert into ssc.usage_collection (org_id, collected_until) values (:org, :until) "
    "on conflict (org_id) do update set "
    "collected_until = greatest(ssc.usage_collection.collected_until, excluded.collected_until), "
    "updated_at = now()"
)


@dataclass(frozen=True, slots=True)
class _Environment:
    id: str
    app_id: str
    billing: Billing | None


@dataclass(frozen=True, slots=True)
class Collected:
    """What one org's run did: ``window`` read (None when there was nothing to read) and how
    many events it wrote. ``skipped`` says why nothing was read from the cell."""

    window: UsageWindow | None
    written: int
    skipped: str | None = None


def floor_hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def next_window(cursor: datetime | None, now: datetime) -> UsageWindow | None:
    """The hours to read: from ``cursor`` (or the last whole hour) to the last whole hour that
    ended ``LAG`` before ``now``, at most ``MAX_WINDOW_HOURS``. None when none is due."""
    end = floor_hour(now - LAG)
    start = end - HOUR if cursor is None else cursor.astimezone(UTC)
    if start >= end:
        return None
    return UsageWindow(start=start, end=min(end, start + MAX_WINDOW_HOURS * HOUR))


async def collect_org(
    engine: AsyncEngine,
    usage: CellUsage,
    org_id: str,
    now: datetime,
    cache: dict[UsageWindow, UsageReport] | None = None,
) -> Collected:
    """One run for one org. ``cache`` shares a cell read between orgs asking for one window."""
    async with bound_org(engine, org_id) as conn:
        cursor = (await conn.execute(_CURSOR, {"org": org_id})).scalar_one_or_none()
        window = next_window(cursor, now)
        environments = [] if window is None else await _environments(conn, org_id)
    if window is None:
        return Collected(None, 0)
    if not environments:
        async with bound_org(engine, org_id) as conn:
            await conn.execute(_ADVANCE, {"org": org_id, "until": window.end})
        return Collected(window, 0)
    report = None if cache is None else cache.get(window)
    if report is None:
        try:
            report = await usage.read(window)
        except UsageNotConfiguredError as exc:
            log.warning("usage events skipped: the cell has no usage source (%s)", exc)
            return Collected(None, 0, "not_configured")
        except UsageError as exc:
            log.warning("usage events not read; the next run tries again (%s)", exc)
            return Collected(None, 0, "error")
        if cache is not None:
            cache[window] = report
    async with bound_org(engine, org_id) as conn:
        written = await _write(conn, org_id, environments, report)
        await conn.execute(_ADVANCE, {"org": org_id, "until": window.end})
    return Collected(window, written)


async def collect_all(engine: AsyncEngine, usage: CellUsage, now: datetime) -> int:
    """One run for every org; returns how many events were written. One org's failure is
    logged and the others still run."""
    cache: dict[UsageWindow, UsageReport] = {}
    written = 0
    for org_id in await all_org_ids(engine):
        try:
            done = await collect_org(engine, usage, org_id, now, cache)
        except Exception:
            log.exception("usage collection of %s raised", org_id)
            continue
        if done.skipped == "not_configured":
            return written
        written += done.written
    return written


async def _environments(conn: AsyncConnection, org_id: str) -> list[_Environment]:
    out: list[_Environment] = []
    for env_id, app_id, release_id in (await conn.execute(_ENVIRONMENTS, {"org": org_id})).all():
        spec = None if release_id is None else await release_spec(conn, org_id, release_id)
        billing = None if spec is None else billing_for(spec.manifest.runtime, spec.framework)
        out.append(_Environment(env_id, app_id, billing))
    return out


async def _write(
    conn: AsyncConnection, org_id: str, environments: list[_Environment], report: UsageReport
) -> int:
    by_service = {service_name(e.id): e for e in environments}
    written = 0
    for hour in report.hours:
        env = by_service.get(hour.service)
        if env is None:
            continue
        billing = env.billing or "request"
        session = hour.active_seconds if billing == "instance" else 0
        written += await record_once(
            conn,
            org_id=org_id,
            kind=MetricKind.USAGE_HOUR,
            dedup_key=f"{env.id}:{int(hour.hour.timestamp())}",
            app_id=env.app_id,
            environment_id=env.id,
            properties={
                "instance_seconds": hour.instance_seconds,
                "session_seconds": session,
                "billing": billing,
            },
            at=hour.hour,
        )
    for start in report.cold_starts:
        env = by_service.get(start.service)
        if env is None:
            continue
        written += await record_once(
            conn,
            org_id=org_id,
            kind=MetricKind.COLD_START,
            dedup_key=f"{env.id}:{int(start.minute.timestamp())}",
            app_id=env.app_id,
            environment_id=env.id,
            properties={"count": start.count, "duration_ms": start.duration_ms},
            at=start.minute,
        )
    return written
