"""Timers: an environment's schedules and their runs (SSC-041, decision 020).

Schedules come from the manifest and change only with a deploy; here a person who may change the
environment lists them, reads their runs, pauses and resumes them and runs one now. A manual run
answers ``202`` with a ``Location`` to poll; the worker dispatches it. Preview schedules are
stored paused and only run this way.
"""

from datetime import UTC, datetime
from typing import Annotated, Final, Literal, NoReturn

from fastapi import APIRouter, Query, Response
from pydantic import Field
from sqlalchemy import RowMapping, text

from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_builder
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.timers import service
from ssc_control.timers.service import PauseReason, RefusalReason, ScheduleRefusedError

router = APIRouter()

MAX_PAGE: Final = 100
_BASE: Final = "/apps/{app_id}/environments/{environment_id}/schedules"

ScheduleState = Literal["active", "paused", "deleted"]
RunTrigger = Literal["schedule", "manual"]
RunState = Literal["queued", "running", "succeeded", "failed", "timed_out", "skipped"]
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
    "abandoned",
    "start_failed",
]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]
Before = Annotated[
    str | None,
    Query(pattern=r"^tmr_[a-z0-9]{20}$", description="A `next_before` from the previous page."),
]


class TimerRunOut(Strict):
    run_id: str
    schedule_id: str
    trigger: RunTrigger
    state: RunState
    error: RunError | None = Field(description="Why a run failed, timed out or was skipped.")
    http_status: int | None
    start_ms: int | None = Field(
        description="How long the app, and the gateway in front of it, took to answer the run's "
        "start request; the run's timeout counts from after it."
    )
    duration_ms: int | None = Field(description="How long the call to the declared path took.")
    scheduled_for: datetime = Field(
        description="The instant a scheduled run was for; when a manual run was asked for."
    )
    requested_by_user_id: str | None = Field(description="Who asked for a manual run.")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ScheduleOut(Strict):
    schedule_id: str
    environment_id: str
    name: str
    cron: str
    timezone: str
    path: str
    method: Literal["GET", "POST"]
    timeout_seconds: int
    state: ScheduleState
    pause_reason: PauseReason | None
    next_run_at: datetime | None = Field(description="The armed instant of an active schedule.")
    declared_by_user_id: str = Field(
        description="Whose authority it runs on, with the app's owner: who last deployed or "
        "resumed it."
    )
    last_run: TimerRunOut | None


class ScheduleList(Strict):
    environment_id: str
    items: list[ScheduleOut] = Field(description="By name; deleted schedules are left out.")


class TimerRunList(Strict):
    items: list[TimerRunOut] = Field(description="Newest `scheduled_for` first.")
    next_before: str | None = Field(
        description="Pass as `before` for the next page; null at the end."
    )


class RunAccepted(Strict):
    run_id: str
    state: Literal["queued"]


_RUN_COLUMNS: Final = (
    "t.id as run_id, t.schedule_id, t.trigger, t.state, t.error, t.http_status, t.start_ms, "
    "t.duration_ms, t.scheduled_for, t.requested_by_user_id, t.created_at, t.started_at, "
    "t.finished_at"
)
_SCHEDULES: Final = (
    "select s.id as schedule_id, s.environment_id, s.name, s.cron, s.timezone, s.path, "
    "s.method, s.timeout_seconds, s.state, s.pause_reason, s.next_run_at, "
    "s.declared_by_user_id, l.* from ssc.schedule s left join lateral (select "
    "t.id as run_id, t.schedule_id as run_schedule_id, t.trigger, t.state as run_state, t.error, "
    "t.http_status, t.start_ms, t.duration_ms, t.scheduled_for, t.requested_by_user_id, "
    "t.created_at, t.started_at, t.finished_at from ssc.timer_run t "
    "where t.org_id = s.org_id and t.schedule_id = s.id "
    "order by t.scheduled_for desc, t.id desc limit 1) l on true "
    "where s.org_id = :org and s.environment_id = :env "
)
_SELECT_SCHEDULES = text(_SCHEDULES + "and s.state <> 'deleted' order by s.name")
_SELECT_SCHEDULE = text(_SCHEDULES + "and s.id = :id")
_SELECT_ENV = text(
    "select 1 from ssc.environment where org_id = :org and app_id = :app and id = :env"
)
_SELECT_RUNS = text(
    f"select {_RUN_COLUMNS} from ssc.timer_run t where t.org_id = :org "  # noqa: S608  (constant SQL fragments)
    "and t.schedule_id = :id and (cast(:before as text) is null or (t.scheduled_for, t.id) < "
    "(select b.scheduled_for, b.id from ssc.timer_run b where b.org_id = :org and b.id = :before)) "
    "order by t.scheduled_for desc, t.id desc limit :limit"
)
_SELECT_BEFORE = text(
    "select 1 from ssc.timer_run where org_id = :org and schedule_id = :id and id = :before"
)
_SELECT_RUN = text(
    f"select {_RUN_COLUMNS} from ssc.timer_run t "  # noqa: S608  (constant SQL fragments)
    "where t.org_id = :org and t.schedule_id = :id and t.id = :run"
)

_REFUSAL: Final[dict[RefusalReason, ErrorCode]] = {
    "not_found": ErrorCode.NOT_FOUND,
    "deleted": ErrorCode.SCHEDULE_DELETED,
    "in_flight": ErrorCode.TIMER_RUN_IN_FLIGHT,
    "app_inactive": ErrorCode.APP_NOT_ACTIVE,
    "cannot_resume": ErrorCode.SCHEDULE_CANNOT_RESUME,
}


def _refused(e: ScheduleRefusedError) -> NoReturn:
    raise Refusal(_REFUSAL[e.reason], evidence=dict(e.evidence)) from e


async def _environment(uow: UnitOfWork, app_id: str, environment_id: str) -> str:
    """``NOT_FOUND`` for an environment the org's app does not have, then ``FORBIDDEN`` for a
    caller who may not change it; the caller's user id."""
    params = {"org": uow.org_id, "app": app_id, "env": environment_id}
    if (await uow.conn.execute(_SELECT_ENV, params)).first() is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    return await require_builder(uow, environment_id)


def _run(row: RowMapping) -> TimerRunOut:
    return TimerRunOut(**dict(row))


def _schedule(row: RowMapping) -> ScheduleOut:
    last = None
    if row["run_id"] is not None:
        last = TimerRunOut(
            run_id=row["run_id"],
            schedule_id=row["run_schedule_id"],
            trigger=row["trigger"],
            state=row["run_state"],
            error=row["error"],
            http_status=row["http_status"],
            start_ms=row["start_ms"],
            duration_ms=row["duration_ms"],
            scheduled_for=row["scheduled_for"],
            requested_by_user_id=row["requested_by_user_id"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )
    return ScheduleOut(
        schedule_id=row["schedule_id"],
        environment_id=row["environment_id"],
        name=row["name"],
        cron=row["cron"],
        timezone=row["timezone"],
        path=row["path"],
        method=row["method"],
        timeout_seconds=row["timeout_seconds"],
        state=row["state"],
        pause_reason=row["pause_reason"],
        next_run_at=row["next_run_at"],
        declared_by_user_id=row["declared_by_user_id"],
        last_run=last,
    )


async def _one(uow: UnitOfWork, environment_id: str, schedule_id: str) -> ScheduleOut:
    params = {"org": uow.org_id, "env": environment_id, "id": schedule_id}
    row = (await uow.conn.execute(_SELECT_SCHEDULE, params)).mappings().first()
    if row is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"schedule_id": schedule_id})
    return _schedule(row)


@router.get(
    _BASE,
    response_model=ScheduleList,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def list_schedules(app_id: Id, environment_id: Id, uow: UserUoW) -> ScheduleList:
    """The environment's live schedules, each with its latest run."""
    await _environment(uow, app_id, environment_id)
    params = {"org": uow.org_id, "env": environment_id}
    rows = (await uow.conn.execute(_SELECT_SCHEDULES, params)).mappings().all()
    return ScheduleList(environment_id=environment_id, items=[_schedule(r) for r in rows])


@router.get(
    _BASE + "/{schedule_id}",
    response_model=ScheduleOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def get_schedule(
    app_id: Id, environment_id: Id, schedule_id: Id, uow: UserUoW
) -> ScheduleOut:
    """One schedule, deleted ones included."""
    await _environment(uow, app_id, environment_id)
    return await _one(uow, environment_id, schedule_id)


@router.get(
    _BASE + "/{schedule_id}/runs",
    response_model=TimerRunList,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def list_runs(  # noqa: PLR0913  (FastAPI maps each parameter to the request)
    *,
    app_id: Id,
    environment_id: Id,
    schedule_id: Id,
    uow: UserUoW,
    before: Before = None,
    limit: Limit = 50,
) -> TimerRunList:
    """A schedule's runs, newest first. A ``before`` that is not one of its runs is
    ``VALIDATION_FAILED``."""
    await _environment(uow, app_id, environment_id)
    await _one(uow, environment_id, schedule_id)
    params = {"org": uow.org_id, "id": schedule_id, "before": before}
    if before is not None and (await uow.conn.execute(_SELECT_BEFORE, params)).first() is None:
        raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"before": before})
    rows = (await uow.conn.execute(_SELECT_RUNS, {**params, "limit": limit})).mappings().all()
    items = [_run(r) for r in rows]
    return TimerRunList(items=items, next_before=items[-1].run_id if len(items) == limit else None)


@router.get(
    _BASE + "/{schedule_id}/runs/{run_id}",
    response_model=TimerRunOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def get_run(
    app_id: Id, environment_id: Id, schedule_id: Id, run_id: Id, uow: UserUoW
) -> TimerRunOut:
    await _environment(uow, app_id, environment_id)
    await _one(uow, environment_id, schedule_id)
    params = {"org": uow.org_id, "id": schedule_id, "run": run_id}
    row = (await uow.conn.execute(_SELECT_RUN, params)).mappings().first()
    if row is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"run_id": run_id})
    return _run(row)


@router.post(
    _BASE + "/{schedule_id}/runs",
    status_code=202,
    response_model=RunAccepted,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.SCHEDULE_DELETED,
        ErrorCode.APP_NOT_ACTIVE,
        ErrorCode.TIMER_RUN_IN_FLIGHT,
    ),
)
async def run_now(app_id: Id, environment_id: Id, schedule_id: Id, uow: UserUoW) -> Response:
    """Run the schedule once now, paused or in preview: 202 plus a ``Location`` to poll.

    ``TIMER_RUN_IN_FLIGHT`` while a manual run waits or any run is running; ``APP_NOT_ACTIVE``
    while the app is disabled or quarantined."""
    user_id = await _environment(uow, app_id, environment_id)
    try:
        run_id = await service.request_run(
            uow.conn,
            org_id=uow.org_id,
            environment_id=environment_id,
            schedule_id=schedule_id,
            user_id=user_id,
            actor=actor_of(uow.principal),
            now=datetime.now(UTC),
        )
    except ScheduleRefusedError as e:
        _refused(e)
    return uow.reply(
        RunAccepted(run_id=run_id, state="queued"),
        status=202,
        headers={
            "Location": f"/v1/apps/{app_id}/environments/{environment_id}/schedules/"
            f"{schedule_id}/runs/{run_id}"
        },
    )


@router.post(
    _BASE + "/{schedule_id}/pause",
    response_model=ScheduleOut,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND, ErrorCode.SCHEDULE_DELETED
    ),
)
async def pause_schedule(app_id: Id, environment_id: Id, schedule_id: Id, uow: UserUoW) -> Response:
    """Pause by hand until a person resumes it. A schedule SSC paused becomes paused by hand;
    one already paused by hand, or in preview, is left as it is."""
    await _environment(uow, app_id, environment_id)
    try:
        await service.pause(
            uow.conn,
            org_id=uow.org_id,
            environment_id=environment_id,
            schedule_id=schedule_id,
            actor=actor_of(uow.principal),
        )
    except ScheduleRefusedError as e:
        _refused(e)
    return uow.reply(await _one(uow, environment_id, schedule_id))


@router.post(
    _BASE + "/{schedule_id}/resume",
    response_model=ScheduleOut,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.SCHEDULE_DELETED,
        ErrorCode.SCHEDULE_CANNOT_RESUME,
    ),
)
async def resume_schedule(
    app_id: Id, environment_id: Id, schedule_id: Id, uow: UserUoW
) -> Response:
    """Arm it again from now; the caller becomes whose authority it runs on. An active schedule
    is left as it is. ``SCHEDULE_CANNOT_RESUME`` in preview, while the kill switch holds the
    schedule, while the app is not active, and while the app's owner is not active."""
    user_id = await _environment(uow, app_id, environment_id)
    try:
        await service.resume(
            uow.conn,
            org_id=uow.org_id,
            environment_id=environment_id,
            schedule_id=schedule_id,
            user_id=user_id,
            actor=actor_of(uow.principal),
            now=datetime.now(UTC),
        )
    except ScheduleRefusedError as e:
        _refused(e)
    return uow.reply(await _one(uow, environment_id, schedule_id))
