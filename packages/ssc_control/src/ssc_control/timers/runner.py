"""One timer run, and the sweep (SSC-041, decision 020).

``run_timer`` claims, dispatches and records one run in three steps:

1. Claim, in one org-bound transaction holding the schedule's row lock. A ``running`` row past
   its deadline (``timeout_seconds`` plus a minute) is closed as ``timed_out``/``abandoned``.
   A scheduled run is stale, and does nothing, unless the schedule is ``active`` and armed for
   exactly this instant. It is then ``skipped`` (``owner_inactive``, ``builder_access_revoked``)
   and the schedule paused when the app's owner or its declarer has lost authority; ``skipped``
   (``app_inactive``) while the app is not active; ``skipped`` (``overlap``) while another run is
   running; otherwise ``running``. Every scheduled claim that is not paused arms the next instant
   after the later of this one and now, so missed instants coalesce into one late run. A manual
   run is stale unless its row is ``queued``; it is ``skipped`` (``deleted``, ``app_inactive``,
   ``owner_inactive``, ``builder_access_revoked`` for the requester, ``overlap``) or started.
2. Dispatch outside any transaction, once, under ``asyncio.timeout(timeout_seconds)``. No
   dispatcher configured fails the run (``dispatch_unavailable``).
3. Record the outcome and a ``timer_run`` metrics event in a second transaction.

Running the task twice for one instant or one manual run dispatches once: the second claim is
stale, and ``timer_run_once_per_instant`` backs that up.

``sweep_org`` is the periodic safety net: it closes abandoned runs, drops manual runs queued for
over an hour, pauses schedules that lost authority, and re-defers any armed instant more than a
minute overdue (a lost job, a claim that ran out of retries).
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts.ids import new_id
from ssc_control.db.bind import bound_org
from ssc_control.domain.schedule_time import next_after
from ssc_control.ports import MetricKind, MetricsPort
from ssc_control.timers.dispatch import DispatchResult, ScheduleDispatcher, TimerCall
from ssc_control.timers.service import (
    Blocker,
    may_build,
    pause_blocked,
    schedule_actor,
    set_state,
)
from ssc_control.timers.tasks import defer_scheduled_run

log = logging.getLogger(__name__)

RUN_SLACK: Final = timedelta(minutes=1)
"""How long past its timeout a ``running`` row may stay before it counts as abandoned."""
QUEUED_TTL: Final = timedelta(hours=1)
"""How long a manual run may wait for the worker before it is dropped."""
REARM_GRACE: Final = timedelta(minutes=1)
"""How overdue an armed instant must be before the sweep defers it again."""

Outcome = Literal["stale", "skipped", "succeeded", "failed", "timed_out"]
RunError = Literal[
    "overlap",
    "app_inactive",
    "owner_inactive",
    "builder_access_revoked",
    "deleted",
    "dispatch_unavailable",
    "dispatch_error",
    "http_error",
    "timeout",
]

_SCHEDULE = text(
    "select s.id, s.environment_id, s.name, s.cron, s.timezone, s.path, s.method, "  # noqa: S608  (constant SQL fragments)
    "s.timeout_seconds, s.state, s.pause_reason, s.next_run_at, s.declared_by_user_id, "
    "e.name as environment_name, a.id as app_id, a.status as app_status, "
    f"o.status = 'active' as owner_active, {may_build('d')} as declarer_may_build "
    "from ssc.schedule s "
    "join ssc.environment e on e.org_id = s.org_id and e.id = s.environment_id "
    "join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "join ssc.user_account o on o.org_id = a.org_id and o.id = a.owner_user_id "
    "join ssc.user_account d on d.org_id = s.org_id and d.id = s.declared_by_user_id "
    "where s.org_id = :org and s.id = :id for update of s"
)
_REQUESTER_MAY_BUILD = text(
    f"select {may_build('r')} from ssc.environment e "  # noqa: S608  (constant SQL fragments)
    "join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "join ssc.user_account r on r.org_id = e.org_id and r.id = :by "
    "where e.org_id = :org and e.id = :env"
)
_ABANDON_RUNNING = text(
    "update ssc.timer_run t set state = 'timed_out', error = 'abandoned', finished_at = :now "
    "from ssc.schedule s where t.org_id = :org and s.org_id = t.org_id and s.id = t.schedule_id "
    "and t.state = 'running' and (cast(:only as text) is null or s.id = cast(:only as text)) "
    "and t.started_at + make_interval(secs => s.timeout_seconds + :slack) < :now"
)
_ABANDON_QUEUED = text(
    "update ssc.timer_run set state = 'skipped', error = 'abandoned', finished_at = :now "
    "where org_id = :org and state = 'queued' and scheduled_for < :cutoff"
)
_RUNNING = text(
    "select 1 from ssc.timer_run where org_id = :org and schedule_id = :id and state = 'running'"
)
_INSERT_RUN = text(
    "insert into ssc.timer_run (id, org_id, schedule_id, trigger, scheduled_for, state, error, "
    "started_at, finished_at) values (:id, :org, :sch, 'schedule', :at, :state, :error, "
    ":started, :finished)"
)
_REARM = text(
    "update ssc.schedule set next_run_at = :next, last_scheduled_for = :at "
    "where org_id = :org and id = :id"
)
_LOCK_MANUAL = text(
    "select state, requested_by_user_id from ssc.timer_run "
    "where org_id = :org and id = :id and schedule_id = :sch and trigger = 'manual' for update"
)
_START_MANUAL = text(
    "update ssc.timer_run set state = 'running', started_at = :now where org_id = :org and id = :id"
)
_SKIP_MANUAL = text(
    "update ssc.timer_run set state = 'skipped', error = :error, finished_at = :now "
    "where org_id = :org and id = :id"
)
_FINISH = text(
    "update ssc.timer_run set state = :state, error = :error, http_status = :status, "
    "duration_ms = :ms, finished_at = :now where org_id = :org and id = :id and state = 'running'"
)
_OVERDUE = text(
    "select id, next_run_at from ssc.schedule where org_id = :org and state = 'active' "
    "and next_run_at <= :cutoff order by next_run_at, id"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunDeps:
    engine: AsyncEngine
    dispatcher: ScheduleDispatcher | None
    metrics: MetricsPort
    clock: Callable[[], datetime]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Claim:
    run_id: str
    trigger: Literal["schedule", "manual"]
    call: TimerCall
    timeout_seconds: int
    app_id: str
    environment_name: str
    requested_by_user_id: str | None


async def _claim_scheduled(
    conn: AsyncConnection, s: RowMapping, *, org_id: str, instant: datetime, now: datetime
) -> _Claim | Outcome:
    if s["state"] != "active" or s["next_run_at"] != instant:
        return "stale"
    run_id = new_id("tmr")
    row: dict[str, Any] = {"id": run_id, "org": org_id, "sch": s["id"], "at": instant}
    blocker: Blocker | None = None
    error: RunError | None
    if not s["owner_active"]:
        blocker, error = "owner_deactivated", "owner_inactive"
    elif not s["declarer_may_build"]:
        blocker, error = "builder_access_revoked", "builder_access_revoked"
    elif s["app_status"] != "active":
        error = "app_inactive"
    elif (await conn.execute(_RUNNING, {"org": org_id, "id": s["id"]})).first() is not None:
        error = "overlap"
    else:
        error = None
    if blocker is not None:
        await set_state(
            conn, org_id=org_id, row=s, reason=blocker, actor=schedule_actor(s["id"]), now=now
        )
    else:
        armed = next_after(s["cron"], s["timezone"], max(instant, now))
        await conn.execute(_REARM, {"org": org_id, "id": s["id"], "next": armed, "at": instant})
        await defer_scheduled_run(conn, org_id=org_id, schedule_id=s["id"], instant=armed)
    if error is not None:
        await conn.execute(
            _INSERT_RUN,
            {**row, "state": "skipped", "error": error, "started": None, "finished": now},
        )
        return "skipped"
    await conn.execute(
        _INSERT_RUN, {**row, "state": "running", "error": None, "started": now, "finished": None}
    )
    return _started(s, run_id, org_id=org_id, trigger="schedule", requested_by=None)


async def _claim_manual(
    conn: AsyncConnection, s: RowMapping, *, org_id: str, run_id: str, now: datetime
) -> _Claim | Outcome:
    params = {"org": org_id, "id": run_id, "sch": s["id"]}
    run = (await conn.execute(_LOCK_MANUAL, params)).mappings().first()
    if run is None or run["state"] != "queued":
        return "stale"
    requested_by = str(run["requested_by_user_id"])
    by = {"org": org_id, "env": s["environment_id"], "by": requested_by}
    if s["state"] == "deleted":
        error: RunError | None = "deleted"
    elif s["app_status"] != "active":
        error = "app_inactive"
    elif not s["owner_active"]:
        error = "owner_inactive"
    elif not (await conn.execute(_REQUESTER_MAY_BUILD, by)).scalar_one_or_none():
        error = "builder_access_revoked"
    elif (await conn.execute(_RUNNING, {"org": org_id, "id": s["id"]})).first() is not None:
        error = "overlap"
    else:
        error = None
    if error is not None:
        await conn.execute(_SKIP_MANUAL, {**params, "error": error, "now": now})
        return "skipped"
    await conn.execute(_START_MANUAL, {**params, "now": now})
    return _started(s, run_id, org_id=org_id, trigger="manual", requested_by=requested_by)


def _started(
    s: RowMapping,
    run_id: str,
    *,
    org_id: str,
    trigger: Literal["schedule", "manual"],
    requested_by: str | None,
) -> _Claim:
    call = TimerCall(
        org_id=org_id,
        environment_id=s["environment_id"],
        schedule_id=s["id"],
        run_id=run_id,
        method=s["method"],
        path=s["path"],
    )
    return _Claim(
        run_id=run_id,
        trigger=trigger,
        call=call,
        timeout_seconds=int(s["timeout_seconds"]),
        app_id=s["app_id"],
        environment_name=s["environment_name"],
        requested_by_user_id=requested_by,
    )


async def _dispatch(
    dispatcher: ScheduleDispatcher | None, claim: _Claim
) -> tuple[Outcome, RunError | None, int | None]:
    if dispatcher is None:
        return "failed", "dispatch_unavailable", None
    budget = asyncio.timeout(claim.timeout_seconds)
    try:
        async with budget:
            result = await dispatcher.dispatch(claim.call)
    except TimeoutError:
        if budget.expired():
            return "timed_out", "timeout", None
        log.warning("timer dispatch timed out on its own", extra={"run_id": claim.run_id})
        result = DispatchResult(error="dispatch_error")
    except Exception:
        log.exception("timer dispatch failed", extra={"run_id": claim.run_id})
        result = DispatchResult(error="dispatch_error")
    return _answer(result)


def _answer(result: DispatchResult) -> tuple[Outcome, RunError | None, int | None]:
    status = result.http_status
    if result.error is not None or status is None:
        return "failed", "dispatch_error", status
    if 200 <= status <= 299:
        return "succeeded", None, status
    return "failed", "http_error", status


async def run_timer(  # noqa: PLR0913  (keyword-only)
    deps: RunDeps,
    *,
    org_id: str,
    schedule_id: str,
    scheduled_for: str | None = None,
    run_id: str | None = None,
) -> Outcome:
    """One run: the instant ``scheduled_for`` (ISO 8601) of ``schedule_id``, or its queued
    manual run ``run_id``. Returns what became of it."""
    if scheduled_for is not None and run_id is not None:
        raise ValueError("a timer run is either scheduled_for an instant or a manual run_id")
    async with bound_org(deps.engine, org_id) as conn:
        now = deps.clock()
        params = {"org": org_id, "only": schedule_id, "now": now, "slack": _seconds(RUN_SLACK)}
        await conn.execute(_ABANDON_RUNNING, params)
        s = (await conn.execute(_SCHEDULE, {"org": org_id, "id": schedule_id})).mappings().first()
        if s is None:
            return "stale"
        if scheduled_for is not None:
            instant = datetime.fromisoformat(scheduled_for).astimezone(UTC)
            claim = await _claim_scheduled(conn, s, org_id=org_id, instant=instant, now=now)
        elif run_id is not None:
            claim = await _claim_manual(conn, s, org_id=org_id, run_id=run_id, now=now)
        else:
            raise ValueError("a timer run needs scheduled_for or run_id")
    if not isinstance(claim, _Claim):
        return claim
    started = time.monotonic()
    outcome, error, status = await _dispatch(deps.dispatcher, claim)
    duration_ms = round((time.monotonic() - started) * 1000)
    async with bound_org(deps.engine, org_id) as conn:
        finished = await conn.execute(
            _FINISH,
            {
                "org": org_id,
                "id": claim.run_id,
                "state": outcome,
                "error": error,
                "status": status,
                "ms": duration_ms,
                "now": deps.clock(),
            },
        )
        if finished.rowcount:
            await deps.metrics.record_event(
                conn,
                org_id=org_id,
                kind=MetricKind.TIMER_RUN,
                app_id=claim.app_id,
                user_id=claim.requested_by_user_id,
                properties={
                    "trigger": claim.trigger,
                    "environment": claim.environment_name,
                    "outcome": outcome,
                    "duration_ms": duration_ms,
                },
            )
    return outcome


def _seconds(delta: timedelta) -> int:
    return int(delta.total_seconds())


async def sweep_org(conn: AsyncConnection, org_id: str, *, now: datetime) -> int:
    """Close abandoned runs, drop stale manual runs, pause schedules that lost authority and
    re-defer overdue instants, in ``conn``'s org-bound transaction. Returns how many were
    deferred again."""
    await conn.execute(
        _ABANDON_RUNNING, {"org": org_id, "only": None, "now": now, "slack": _seconds(RUN_SLACK)}
    )
    await conn.execute(_ABANDON_QUEUED, {"org": org_id, "now": now, "cutoff": now - QUEUED_TTL})
    for schedule_id, blocker in await pause_blocked(conn, org_id):
        log.warning(
            "schedule paused: authority lost",
            extra={"org_id": org_id, "schedule_id": schedule_id, "pause_reason": blocker},
        )
    overdue = (await conn.execute(_OVERDUE, {"org": org_id, "cutoff": now - REARM_GRACE})).all()
    deferred = 0
    for schedule_id, instant in overdue:
        job = await defer_scheduled_run(
            conn, org_id=org_id, schedule_id=str(schedule_id), instant=instant
        )
        deferred += job is not None
    return deferred
