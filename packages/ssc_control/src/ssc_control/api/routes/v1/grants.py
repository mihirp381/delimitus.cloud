"""Sharing rules of one environment, behind ``If-Match`` (SSC-021, decision 019).

Only an active org admin, the app's owner, or a builder on the environment may change them.
Grants must meet the environment's floor (``domain.grant_rules``): preview is for builders.
A change that adds or removes a grant marks the org's access snapshot dirty and pauses the
schedules whose declarer may no longer build (decision 020).

A change made through an agent credential, and a change that widens the audience of a
data-connected app, applies only once the matching approval is approved (decision 016). So does a
change that widens the audience beyond the ceiling of a data connection the environment uses
(``exceed_ceiling``, SSC-052), decided by the connection's owner or an org admin. Until then an
agent session gets ``202`` with the pending approval ids and a person gets ``APPROVAL_REQUIRED``;
the grants and their version stay as they were.

Each grant a change actually adds records one ``share`` metrics event (SSC-028).
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Response
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.authz import require_builder
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import (
    ETAG,
    IF_MATCH,
    Id,
    Strict,
    etag,
    parse_if_match,
    require_user,
)
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.approvals.capabilities import RecordedCapabilities
from ssc_control.approvals.service import ApprovalRow, newest, request, share_requirements
from ssc_control.connections import service as connections
from ssc_control.domain import grant_rules
from ssc_control.domain.approval_rules import (
    GrantKey,
    Requirement,
    RequirementKind,
    exceed_subject_key,
)
from ssc_control.domain.audience import Ceiling
from ssc_control.metrics.source_tool import SOURCE_TOOL_HEADER, source_tool_of
from ssc_control.ports import MetricKind
from ssc_control.snapshot.service import mark_dirty
from ssc_control.timers.service import pause_blocked

router = APIRouter()


class GrantIn(Strict):
    role: Literal["builder", "user"]
    subject_kind: Literal["user", "group", "org"]
    subject_id: str | None = Field(
        default=None, description="A `usr_` or `grp_` id; absent when subject_kind is `org`."
    )


class GrantsIn(Strict):
    grants: list[GrantIn] = Field(max_length=200)


class GrantOut(Strict):
    id: str
    role: Literal["builder", "user"]
    subject_kind: Literal["user", "group", "org"]
    subject_id: str | None


class GrantsOut(Strict):
    environment_id: str
    grants_version: int
    grants: list[GrantOut]


class GrantsPending(Strict):
    """An agent session's change, waiting for another admin. Nothing was applied."""

    environment_id: str
    grants_version: int = Field(description="Unchanged: the version the change will replace.")
    approval_ids: list[str] = Field(description="Retry the same change once all are approved.")


_LOCK_ENV = text(
    "select id, grants_version, profile, name from ssc.environment "
    "where org_id = :org and app_id = :app and id = :env for update"
)
_SHARE_APP_STATUS = text("select status from ssc.app where org_id = :org and id = :app for share")
_SELECT_ENV = text(
    "select id, grants_version from ssc.environment "
    "where org_id = :org and app_id = :app and id = :env"
)
_SELECT_GRANTS = text(
    "select id, role, subject_kind, user_id, group_id from ssc.app_grant "
    "where org_id = :org and environment_id = :env order by role, subject_kind, user_id, group_id"
)
_DELETE_GRANT = text("delete from ssc.app_grant where org_id = :org and id = :id")
_INSERT_GRANT = text(
    "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, user_id, group_id, "
    "granted_by_user_id) values (:id, :org, :env, :role, :kind, :user, :group, :by)"
)
_BUMP_GRANTS = text(
    "update ssc.environment set grants_version = grants_version + 1 "
    "where org_id = :org and id = :env returning grants_version"
)


def _grant_out(row: dict[str, Any]) -> GrantOut:
    return GrantOut(
        id=str(row["id"]),
        role=row["role"],
        subject_kind=row["subject_kind"],
        subject_id=row["user_id"] or row["group_id"],
    )


def _grant_key(g: GrantIn | GrantOut) -> GrantKey:
    return (g.role, g.subject_kind, g.subject_id)


def _grants_payload(grants: dict[GrantKey, GrantIn]) -> list[dict[str, Any]]:
    return [
        grants[k].model_dump(mode="json")
        for k in sorted(grants, key=lambda k: (k[0], k[1], k[2] or ""))
    ]


async def _approvals_for(  # noqa: PLR0913  (keyword-only)
    uow: UnitOfWork,
    *,
    environment_id: str,
    profile: str,
    version: int,
    existing: set[GrantKey],
    desired: dict[GrantKey, GrantIn],
    exceeded: tuple[str, ...],
) -> tuple[list[Requirement], dict[Requirement, ApprovalRow]]:
    """What this change needs approved, and the newest request for each."""
    needed = await share_requirements(
        uow.conn,
        org_id=uow.org_id,
        environment_id=environment_id,
        profile=profile,
        base_version=version,
        before=existing,
        after=set(desired),
        via_agent=uow.principal.is_agent,
        source=RecordedCapabilities(),
        exceeded=exceeded,
    )
    found = await newest(
        uow.conn, org_id=uow.org_id, environment_id=environment_id, requirements=needed
    )
    return needed, found


def _check_rules(
    target: grant_rules.SharingTarget,
    desired: dict[GrantKey, GrantIn],
    ceilings: dict[str, Ceiling],
) -> tuple[str, ...]:
    """Floors and one grant per subject, then the connections whose ceiling ``desired`` exceeds."""
    problems = grant_rules.validate(target.name, desired)
    if problems:
        raise Refusal(
            ErrorCode.VALIDATION_FAILED,
            evidence={"problems": [{"problem": p.problem, "grant": p.grant} for p in problems]},
        )
    return grant_rules.audience_ceiling(target, set(desired), ceilings)


async def _ask(  # noqa: PLR0913  (keyword-only)
    uow: UnitOfWork,
    environment_id: str,
    open_: list[Requirement],
    *,
    desired: dict[GrantKey, GrantIn],
    version: int,
    exceeded: tuple[str, ...],
) -> list[str]:
    """Open (or find) the approval request for each open requirement; their ids."""
    asked: list[str] = []
    names = {exceed_subject_key(n, set(desired)): n for n in exceeded}
    for req in open_:
        payload: dict[str, Any] = {"grants": _grants_payload(desired)}
        if req.kind is RequirementKind.AGENT_SHARE:
            payload["grants_version"] = version
        if req.kind is RequirementKind.EXCEED_CEILING:
            payload["connection"] = names[req.subject_key]
        row, _ = await request(
            uow.conn,
            org_id=uow.org_id,
            environment_id=environment_id,
            requirement=req,
            requested_by=uow.principal.subject,
            via_agent=True,
            payload=payload,
            actor=actor_of(uow.principal),
        )
        asked.append(row.id)
    return asked


async def _grants_out(uow: UnitOfWork, env_id: str, version: int) -> GrantsOut:
    rows = (await uow.conn.execute(_SELECT_GRANTS, {"org": uow.org_id, "env": env_id})).mappings()
    return GrantsOut(
        environment_id=env_id, grants_version=version, grants=[_grant_out(dict(r)) for r in rows]
    )


@router.get(
    "/apps/{app_id}/environments/{environment_id}/grants",
    response_model=GrantsOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.NOT_FOUND),
)
async def get_grants(app_id: Id, environment_id: Id, uow: UserUoW, response: Response) -> GrantsOut:
    env = (
        await uow.conn.execute(
            _SELECT_ENV, {"org": uow.org_id, "app": app_id, "env": environment_id}
        )
    ).first()
    if env is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    version = int(env[1])
    response.headers[ETAG] = etag(version)
    return await _grants_out(uow, environment_id, version)


@router.put(
    "/apps/{app_id}/environments/{environment_id}/grants",
    response_model=GrantsOut,
    responses={
        202: {
            "model": GrantsPending,
            "description": "Made through an agent credential, or widening a data-connected app: "
            "waiting for approval. Nothing changed.",
        },
        **problem_responses(
            *AUTHENTICATED,
            ErrorCode.NOT_FOUND,
            ErrorCode.FORBIDDEN,
            ErrorCode.PRECONDITION_REQUIRED,
            ErrorCode.PRECONDITION_STALE,
            ErrorCode.REFERENCE_NOT_FOUND,
            ErrorCode.VALIDATION_FAILED,
            ErrorCode.APPROVAL_REQUIRED,
            ErrorCode.APP_NOT_ACTIVE,
        ),
    },
)
async def put_grants(  # noqa: PLR0913  (FastAPI maps each parameter to the request)
    *,
    app_id: Id,
    environment_id: Id,
    body: GrantsIn,
    uow: UserUoW,
    if_match: Annotated[str | None, Header(alias=IF_MATCH)] = None,
    source_tool: Annotated[
        str | None,
        Header(
            alias=SOURCE_TOOL_HEADER,
            description="The builder tool making the change, for product metrics. "
            "An agent credential's client id takes precedence.",
        ),
    ] = None,
) -> Response:
    """Replace the sharing rules of one environment. Requires ``If-Match`` with the current ETag.

    Only an org admin, the app's owner or a builder on this environment may; anyone else gets
    ``FORBIDDEN``. A grant below the environment's floor (``user`` on preview) or a second grant
    for one subject is ``VALIDATION_FAILED``. A quarantined app's sharing is frozen:
    ``APP_NOT_ACTIVE``.

    A change that needs approval is not applied: an agent session gets ``202`` and the pending
    approval ids (asked for here); a person gets ``APPROVAL_REQUIRED`` naming what to ask for."""
    expected = parse_if_match(if_match)
    by = require_user(uow)
    for g in body.grants:
        if (g.subject_kind == "org") != (g.subject_id is None):
            raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"grant": g.model_dump()})
    env = (
        await uow.conn.execute(_LOCK_ENV, {"org": uow.org_id, "app": app_id, "env": environment_id})
    ).first()
    if env is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    await require_builder(uow, environment_id)
    # Sharing freeze (SSC-025). FOR SHARE waits for a kill switch pulled at the same moment.
    status = (
        await uow.conn.execute(_SHARE_APP_STATUS, {"org": uow.org_id, "app": app_id})
    ).scalar()
    if status == "quarantined":
        raise Refusal(ErrorCode.APP_NOT_ACTIVE, evidence={"app_id": app_id, "status": status})
    current = int(env[1])
    if current != expected:
        raise Refusal(
            ErrorCode.PRECONDITION_STALE, evidence={"expected": expected, "current": current}
        )
    existing = {_grant_key(g): g for g in (await _grants_out(uow, environment_id, current)).grants}
    desired = {_grant_key(g): g for g in body.grants}
    ceilings = await connections.ceilings(uow.conn, uow.org_id, environment_id, set(desired))
    exceeded = _check_rules(
        grant_rules.SharingTarget(environment_id, str(env[3]), str(env[2])), desired, ceilings
    )
    needed, found = await _approvals_for(
        uow,
        environment_id=environment_id,
        profile=str(env[2]),
        version=current,
        existing=set(existing),
        desired=desired,
        exceeded=exceeded,
    )
    open_ = [r for r in needed if r not in found or found[r].state != "approved"]
    if open_ and uow.principal.is_agent:
        asked = await _ask(
            uow, environment_id, open_, desired=desired, version=current, exceeded=exceeded
        )
        pending = GrantsPending(
            environment_id=environment_id, grants_version=current, approval_ids=asked
        )
        return uow.reply(pending, status=202, headers={ETAG: etag(current)})
    if open_:
        raise Refusal(
            ErrorCode.APPROVAL_REQUIRED,
            evidence={
                "environment_id": environment_id,
                "requirements": [
                    {
                        "kind": r.kind.value,
                        "subject_key": r.subject_key,
                        "approval_id": found[r].id if r in found else None,
                        "state": found[r].state if r in found else None,
                    }
                    for r in open_
                ],
            },
        )
    # The approval that let the change through, agent_share first, is linked from each grant row.
    approved = sorted(needed, key=lambda r: r.kind is not RequirementKind.AGENT_SHARE)
    policy_id = found[approved[0]].policy_decision_id if approved else None
    changed = existing.keys() != desired.keys()
    for key, old in existing.items():
        if key not in desired:
            await uow.conn.execute(_DELETE_GRANT, {"org": uow.org_id, "id": old.id})
            await uow.audit(
                AuditAction.GRANT_REMOVED,
                target_kind="app_grant",
                target_id=old.id,
                before={"environment_id": environment_id, **old.model_dump(exclude={"id"})},
                policy_decision_id=policy_id,
            )
    for key, new in desired.items():
        if key not in existing:
            gid = new_id("gnt")
            await uow.conn.execute(
                _INSERT_GRANT,
                {
                    "id": gid,
                    "org": uow.org_id,
                    "env": environment_id,
                    "role": new.role,
                    "kind": new.subject_kind,
                    "user": new.subject_id if new.subject_kind == "user" else None,
                    "group": new.subject_id if new.subject_kind == "group" else None,
                    "by": by,
                },
            )
            await uow.audit(
                AuditAction.GRANT_ADDED,
                target_kind="app_grant",
                target_id=gid,
                after={"environment_id": environment_id, **new.model_dump()},
                policy_decision_id=policy_id,
            )
            await uow.metrics.record_event(
                uow.conn,
                org_id=uow.org_id,
                kind=MetricKind.SHARE,
                app_id=app_id,
                user_id=by,
                source_tool=source_tool_of(uow.principal, source_tool),
                properties={
                    "environment": str(env[3]),
                    "role": new.role,
                    "subject_kind": new.subject_kind,
                    "via_agent": uow.principal.is_agent,
                },
            )
    bumped = (
        await uow.conn.execute(_BUMP_GRANTS, {"org": uow.org_id, "env": environment_id})
    ).scalar_one()
    version = int(bumped)
    if changed:
        approved_over = (
            set(exceeded)
            if any(r.kind is RequirementKind.EXCEED_CEILING for r in needed)
            else set[str]()
        )
        await connections.settle_environment(
            uow.conn,
            uow.org_id,
            environment_id,
            approved=approved_over,
            actor=actor_of(uow.principal),
        )
        await mark_dirty(uow.conn, uow.org_id)
        await pause_blocked(uow.conn, uow.org_id)
    return uow.reply(await _grants_out(uow, environment_id, version), headers={ETAG: etag(version)})
