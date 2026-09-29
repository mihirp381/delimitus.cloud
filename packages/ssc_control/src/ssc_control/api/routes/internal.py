"""``/internal/v1``: what cell services call. Workload and operator credentials only.

One stub so the surface exists in the OpenAPI file and the conventions (credential audience,
idempotency, unit of work) are exercised. Real endpoints arrive with SSC-013 (snapshot
acknowledgements) and SSC-016 (deployment progress).
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from ssc_contracts.errors import ErrorCode
from ssc_control.api.idempotency import InternalIdempotent
from ssc_control.api.routes.common import POST_COMMON, problem_responses
from ssc_control.api.uow import InternalUoW

router = APIRouter(prefix="/internal/v1", tags=["internal"])


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cell_label: str = Field(pattern=r"^[a-z][a-z0-9]{7,15}$")
    snapshot_version: int | None = Field(default=None, ge=0)


class HeartbeatAck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    acknowledged: Literal[True]
    org_id: str
    at: datetime


@router.post(
    "/heartbeat",
    response_model=HeartbeatAck,
    dependencies=[InternalIdempotent],
    responses=POST_COMMON | problem_responses(ErrorCode.FORBIDDEN),
)
async def heartbeat(body: Heartbeat, uow: InternalUoW) -> Response:
    return uow.reply(HeartbeatAck(acknowledged=True, org_id=uow.org_id, at=datetime.now(UTC)))
