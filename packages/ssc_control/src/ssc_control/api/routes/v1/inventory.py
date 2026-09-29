"""The admin's inventory and the kill switch (SSC-025, C30).

Every endpoint here is for an org's active admins. Anyone else gets ``FORBIDDEN`` before any
lookup, so a member cannot tell which apps exist.

Pulling the switch answers ``202`` once the app's new status is committed: the deny holds from
then on, whatever happens to the saga the worker runs after it. ``GET`` on the run's
``Location`` shows each step with its timings. ``enable`` puts the app back; the reconciler
restores serving. A transfer moves an app to another active member.
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_admin
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.apps import AppOut, EnvironmentOut
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.runtime import runtime_of
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.lifecycle import inventory, kill_switch
from ssc_control.lifecycle.kill_switch import Mode, RefusedError, StepName, StepState

router = APIRouter()

AppStatus = Literal["active", "disabled", "quarantined"]
_SLUG_PATTERN = r"^[a-z]([a-z0-9-]{0,38}[a-z0-9])?$"


class Owner(Strict):
    user_id: str
    display_name: str


class CurrentRelease(Strict):
    release_id: str
    number: int


class LastDeploy(Strict):
    operation_id: str
    kind: Literal["deploy", "rollback"]
    state: Literal["pending", "running", "healthy", "failed", "superseded"]
    at: datetime = Field(description="When it finished, or started if it has not.")


class Sharing(Strict):
    org_wide: bool
    users: int = Field(description="Grants to single users, builders included.")
    groups: int = Field(description="Grants to groups, builders included.")


class InventoryEnvironment(Strict):
    environment_id: str
    name: Literal["prod", "preview"]
    current_release: CurrentRelease | None
    last_deploy: LastDeploy | None
    sharing: Sharing


class InventoryApp(Strict):
    app_id: str
    slug: str
    status: AppStatus
    owner: Owner
    created_at: datetime
    last_used_at: datetime | None = Field(
        description="The last time anyone opened the app; null until the gateway reports it."
    )
    environments: list[InventoryEnvironment]


class InventoryPage(Strict):
    items: list[InventoryApp]
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page.")


class KillSwitchCreate(Strict):
    mode: Mode = Field(
        description="`disable` stops the app; `quarantine` also freezes its sharing rules. "
        "A disabled app can still be quarantined."
    )


class KillSwitchAccepted(Strict):
    run_id: str
    state: Literal["running"]


class KillSwitchStep(Strict):
    name: StepName
    state: StepState
    snapshot_version: int | None
    started_at: datetime
    finished_at: datetime | None
    elapsed_ms: int | None
    attempts: int
    error: str | None = Field(description="The reason code of the last failed try, if any.")


class KillSwitchRun(Strict):
    run_id: str
    app_id: str
    mode: Mode
    state: Literal["running", "completed", "failed"]
    steps: list[KillSwitchStep] = Field(description="The steps begun so far, in order.")
    started_at: datetime
    finished_at: datetime | None
    total_ms: int | None


class OwnerTransfer(Strict):
    user_id: str = Field(pattern=r"^usr_[a-z0-9]{20}$")


_SELECT_RUN = text(
    "select id, app_id, mode, state, steps, started_at, finished_at "
    "from ssc.kill_switch_run where org_id = :org and app_id = :app and id = :id"
)
_SELECT_APP = text(
    "select id, slug, owner_user_id, status, created_at from ssc.app "
    "where org_id = :org and id = :id"
)
_SELECT_ENVS = text(
    "select id, name, config_version, grants_version, current_deployment_id "
    "from ssc.environment where org_id = :org and app_id = :app order by name"
)
_LOCK_APP_OWNER = text(
    "select owner_user_id from ssc.app where org_id = :org and id = :id for update"
)
_SELECT_USER_STATUS = text("select status from ssc.user_account where org_id = :org and id = :id")
_SET_OWNER = text("update ssc.app set owner_user_id = :owner where org_id = :org and id = :id")
_STATUS_ACTION = {
    "disabled": AuditAction.APP_DISABLED,
    "quarantined": AuditAction.APP_QUARANTINED,
}


async def _app_out(uow: UnitOfWork, app_id: str) -> AppOut:
    row = (await uow.conn.execute(_SELECT_APP, {"org": uow.org_id, "id": app_id})).mappings().one()
    envs = await uow.conn.execute(_SELECT_ENVS, {"org": uow.org_id, "app": app_id})
    return AppOut(**dict(row), environments=[EnvironmentOut(**dict(e)) for e in envs.mappings()])


@router.get(
    "/inventory",
    response_model=InventoryPage,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def get_inventory(
    uow: UserUoW,
    limit: Annotated[int, Query(ge=1, le=inventory.MAX_PAGE)] = inventory.MAX_PAGE,
    cursor: Annotated[
        str | None, Query(pattern=_SLUG_PATTERN, description="The previous page's `next_cursor`.")
    ] = None,
) -> InventoryPage:
    """Every app in the org by slug, with its owner, environments, sharing and last use."""
    await require_admin(uow)
    rows, next_cursor = await inventory.page(uow.conn, uow.org_id, limit=limit, cursor=cursor)
    return InventoryPage(
        items=[InventoryApp.model_validate(r) for r in rows], next_cursor=next_cursor
    )


@router.post(
    "/apps/{app_id}/kill-switch",
    status_code=202,
    response_model=KillSwitchAccepted,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.APP_NOT_ACTIVE,
        ErrorCode.KILL_SWITCH_IN_FLIGHT,
    ),
)
async def pull_kill_switch(app_id: Id, body: KillSwitchCreate, uow: UserUoW) -> Response:
    """Stop the app now: 202 plus a ``Location`` to follow the steps.

    ``APP_NOT_ACTIVE`` when the app is already stopped in that mode; ``KILL_SWITCH_IN_FLIGHT``
    while an earlier pull is still running."""
    await require_admin(uow)
    try:
        started = await kill_switch.start(
            uow.conn,
            org_id=uow.org_id,
            app_id=app_id,
            mode=body.mode,
            actor=actor_of(uow.principal),
        )
    except RefusedError as e:
        raise Refusal(e.code, evidence=dict(e.evidence)) from e
    await uow.audit(
        _STATUS_ACTION[started.after],
        target_kind="app",
        target_id=app_id,
        before={"status": started.before},
        after={"status": started.after},
    )
    return uow.reply(
        KillSwitchAccepted(run_id=started.run_id, state="running"),
        status=202,
        headers={"Location": f"/v1/apps/{app_id}/kill-switch/{started.run_id}"},
    )


@router.get(
    "/apps/{app_id}/kill-switch/{run_id}",
    response_model=KillSwitchRun,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def get_kill_switch_run(app_id: Id, run_id: Id, uow: UserUoW) -> KillSwitchRun:
    """One pull of the switch: each step's state and timings, and the total."""
    await require_admin(uow)
    params = {"org": uow.org_id, "app": app_id, "id": run_id}
    row = (await uow.conn.execute(_SELECT_RUN, params)).mappings().first()
    if row is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"run_id": run_id})
    started: datetime = row["started_at"]
    finished: datetime | None = row["finished_at"]
    return KillSwitchRun(
        run_id=row["id"],
        app_id=row["app_id"],
        mode=row["mode"],
        state=row["state"],
        steps=[
            KillSwitchStep(
                name=s.name,
                state=s.state,
                snapshot_version=s.snapshot_version,
                started_at=s.started_at,
                finished_at=s.finished_at,
                elapsed_ms=s.elapsed_ms,
                attempts=s.attempts,
                error=s.error,
            )
            for s in kill_switch.steps_of(row["steps"])
        ],
        started_at=started,
        finished_at=finished,
        total_ms=None if finished is None else kill_switch.span_ms(started, finished),
    )


@router.post(
    "/apps/{app_id}/enable",
    response_model=AppOut,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.APP_ALREADY_ACTIVE,
        ErrorCode.KILL_SWITCH_IN_FLIGHT,
    ),
)
async def enable_app(app_id: Id, request: Request, uow: UserUoW) -> Response:
    """Make a disabled or quarantined app active again and resume the schedules the kill
    switch paused. The reconciler brings it back up. ``KILL_SWITCH_IN_FLIGHT`` until the
    running pull has finished."""
    await require_admin(uow)
    try:
        before = await kill_switch.enable(
            uow.conn,
            org_id=uow.org_id,
            app_id=app_id,
            timers=runtime_of(request).timers,
            actor=actor_of(uow.principal),
        )
    except RefusedError as e:
        raise Refusal(e.code, evidence=dict(e.evidence)) from e
    await uow.audit(
        AuditAction.APP_ENABLED,
        target_kind="app",
        target_id=app_id,
        before={"status": before},
        after={"status": "active"},
    )
    return uow.reply(await _app_out(uow, app_id))


@router.put(
    "/apps/{app_id}/owner",
    response_model=AppOut,
    responses=problem_responses(
        *AUTHENTICATED,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.REFERENCE_NOT_FOUND,
        ErrorCode.OWNER_NOT_ACTIVE,
    ),
)
async def transfer_owner(app_id: Id, body: OwnerTransfer, uow: UserUoW) -> AppOut:
    """Give the app to another member. ``REFERENCE_NOT_FOUND`` for a user not in the org,
    ``OWNER_NOT_ACTIVE`` for a deactivated one."""
    await require_admin(uow)
    before = (
        await uow.conn.execute(_LOCK_APP_OWNER, {"org": uow.org_id, "id": app_id})
    ).scalar_one_or_none()
    if before is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"app_id": app_id})
    status = (
        await uow.conn.execute(_SELECT_USER_STATUS, {"org": uow.org_id, "id": body.user_id})
    ).scalar_one_or_none()
    if status is None:
        raise Refusal(ErrorCode.REFERENCE_NOT_FOUND, evidence={"user_id": body.user_id})
    if status != "active":
        raise Refusal(ErrorCode.OWNER_NOT_ACTIVE, evidence={"user_id": body.user_id})
    if before != body.user_id:
        await uow.conn.execute(_SET_OWNER, {"org": uow.org_id, "id": app_id, "owner": body.user_id})
        await uow.audit(
            AuditAction.APP_OWNER_TRANSFERRED,
            target_kind="app",
            target_id=app_id,
            before={"owner_user_id": before},
            after={"owner_user_id": body.user_id},
        )
    return await _app_out(uow, app_id)
