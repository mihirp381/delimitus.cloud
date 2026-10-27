"""Approval requests (SSC-045, SSC-049): ask, list, read, decide, cancel.

Anyone who may change an environment may ask, agent sessions included. Admins and the owner of
a connection a request names decide from the inbox (``/decide``), in a normal session: never an
agent session, and the requester only when an admin. Approving applies the grant
(:func:`ssc_control.api.routes.v1.grants.apply_approved`). The ``/decision`` endpoint stays for
SSC staff recording what an admin decided by email or chat. The database refuses agent approval
even if this code is wrong.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Final, Literal, cast

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.egress import host_pattern_problem
from ssc_contracts.errors import ErrorCode
from ssc_contracts.manifest import Hostname, Name
from ssc_contracts.text import PRINTABLE_LINES
from ssc_control import names
from ssc_control.api.auth import PrincipalKind
from ssc_control.api.authz import require_builder
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict, require_user
from ssc_control.api.routes.v1.grants import (
    ApplyOutcome,
    GrantIn,
    apply_approved,
    lock_environment,
    missing_subjects,
)
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.approvals import service
from ssc_control.approvals.service import ApprovalRefusedError, ApprovalRow, Decider
from ssc_control.connections import service as connections
from ssc_control.domain.approval_rules import (
    ApprovalState,
    GrantKey,
    Requirement,
    RequirementKind,
    agent_share_subject_key,
    check_decider,
    exceed_subject_key,
    share_subject_key,
)

router = APIRouter()

EnvId = Annotated[str, Field(pattern=r"^env_[a-z0-9]{20}$")]
AprId = Annotated[str, Field(pattern=r"^apr_[a-z0-9]{20}$")]
UsrId = Annotated[str, Field(pattern=r"^usr_[a-z0-9]{20}$")]
Reason = Annotated[str, Field(min_length=1, max_length=500, pattern=PRINTABLE_LINES)]

_NAME: Final = TypeAdapter[str](Name)
_HOST: Final = TypeAdapter[str](Hostname)
_REFUSALS: Final[dict[service.RefusalReason, ErrorCode]] = {
    "not_found": ErrorCode.NOT_FOUND,
    "not_pending": ErrorCode.APPROVAL_NOT_PENDING,
    "agent_session": ErrorCode.AGENT_SESSION_REFUSED,
    "self_approval": ErrorCode.SELF_APPROVAL_REFUSED,
    "not_eligible": ErrorCode.APPROVER_NOT_ELIGIBLE,
    "not_requester": ErrorCode.FORBIDDEN,
}
_SHARING: Final = frozenset(
    {RequirementKind.WIDEN_AUDIENCE, RequirementKind.AGENT_SHARE, RequirementKind.EXCEED_CEILING}
)
_USER_NAMES = text(
    "select id, display_name from ssc.user_account where org_id = :org and id = any(:ids)"
)
_GROUP_NAMES = text(
    "select id, display_name from ssc.user_group where org_id = :org and id = any(:ids)"
)
CANCELLED_BY_REQUESTER: Final = "Withdrawn by the requester."
_SHARE_ENV = text(
    "select 1 from ssc.environment e join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "where e.org_id = :org and e.id = :env and a.status not in ('deleting', 'deleted') "
    "for share of e"
)
# FOR SHARE waits for a removal asked for at the same moment (GA-12), whose last step cancels
# the requests that are open.
_SELECT_ENV = text(
    "select e.grants_version, a.id, a.status from ssc.environment e "
    "join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "where e.org_id = :org and e.id = :env and a.status <> 'deleted' for share of a"
)
_SELECT_ROLE = text("select role, status from ssc.user_account where org_id = :org and id = :id")


class Approval(Strict):
    id: str
    app_id: str
    app: str = Field(description="The app's slug.")
    environment_id: str
    environment: Literal["prod", "preview"]
    kind: RequirementKind
    subject_key: str = Field(
        description="What exactly is asked: a connection name, a host, or a `sha256:` digest "
        "of the grant set (and, for `agent_share`, the grants version it replaces)."
    )
    payload: dict[str, Any]
    state: ApprovalState
    requested_by_user_id: str
    requested_by_name: str
    requested_by_email: str | None
    requested_via_agent: bool
    decided_by_user_id: str | None
    decided_by_name: str | None = Field(
        description="Who decided or withdrew it; `Former member` once they are no longer active."
    )
    decided_by_email: str | None
    decided_at: datetime | None
    decision_reason: str | None
    decision_channel: Literal["email", "chat", "console", "cli"] | None
    recorded_by_operator: str | None = Field(
        description="The SSC operator who recorded the decision, when one did."
    )
    policy_decision_id: str | None
    created_at: datetime
    subject_names: dict[str, str] = Field(
        description="The name of each person and group the payload's grants name, by id."
    )


Role = Literal["builder", "user"]
SubjectKind = Literal["user", "group", "org"]


class DiffGrant(Strict):
    role: Role
    subject_kind: SubjectKind
    subject_id: str | None
    subject_name: str | None = Field(description="The user's or group's display name, if known.")


class GrantDiff(Strict):
    """What the request would change about the environment's sharing rules, compared with the
    rules in force now."""

    added: list[DiffGrant]
    removed: list[DiffGrant]


class ApprovalConnection(Strict):
    """The data connection an `exceed_ceiling` request is about (never its address)."""

    name: str
    classification: Literal["internal", "confidential", "restricted"]
    owner_user_id: str | None
    ceiling_audience: Literal["org", "subjects"]
    ceiling_subjects: int = Field(description="How many groups and users the ceiling lists.")


class ApprovalDetail(Approval):
    """One request with what a decider needs to see: names, who asked and the change."""

    grant_diff: GrantDiff | None = Field(
        description="Sharing requests only; null for a data source or an internet host."
    )
    connection: ApprovalConnection | None
    can_decide: bool = Field(description="Whether the caller may approve or reject it now.")
    can_cancel: bool = Field(description="Whether the caller may withdraw it now.")


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
        description="The connection name or host. Derived by the server for `widen_audience`, "
        "`agent_share` and `exceed_ceiling`; when sent for those it must match.",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict[str, Any],
        description="`widen_audience`: `{grants}`. `agent_share`: `{grants_version, grants}`. "
        "`exceed_ceiling`: `{connection, grants}`. Others: `{}`.",
    )


class DecisionIn(Strict):
    outcome: Literal["approved", "denied"]
    approver_user_id: UsrId = Field(
        description="The org admin who decided, or for `exceed_ceiling` the connection's owner."
    )
    channel: Literal["email", "chat"] = Field(description="How the decision reached SSC.")
    reason: Reason


class PersonDecisionIn(Strict):
    outcome: Literal["approved", "denied"]
    reason: Reason
    channel: Literal["console", "cli"] = Field(
        default="console", description="Where the decision was made."
    )


class ApprovalDecided(Approval):
    """A decision, and what approving it did."""

    applied: ApplyOutcome = Field(
        description="`applied`: the grants were written, or for `connect_data_source` the "
        "environment is linked to the connection. `waiting`: another requirement for the same "
        "change is still open. `not_applied`: approved, but the change no longer fits (see "
        "`applied_reason`: for `connect_data_source`, `connection_missing` or `ceiling`); ask "
        "again. `not_applicable`: nothing to apply."
    )
    applied_reason: str | None


class CancelIn(Strict):
    reason: Reason = CANCELLED_BY_REQUESTER
    channel: Literal["console", "cli"] = "console"


class ApprovalQuery(Strict):
    inbox: bool = Field(
        default=False,
        description="Only the pending requests the caller may decide: every one for an org "
        "admin, their own included, else those on connections they own that they did not ask "
        "for. Empty in an agent session.",
    )
    state: ApprovalState | None = None
    environment_id: EnvId | None = None
    before: AprId | None = Field(default=None, description="The previous page's `next_before`.")
    limit: int = Field(default=50, ge=1, le=200)


class _WidenPayload(Strict):
    grants: list[GrantIn] = Field(max_length=200)


class _AgentSharePayload(Strict):
    grants_version: int = Field(ge=1)
    grants: list[GrantIn] = Field(max_length=200)


class _ExceedPayload(Strict):
    connection: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
    grants: list[GrantIn] = Field(max_length=200)


class _NoPayload(Strict):
    """Connection names and hosts carry no free text: the subject key says it all."""


def _payload_subjects(row: ApprovalRow) -> list[tuple[str, str]]:
    grants: object = row.payload.get("grants")
    if not isinstance(grants, list):
        return []
    out: list[tuple[str, str]] = []
    for g in cast(list[object], grants):
        if not isinstance(g, dict):
            continue
        grant = cast(dict[str, object], g)
        kind, subject = grant.get("subject_kind"), grant.get("subject_id")
        if kind in {"user", "group"} and isinstance(subject, str) and subject:
            out.append((str(kind), subject))
    return out


async def approvals_out(uow: UnitOfWork, rows: Sequence[ApprovalRow]) -> list[Approval]:
    wanted = [s for r in rows for s in _payload_subjects(r)]
    users = await names.people(uow.conn, uow.org_id, [i for k, i in wanted if k == "user"])
    found = await names.groups(uow.conn, uow.org_id, [i for k, i in wanted if k == "group"])

    def named(kind: str, subject_id: str) -> str:
        if kind == "user":
            return users.of(subject_id).name
        return found.get(subject_id, names.REMOVED_GROUP)

    return [approval_out(r, {i: named(k, i) for k, i in _payload_subjects(r)}) for r in rows]


async def approval_named(uow: UnitOfWork, row: ApprovalRow) -> Approval:
    return (await approvals_out(uow, [row]))[0]


def approval_out(row: ApprovalRow, subject_names: dict[str, str]) -> Approval:
    return Approval(
        id=row.id,
        app_id=row.app_id,
        app=row.app,
        environment_id=row.environment_id,
        environment=cast(Literal["prod", "preview"], row.environment),
        kind=row.kind,
        subject_key=row.subject_key,
        payload=row.payload,
        state=row.state,
        requested_by_user_id=row.requested_by_user_id,
        requested_by_name=row.requested_by_name,
        requested_by_email=row.requested_by_email,
        requested_via_agent=row.requested_via_agent,
        decided_by_user_id=row.decided_by_user_id,
        decided_by_name=row.decided_by_name,
        decided_by_email=row.decided_by_email,
        decided_at=row.decided_at,
        decision_reason=row.decision_reason,
        decision_channel=row.decision_channel,
        recorded_by_operator=row.recorded_by_operator,
        policy_decision_id=row.policy_decision_id,
        created_at=row.created_at,
        subject_names=subject_names,
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
        case RequirementKind.EXCEED_CEILING:
            exceed = _parse(_ExceedPayload, body.payload)
            key = exceed_subject_key(exceed.connection, _grant_keys(exceed.grants))
            stored = exceed.model_dump(mode="json")
        case RequirementKind.CONNECT_DATA_SOURCE:
            key = _named(body, _NAME)
            stored = _parse(_NoPayload, body.payload).model_dump(mode="json")
        case RequirementKind.ENABLE_INTERNET_HOSTS:
            key = _named(body, _HOST)
            # A host the allowlist refuses (Cloud Storage, say) could be approved and then
            # never allowed, so it is refused here.
            if (problem := host_pattern_problem(key)) is not None:
                raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"subject_key": problem})
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


async def _visible(uow: UnitOfWork, row: ApprovalRow, sees_all: bool) -> bool:
    """Everyone sees their own requests; the owner of the connection an `exceed_ceiling`
    request names sees that one."""
    me = uow.principal.subject
    if sees_all or row.requested_by_user_id == me:
        return True
    name = row.connection
    return name is not None and await service.connection_owner(uow.conn, uow.org_id, name) == me


async def _display_names(uow: UnitOfWork, query: Any, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = await uow.conn.execute(query, {"org": uow.org_id, "ids": ids})
    return {str(r[0]): str(r[1]) for r in rows}


async def _diff(uow: UnitOfWork, row: ApprovalRow) -> GrantDiff | None:
    """The stored grant set against the environment's rules now; None for other kinds."""
    if row.kind not in _SHARING:
        return None
    wanted = {
        (g.role, g.subject_kind, g.subject_id)
        for g in _parse(_WidenPayload, {"grants": row.payload.get("grants", [])}).grants
    }
    current = await connections.environment_grants(uow.conn, uow.org_id, row.environment_id)

    def keyed(keys: set[GrantKey]) -> list[GrantKey]:
        return sorted(keys, key=lambda k: (k[0], k[1], k[2] or ""))

    users = {k[2] for k in wanted | current if k[1] == "user" and k[2]}
    groups = {k[2] for k in wanted | current if k[1] == "group" and k[2]}
    names = await _display_names(uow, _USER_NAMES, sorted(users))
    names |= await _display_names(uow, _GROUP_NAMES, sorted(groups))

    def out(keys: set[GrantKey]) -> list[DiffGrant]:
        return [
            DiffGrant(
                role=cast(Role, r),
                subject_kind=cast(SubjectKind, k),
                subject_id=i,
                subject_name=None if i is None else names.get(i),
            )
            for r, k, i in keyed(keys)
        ]

    return GrantDiff(added=out(wanted - current), removed=out(current - wanted))


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
            *POST_COMMON,
            ErrorCode.FORBIDDEN,
            ErrorCode.NOT_FOUND,
            ErrorCode.PRECONDITION_STALE,
            ErrorCode.APP_DELETING,
        ),
    },
)
async def create_approval(body: ApprovalCreate, uow: UserUoW) -> Response:
    """Ask an admin of the org to approve one change to one environment. Allowed to the
    environment's builders, its app's owner and org admins, agent sessions included. An
    `exceed_ceiling` request is decided by the named connection's owner or an org admin. A grant
    naming a user or group the org does not have is `NOT_FOUND`."""
    require_user(uow)
    params = {"org": uow.org_id, "env": body.environment_id}
    await uow.conn.execute(_SHARE_ENV, params)
    env = (await uow.conn.execute(_SELECT_ENV, params)).first()
    if env is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": body.environment_id})
    if env.status == "deleting":
        raise Refusal(ErrorCode.APP_DELETING, evidence={"app_id": env.id})
    by = await require_builder(uow, body.environment_id)
    requirement, payload = _requirement(body, int(env.grants_version))
    grants = cast("list[dict[str, Any]]", payload.get("grants", []))
    if missing := await missing_subjects(
        uow, ((g["subject_kind"], g["subject_id"]) for g in grants)
    ):
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"subject_ids": missing})
    if requirement.kind is RequirementKind.EXCEED_CEILING:
        named = str(payload["connection"])
        if await connections.get(uow.conn, uow.org_id, named) is None:
            raise Refusal(ErrorCode.NOT_FOUND, evidence={"connection": named})
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
    return uow.reply(await approval_named(uow, row), status=201 if created else 200)


async def _detail(uow: UnitOfWork, row: ApprovalRow, sees_all: bool) -> ApprovalDetail:
    linked = (
        None
        if row.connection is None
        else await connections.get(uow.conn, uow.org_id, row.connection)
    )
    me = uow.principal.subject
    pending = row.state == "pending"
    is_user = uow.principal.kind is PrincipalKind.USER
    can_decide = (
        pending
        and is_user
        and check_decider(
            row.requested_by_user_id,
            me,
            "admin" if sees_all else None,
            True,
            uow.principal.is_agent,
            connection_owner_id=None if linked is None else linked.owner_user_id,
        )
        is None
    )
    return ApprovalDetail(
        **(await approval_named(uow, row)).model_dump(),
        grant_diff=await _diff(uow, row),
        connection=None
        if linked is None
        else ApprovalConnection(
            name=linked.name,
            classification=linked.classification,
            owner_user_id=linked.owner_user_id,
            ceiling_audience="org" if linked.ceiling.subjects is None else "subjects",
            ceiling_subjects=len(linked.ceiling.subjects or ()),
        ),
        can_decide=can_decide,
        can_cancel=pending and is_user and row.requested_by_user_id == me,
    )


@router.get(
    "/approvals",
    response_model=ApprovalPage,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def list_approvals(params: Annotated[ApprovalQuery, Query()], uow: UserUoW) -> ApprovalPage:
    """Newest first. Org admins and operators see every request; others see their own and the
    `exceed_ceiling` requests on connections they own. `inbox` narrows to what the caller may
    decide. An operator's read is audited as ``operator.access``."""
    sees_all = await sees_every_request(uow)
    me = uow.principal.subject
    if params.inbox and (uow.principal.kind is not PrincipalKind.USER or uow.principal.is_agent):
        return ApprovalPage(approvals=[], next_before=None)
    before: ApprovalRow | None = None
    if params.before is not None:
        before = await service.get(uow.conn, org_id=uow.org_id, approval_id=params.before)
        if before is None or not await _visible(uow, before, sees_all):
            raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"before": params.before})
    rows = await service.search(
        uow.conn,
        org_id=uow.org_id,
        visible_to=None if sees_all else me,
        decidable_by=me if params.inbox else None,
        decider_is_admin=sees_all,
        state=params.state,
        environment_id=params.environment_id,
        before=before,
        limit=params.limit + 1,
    )
    page = rows[: params.limit]
    if uow.principal.kind is PrincipalKind.OPERATOR:
        await uow.audit(AuditAction.OPERATOR_ACCESS, target_kind="org", target_id=uow.org_id)
    return ApprovalPage(
        approvals=await approvals_out(uow, page),
        next_before=page[-1].id if len(rows) > params.limit else None,
    )


@router.get(
    "/approvals/{approval_id}",
    response_model=ApprovalDetail,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def get_approval(approval_id: Id, uow: UserUoW) -> ApprovalDetail:
    """One request, with the names, who asked and what it would change. A request the caller
    may not see is ``NOT_FOUND``."""
    sees_all = await sees_every_request(uow)
    row = await service.get(uow.conn, org_id=uow.org_id, approval_id=approval_id)
    if row is None or not await _visible(uow, row, sees_all):
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"approval_id": approval_id})
    if uow.principal.kind is PrincipalKind.OPERATOR:
        await uow.audit(
            AuditAction.OPERATOR_ACCESS, target_kind="approval_request", target_id=approval_id
        )
    return await _detail(uow, row, sees_all)


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
    the approver is not an active org admin."""
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
    return uow.reply(await approval_named(uow, row))


@router.post(
    "/approvals/{approval_id}/decide",
    response_model=ApprovalDecided,
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
async def decide_as_approver(approval_id: Id, body: PersonDecisionIn, uow: UserUoW) -> Response:
    """Approve or reject a request you can see, with a reason. Never in an agent session; an org
    admin, their own requests included, or for `exceed_ceiling` the connection's owner, not on
    their own. Approving a sharing request applies it once every requirement for that change is
    approved; approving a `connect_data_source` request links its environment. One that no
    longer fits stays approved and says so in `applied`; the requester asks again."""
    principal = uow.principal
    if principal.kind is not PrincipalKind.USER:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"kind": principal.kind.value})
    if principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    sees_all = await sees_every_request(uow)
    seen = await service.get(uow.conn, org_id=uow.org_id, approval_id=approval_id)
    if seen is None or not await _visible(uow, seen, sees_all):
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"approval_id": approval_id})
    if seen.kind in _SHARING:
        await lock_environment(uow, seen)
    decider = Decider(
        user_id=principal.subject,
        via_agent=False,
        recorded_by_operator=None,
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
    applied, reason = await apply_approved(uow, row)
    return uow.reply(
        ApprovalDecided(
            **(await approval_named(uow, row)).model_dump(), applied=applied, applied_reason=reason
        )
    )


@router.post(
    "/approvals/{approval_id}/cancel",
    response_model=Approval,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND, ErrorCode.APPROVAL_NOT_PENDING
    ),
)
async def cancel_approval(approval_id: Id, body: CancelIn, uow: UserUoW) -> Response:
    """Withdraw your own pending request. Allowed in an agent session for its own request.
    Audited as `approval.cancelled`."""
    principal = uow.principal
    if principal.kind is not PrincipalKind.USER:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"kind": principal.kind.value})
    sees_all = await sees_every_request(uow)
    seen = await service.get(uow.conn, org_id=uow.org_id, approval_id=approval_id)
    if seen is None or not await _visible(uow, seen, sees_all):
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"approval_id": approval_id})
    try:
        row = await service.cancel(
            uow.conn,
            org_id=uow.org_id,
            approval_id=approval_id,
            user_id=principal.subject,
            channel=body.channel,
            reason=body.reason,
            actor=actor_of(principal),
        )
    except ApprovalRefusedError as e:
        raise Refusal(_REFUSALS[e.reason], evidence={"approval_id": approval_id}) from None
    return uow.reply(await approval_named(uow, row))
