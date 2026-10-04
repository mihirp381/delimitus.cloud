"""Approval requests (SSC-045): ask, list, read, and the operator-recorded decision.

Anyone who may change an environment may ask, agent sessions included. The decision endpoint
takes an operator credential only: SSC staff record what a named admin of the org decided by
email or chat. The approver is never the requester and never an agent session; the database
refuses both even if this code is wrong.
"""

from datetime import datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.manifest import Hostname, Name
from ssc_control.api.auth import PrincipalKind
from ssc_control.api.authz import require_builder
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict, require_user
from ssc_control.api.routes.v1.grants import GrantIn
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.approvals import service
from ssc_control.approvals.service import ApprovalRefusedError, ApprovalRow, Decider
from ssc_control.domain.approval_rules import (
    ApprovalState,
    GrantKey,
    Requirement,
    RequirementKind,
    agent_share_subject_key,
    share_subject_key,
)

router = APIRouter()

EnvId = Annotated[str, Field(pattern=r"^env_[a-z0-9]{20}$")]
AprId = Annotated[str, Field(pattern=r"^apr_[a-z0-9]{20}$")]
UsrId = Annotated[str, Field(pattern=r"^usr_[a-z0-9]{20}$")]
Reason = Annotated[str, Field(min_length=1, max_length=500)]

_NAME: Final = TypeAdapter[str](Name)
_HOST: Final = TypeAdapter[str](Hostname)
_REFUSALS: Final[dict[service.RefusalReason, ErrorCode]] = {
    "not_found": ErrorCode.NOT_FOUND,
    "not_pending": ErrorCode.APPROVAL_NOT_PENDING,
    "agent_session": ErrorCode.AGENT_SESSION_REFUSED,
    "self_approval": ErrorCode.SELF_APPROVAL_REFUSED,
    "not_eligible": ErrorCode.APPROVER_NOT_ELIGIBLE,
}
_SELECT_ENV = text("select grants_version from ssc.environment where org_id = :org and id = :env")
_SELECT_ROLE = text("select role, status from ssc.user_account where org_id = :org and id = :id")


class Approval(Strict):
    id: str
    app_id: str
    environment_id: str
    kind: RequirementKind
    subject_key: str = Field(
        description="What exactly is asked: a connection name, a host, or a `sha256:` digest "
        "of the grant set (and, for `agent_share`, the grants version it replaces)."
    )
    payload: dict[str, Any]
    state: ApprovalState
    requested_by_user_id: str
    requested_via_agent: bool
    decided_by_user_id: str | None
    decided_at: datetime | None
    decision_reason: str | None
    decision_channel: Literal["email", "chat", "console"] | None
    recorded_by_operator: str | None = Field(
        description="The SSC operator who recorded the decision, when one did."
    )
    policy_decision_id: str | None
    created_at: datetime


class ApprovalPage(Strict):
    approvals: list[Approval]
    next_before: str | None = Field(
        description="Pass as `before` for the next, older page; null on the last page."
    )


class ApprovalCreate(Strict):
    environment_id: EnvId
    kind: RequirementKind
    subject_key: str | None = Field(
        default=None,
        max_length=300,
        description="The connection name or host. Derived by the server for `widen_audience` "
        "and `agent_share`; when sent for those it must match.",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict[str, Any],
        description="`widen_audience`: `{grants}`. `agent_share`: `{grants_version, grants}`. "
        "Others: `{}`.",
    )


class DecisionIn(Strict):
    outcome: Literal["approved", "denied"]
    approver_user_id: UsrId = Field(description="The org admin who decided, never the requester.")
    channel: Literal["email", "chat"] = Field(description="How the decision reached SSC.")
    reason: Reason


class ApprovalQuery(Strict):
    state: ApprovalState | None = None
    environment_id: EnvId | None = None
    before: AprId | None = Field(default=None, description="The previous page's `next_before`.")
    limit: int = Field(default=50, ge=1, le=200)


class _WidenPayload(Strict):
    grants: list[GrantIn] = Field(max_length=200)


class _AgentSharePayload(Strict):
    grants_version: int = Field(ge=1)
    grants: list[GrantIn] = Field(max_length=200)


class _NoPayload(Strict):
    """Connection names and hosts carry no free text: the subject key says it all."""


def approval_out(row: ApprovalRow) -> Approval:
    return Approval(
        id=row.id,
        app_id=row.app_id,
        environment_id=row.environment_id,
        kind=row.kind,
        subject_key=row.subject_key,
        payload=row.payload,
        state=row.state,
        requested_by_user_id=row.requested_by_user_id,
        requested_via_agent=row.requested_via_agent,
        decided_by_user_id=row.decided_by_user_id,
        decided_at=row.decided_at,
        decision_reason=row.decision_reason,
        decision_channel=row.decision_channel,
        recorded_by_operator=row.recorded_by_operator,
        policy_decision_id=row.policy_decision_id,
        created_at=row.created_at,
    )


def _parse[M: BaseModel](model: type[M], payload: dict[str, Any]) -> M:
    try:
        return model.model_validate(payload)
    except ValidationError as e:
        errors = [{"loc": list(x["loc"]), "msg": x["msg"]} for x in e.errors()]
        raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"payload": errors}) from None


def _grant_keys(grants: list[GrantIn]) -> set[GrantKey]:
    for g in grants:
        if (g.subject_kind == "org") != (g.subject_id is None):
            raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"grant": g.model_dump()})
    return {(g.role, g.subject_kind, g.subject_id) for g in grants}


def _named(body: ApprovalCreate, adapter: TypeAdapter[str]) -> str:
    if body.subject_key is None:
        raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"subject_key": "required"})
    try:
        return adapter.validate_python(body.subject_key)
    except ValidationError as e:
        msg = e.errors()[0]["msg"]
        raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"subject_key": msg}) from None


def _requirement(body: ApprovalCreate, grants_version: int) -> tuple[Requirement, dict[str, Any]]:
    """The question the body asks, and the payload to store with it."""
    match body.kind:
        case RequirementKind.WIDEN_AUDIENCE:
            widen = _parse(_WidenPayload, body.payload)
            key = share_subject_key(_grant_keys(widen.grants))
            stored = widen.model_dump(mode="json")
        case RequirementKind.AGENT_SHARE:
            share = _parse(_AgentSharePayload, body.payload)
            if share.grants_version != grants_version:
                raise Refusal(
                    ErrorCode.PRECONDITION_STALE,
                    evidence={"expected": share.grants_version, "current": grants_version},
                )
            key = agent_share_subject_key(share.grants_version, _grant_keys(share.grants))
            stored = share.model_dump(mode="json")
        case RequirementKind.CONNECT_DATA_SOURCE:
            key = _named(body, _NAME)
            stored = _parse(_NoPayload, body.payload).model_dump(mode="json")
        case RequirementKind.ENABLE_INTERNET_HOSTS:
            key = _named(body, _HOST)
            stored = _parse(_NoPayload, body.payload).model_dump(mode="json")
    if body.subject_key is not None and body.subject_key != key:
        raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"subject_key": key})
    return Requirement(body.kind, key), stored


async def sees_every_request(uow: UnitOfWork) -> bool:
    """Operators and active org admins see every request; members see their own. Workload
    credentials see none."""
    principal = uow.principal
    if principal.kind is PrincipalKind.OPERATOR:
        return True
    if principal.kind is not PrincipalKind.USER:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"kind": principal.kind.value})
    row = (
        await uow.conn.execute(_SELECT_ROLE, {"org": uow.org_id, "id": principal.subject})
    ).first()
    return row is not None and row[0] == "admin" and row[1] == "active"


def _visible(uow: UnitOfWork, row: ApprovalRow, sees_all: bool) -> bool:
    return sees_all or row.requested_by_user_id == uow.principal.subject


@router.post(
    "/approvals",
    status_code=201,
    response_model=Approval,
    dependencies=[UserIdempotent],
    responses={
        200: {
            "model": Approval,
            "description": "The same question is already pending or "
            "approved; that request is returned.",
        },
        **problem_responses(
            *POST_COMMON, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND, ErrorCode.PRECONDITION_STALE
        ),
    },
)
async def create_approval(body: ApprovalCreate, uow: UserUoW) -> Response:
    """Ask another admin of the org to approve one change to one environment. Allowed to the
    environment's builders, its app's owner and org admins, agent sessions included."""
    require_user(uow)
    version = (
        await uow.conn.execute(_SELECT_ENV, {"org": uow.org_id, "env": body.environment_id})
    ).scalar_one_or_none()
    if version is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": body.environment_id})
    by = await require_builder(uow, body.environment_id)
    requirement, payload = _requirement(body, int(version))
    row, created = await service.request(
        uow.conn,
        org_id=uow.org_id,
        environment_id=body.environment_id,
        requirement=requirement,
        requested_by=by,
        via_agent=uow.principal.is_agent,
        payload=payload,
        actor=actor_of(uow.principal),
    )
    return uow.reply(approval_out(row), status=201 if created else 200)


@router.get(
    "/approvals",
    response_model=ApprovalPage,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def list_approvals(params: Annotated[ApprovalQuery, Query()], uow: UserUoW) -> ApprovalPage:
    """Newest first. Org admins and operators see every request; others see their own. An
    operator's read is audited as ``operator.access``."""
    sees_all = await sees_every_request(uow)
    before: ApprovalRow | None = None
    if params.before is not None:
        before = await service.get(uow.conn, org_id=uow.org_id, approval_id=params.before)
        if before is None or not _visible(uow, before, sees_all):
            raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"before": params.before})
    rows = await service.search(
        uow.conn,
        org_id=uow.org_id,
        requested_by=None if sees_all else uow.principal.subject,
        state=params.state,
        environment_id=params.environment_id,
        before=before,
        limit=params.limit + 1,
    )
    page = rows[: params.limit]
    if uow.principal.kind is PrincipalKind.OPERATOR:
        await uow.audit(AuditAction.OPERATOR_ACCESS, target_kind="org", target_id=uow.org_id)
    return ApprovalPage(
        approvals=[approval_out(r) for r in page],
        next_before=page[-1].id if len(rows) > params.limit else None,
    )


@router.get(
    "/approvals/{approval_id}",
    response_model=Approval,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def get_approval(approval_id: Id, uow: UserUoW) -> Approval:
    """One request. A request the caller may not see is ``NOT_FOUND``."""
    sees_all = await sees_every_request(uow)
    row = await service.get(uow.conn, org_id=uow.org_id, approval_id=approval_id)
    if row is None or not _visible(uow, row, sees_all):
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"approval_id": approval_id})
    if uow.principal.kind is PrincipalKind.OPERATOR:
        await uow.audit(
            AuditAction.OPERATOR_ACCESS, target_kind="approval_request", target_id=approval_id
        )
    return approval_out(row)


@router.post(
    "/approvals/{approval_id}/decision",
    response_model=Approval,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.FORBIDDEN,
        ErrorCode.AGENT_SESSION_REFUSED,
        ErrorCode.SELF_APPROVAL_REFUSED,
        ErrorCode.APPROVER_NOT_ELIGIBLE,
        ErrorCode.NOT_FOUND,
        ErrorCode.APPROVAL_NOT_PENDING,
    ),
)
async def decide_approval(approval_id: Id, body: DecisionIn, uow: UserUoW) -> Response:
    """Record what an org admin decided. Operator credentials only, never in an agent session.
    Refused, in this order: not an operator, an agent session, no such request, already decided,
    the approver asked for it, the approver is not an active org admin."""
    principal = uow.principal
    if principal.kind is not PrincipalKind.OPERATOR:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"kind": principal.kind.value})
    if principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    decider = Decider(
        user_id=body.approver_user_id,
        via_agent=principal.is_agent,
        recorded_by_operator=principal.subject,
        channel=body.channel,
        reason=body.reason,
        outcome=body.outcome,
    )
    try:
        row = await service.decide(
            uow.conn,
            org_id=uow.org_id,
            approval_id=approval_id,
            decider=decider,
            actor=actor_of(principal),
        )
    except ApprovalRefusedError as e:
        raise Refusal(_REFUSALS[e.reason], evidence={"approval_id": approval_id}) from None
    await uow.audit(
        AuditAction.OPERATOR_ACCESS,
        target_kind="approval_request",
        target_id=approval_id,
        policy_decision_id=row.policy_decision_id,
    )
    return uow.reply(approval_out(row))
