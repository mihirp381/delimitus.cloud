"""``POST /internal/v1/heartbeat``: a cell says it is alive and which access snapshot it has
applied (decision 019). The acknowledgement is what ``SnapshotPort.confirmed`` reads."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from ssc_contracts.errors import ErrorCode
from ssc_control.api.idempotency import InternalIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import POST_COMMON, problem_responses
from ssc_control.api.uow import InternalUoW
from ssc_control.snapshot.service import record_ack

router = APIRouter()

_CELL_LABEL = text("select cell_label from ssc.org where id = :org")


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cell_label: str = Field(pattern=r"^[a-z][a-z0-9]{7,15}$")
    snapshot_version: int | None = Field(
        default=None,
        ge=0,
        description="The access snapshot version the cell has applied; 0 or absent for none.",
    )


class HeartbeatAck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    acknowledged: Literal[True]
    org_id: str
    at: datetime


@router.post(
    "/heartbeat",
    response_model=HeartbeatAck,
    dependencies=[InternalIdempotent],
    responses=problem_responses(*POST_COMMON, ErrorCode.FORBIDDEN, ErrorCode.REFERENCE_NOT_FOUND),
)
async def heartbeat(body: Heartbeat, uow: InternalUoW) -> Response:
    """``FORBIDDEN`` when the cell label is not the org's; ``REFERENCE_NOT_FOUND`` for a
    snapshot version that was never published."""
    label = (await uow.conn.execute(_CELL_LABEL, {"org": uow.org_id})).scalar_one()
    if label != body.cell_label:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"reason": "not_the_org_cell"})
    if body.snapshot_version:
        await record_ack(
            uow.conn, uow.org_id, cell_label=body.cell_label, version=body.snapshot_version
        )
    return uow.reply(HeartbeatAck(acknowledged=True, org_id=uow.org_id, at=datetime.now(UTC)))
