"""The org's cell and its lazy resources (SSC-087): which of the database, the egress proxy and
the data gateway it has, what each adds a month, and an org admin turning one on ahead of need.

The console's "Your environment" screen (SSC-057) reads here the deployment or approval that
asked for each resource, from its latest ``cell.resource_requested`` audit row; the cell's
environments and which have a database; and the database's places, counted from the control
plane's records (the cell agent's own count is the one that refuses, ``DB_TIER_FULL``). The fixed
outbound IP is in ``GET /v1/egress`` (SSC-053). The monthly figures are not a bill (A6).

Nothing here turns a resource off: removing one is a runbook step (SSC-059).
"""

from datetime import datetime
from typing import Final, Literal

from fastapi import APIRouter, Response
from pydantic import Field
from sqlalchemy import text

from ssc_contracts import app_database
from ssc_contracts.audit import AuditAction
from ssc_contracts.cells import MONTHLY_USD, CellResource, CellResourceCause
from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_admin
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Strict
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.cell import resources

router = APIRouter()

ResourceName = Literal["database", "egress", "connections"]
ResourceState = Literal["off", "requested", "creating", "ready", "failed"]

PLACES_TOTAL: Final = app_database.ceiling(
    app_database.BASE_TIER_MAX_CONNECTIONS, app_database.SUPERUSER_RESERVE
)
NEARLY_FULL_FREE: Final = 2
"""At this many free places or fewer the database is nearly full and the bigger one is offered."""

_LABEL = text("select cell_label from ssc.org where id = :org")
_ASKED_BY = text(
    "select distinct on (a.target_id) a.target_id, a.after->>'deployment_id', r.id "
    "from ssc.audit_event a left join ssc.approval_request r "
    "on r.org_id = a.org_id and r.policy_decision_id = a.policy_decision_id "
    "where a.org_id = :org and a.target_kind = :kind and a.action = :action "
    "order by a.target_id, a.seq desc, r.id"
)
_ENVIRONMENTS = text(
    "select e.id, e.app_id, a.slug, e.name, d.environment_id is not null "
    "from ssc.environment e join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "left join ssc.app_database d on d.org_id = e.org_id and d.environment_id = e.id "
    "where e.org_id = :org order by a.slug, e.name"
)
_TIER_FULL_AT = text(
    "select max(finished_at) from ssc.deployment where org_id = :org and failure_code = :code"
)


class CellResourceOut(Strict):
    resource: ResourceName
    state: ResourceState = Field(description="`off` until something asks for it.")
    cause: Literal["deploy", "egress_approved", "connection_granted", "file_use", "admin"] | None
    monthly_usd: int = Field(description="About what it adds to the cell's bill a month.")
    attempts: int
    failure_code: str | None
    requested_at: datetime | None
    started_at: datetime | None
    ready_at: datetime | None
    failed_at: datetime | None
    deployment_id: str | None = Field(description="The deployment that asked for it, if one did.")
    approval_id: str | None = Field(description="The approval that asked for it, if one did.")


class CellEnvironmentOut(Strict):
    environment_id: str
    app_id: str
    app_slug: str
    name: Literal["prod", "preview"]
    has_database: bool


class CellDatabaseOut(Strict):
    tier: str = Field(description="The Cloud SQL tier every cell's database is created on.")
    places_used: int = Field(description="App environments with a database, as recorded here.")
    places_total: int = Field(description="App databases the tier holds.")
    connection_limit: int = Field(description="Connections each app database may hold at once.")
    nearly_full: bool = Field(description=f"{NEARLY_FULL_FREE} free places or fewer.")
    tier_full_at: datetime | None = Field(
        description="The last deployment refused with `DB_TIER_FULL`; null if none was."
    )
    bigger_tier: str = Field(description='The paid "bigger database" step (A6: not charged).')
    bigger_tier_monthly_usd: int = Field(description="About what the bigger tier costs a month.")


class CellOut(Strict):
    cell_label: str
    resources: list[CellResourceOut]
    environments: list[CellEnvironmentOut]
    database: CellDatabaseOut


def _out(
    resource: CellResource,
    row: resources.CellResourceRow | None,
    asked_by: tuple[str | None, str | None],
) -> CellResourceOut:
    if row is None:
        return CellResourceOut(
            resource=resource.value,
            state="off",
            cause=None,
            monthly_usd=MONTHLY_USD[resource],
            attempts=0,
            failure_code=None,
            requested_at=None,
            started_at=None,
            ready_at=None,
            failed_at=None,
            deployment_id=None,
            approval_id=None,
        )
    return CellResourceOut(
        resource=resource.value,
        state=row.state.value,
        cause=row.cause.value,
        monthly_usd=row.monthly_usd,
        attempts=row.attempts,
        failure_code=row.failure_code,
        requested_at=row.requested_at,
        started_at=row.started_at,
        ready_at=row.ready_at,
        failed_at=row.failed_at,
        deployment_id=asked_by[0],
        approval_id=asked_by[1],
    )


def _database(
    environments: list[CellEnvironmentOut], tier_full_at: datetime | None
) -> CellDatabaseOut:
    used = sum(e.has_database for e in environments)
    return CellDatabaseOut(
        tier=app_database.BASE_TIER,
        places_used=used,
        places_total=PLACES_TOTAL,
        connection_limit=app_database.CONNECTION_LIMIT,
        nearly_full=PLACES_TOTAL - used <= NEARLY_FULL_FREE,
        tier_full_at=tier_full_at,
        bigger_tier=app_database.BIGGER_TIER,
        bigger_tier_monthly_usd=app_database.BIGGER_TIER_MONTHLY_USD,
    )


async def _cell(uow: UnitOfWork) -> CellOut:
    org = {"org": uow.org_id}
    label = str((await uow.conn.execute(_LABEL, org)).scalar_one())
    found = await resources.states(uow.conn, uow.org_id)
    asked = await uow.conn.execute(
        _ASKED_BY,
        {**org, "kind": resources.TARGET_KIND, "action": AuditAction.CELL_RESOURCE_REQUESTED.value},
    )
    asked_by = {str(r): (dep, appr) for r, dep, appr in asked.all()}
    environments = [
        CellEnvironmentOut(
            environment_id=env, app_id=app, app_slug=slug, name=name, has_database=bool(has_db)
        )
        for env, app, slug, name, has_db in (await uow.conn.execute(_ENVIRONMENTS, org)).all()
    ]
    full = {**org, "code": ErrorCode.DB_TIER_FULL.value}
    tier_full_at = (await uow.conn.execute(_TIER_FULL_AT, full)).scalar()
    return CellOut(
        cell_label=label,
        resources=[
            _out(r, found.get(r), asked_by.get(r.value, (None, None))) for r in CellResource
        ],
        environments=environments,
        database=_database(environments, tier_full_at),
    )


@router.get(
    "/cell",
    response_model=CellOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def get_cell(uow: UserUoW) -> CellOut:
    """Active org admins only (``FORBIDDEN``). Every lazy resource, ``off`` included, the cell's
    environments and its database's places."""
    await require_admin(uow)
    return await _cell(uow)


@router.post(
    "/cell/resources/{resource}/enable",
    response_model=CellOut,
    dependencies=[UserIdempotent],
    responses=problem_responses(*POST_COMMON, ErrorCode.FORBIDDEN, ErrorCode.AGENT_SESSION_REFUSED),
)
async def enable_resource(resource: ResourceName, uow: UserUoW) -> Response:
    """Turn one resource on before an app needs it. Active org admins only, never in an agent
    session. One already on, or being created, is left as it is; a failed one is tried again.
    Audited as ``cell.resource_requested`` when it is asked for."""
    await require_admin(uow)
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    await resources.request(
        uow.conn,
        org_id=uow.org_id,
        resource=CellResource(resource),
        cause=CellResourceCause.ADMIN,
        actor=actor_of(uow.principal),
    )
    return uow.reply(await _cell(uow))
