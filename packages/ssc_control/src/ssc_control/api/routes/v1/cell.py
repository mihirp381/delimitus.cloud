"""The org's cell and its lazy resources (SSC-087): which of the database, the egress proxy and
the data gateway it has, what each adds a month, and an org admin turning one on ahead of need.

Nothing here turns a resource off: removing one is a runbook step (SSC-059).
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import Field
from sqlalchemy import text

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

_LABEL = text("select cell_label from ssc.org where id = :org")


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


class CellOut(Strict):
    cell_label: str
    resources: list[CellResourceOut]


def _out(resource: CellResource, row: resources.CellResourceRow | None) -> CellResourceOut:
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
    )


async def _cell(uow: UnitOfWork) -> CellOut:
    label = str((await uow.conn.execute(_LABEL, {"org": uow.org_id})).scalar_one())
    found = await resources.states(uow.conn, uow.org_id)
    return CellOut(cell_label=label, resources=[_out(r, found.get(r)) for r in CellResource])


@router.get(
    "/cell",
    response_model=CellOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def get_cell(uow: UserUoW) -> CellOut:
    """Active org admins only (``FORBIDDEN``). Every lazy resource, ``off`` included."""
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
