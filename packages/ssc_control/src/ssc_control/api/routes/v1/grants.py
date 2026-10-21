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
the grants and their version stay as they were. ``POST .../grants/requirements`` runs the same check
on the same grants and says which approvals are missing, writing nothing (K-1).

Approving the last open requirement of a change applies it (:func:`apply_approved`, SSC-049).
Approving a ``connect_data_source`` request links its environment to the connection.

Each grant a change actually adds records one ``share`` metrics event (SSC-028).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, Self

from fastapi import APIRouter, Header, Response
from pydantic import Field, StringConstraints, ValidationError, model_validator
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.authz import can_build, require_builder
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
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

type ApplyOutcome = Literal["applied", "waiting", "not_applied", "not_applicable"]

_SHARING = frozenset(
    {RequirementKind.WIDEN_AUDIENCE, RequirementKind.AGENT_SHARE, RequirementKind.EXCEED_CEILING}
)


SubjectId = Annotated[str, StringConstraints(pattern=r"^(usr|grp)_[a-z0-9]{20}$")]
_SUBJECT_KIND_OF: Final = {"usr": "user", "grp": "group"}


class GrantIn(Strict):
    role: Literal["builder", "user"]
    subject_kind: Literal["user", "group", "org"]
    subject_id: SubjectId | None = Field(
        default=None, description="A `usr_` or `grp_` id; absent when subject_kind is `org`."
    )

    @model_validator(mode="after")
    def _id_names_its_kind(self) -> Self:
        if self.subject_id is not None and (
            _SUBJECT_KIND_OF[self.subject_id[:3]] != self.subject_kind
        ):
            raise ValueError("a usr_ id names a user and a grp_ id a group")
        return self


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


class RequirementOut(Strict):
    """One approval a sharing change needs, and the newest request for it if there is one."""

    kind: str = Field(description="`agent_share`, `widen_audience` or `exceed_ceiling`.")
    connection: str | None = Field(
        description="The connection whose ceiling the change passes, for `exceed_ceiling`; "
        "null otherwise."
    )
    approval_id: str | None = Field(description="The newest request for it; null if never asked.")
    state: str | None = Field(description="That request's state; null if never asked.")


class GrantsRequirements(Strict):
    """What a change to these grants needs approved before ``PUT .../grants`` applies it."""

    environment_id: str
    grants_version: int = Field(description="The version the answer was computed against.")
    requirements: list[RequirementOut] = Field(
        description="Each approval not yet approved; empty when the change would apply now."
    )


class GrantsPending(Strict):
    """An agent session's change, waiting for another admin. Nothing was applied."""

    environment_id: str
    grants_version: int = Field(description="Unchanged: the version the change will replace.")
    approval_ids: list[str] = Field(description="Retry the same change once all are approved.")
    requirements: list[RequirementOut] = Field(
        default_factory=list[RequirementOut],
        description="What each approval in `approval_ids` is for, in the same order.",
    )


_LOCK_ENV = text(
    "select id, grants_version, profile, name from ssc.environment "
    "where org_id = :org and app_id = :app and id = :env for update"
)
_SHARE_APP_STATUS = text("select status from ssc.app where org_id = :org and id = :app for share")
_SHARING_FROZEN: Final = frozenset({"quarantined", "deleting", "deleted"})
"""The statuses in which an app's sharing takes no change: quarantined (SSC-025), or going."""
_READ_ENV = text(
    "select id, grants_version, profile, name from ssc.environment "
    "where org_id = :org and app_id = :app and id = :env"
)
_APP_STATUS = text("select status from ssc.app where org_id = :org and id = :app")
_SELECT_ENV = text(
    "select id, grants_version from ssc.environment "
    "where org_id = :org and app_id = :app and id = :env"
)
_SELECT_GRANTS = text(
    "select id, role, subject_kind, user_id, group_id from ssc.app_grant "
    "where org_id = :org and environment_id = :env order by role, subject_kind, user_id, group_id"
)
_DELETE_GRANT = text("delete from ssc.app_grant where org_id = :org and id = :id")
_SELECT_SUBJECTS = text(
    "select id from ssc.user_account where org_id = :org and id = any(:users) "
    "union all select id from ssc.user_group where org_id = :org and id = any(:groups)"
)
_INSERT_GRANT = text(
    "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, user_id, group_id, "
    "granted_by_user_id) values (:id, :org, :env, :role, :kind, :user, :group, :by)"
)
_BUMP_GRANTS = text(
    "update ssc.environment set grants_version = grants_version + 1 "
    "where org_id = :org and id = :env returning grants_version"
)


LOCK_PREVIEW = text(
    "select id from ssc.environment where org_id = :org and app_id = :app and name = 'preview' "
    "for update"
)
_USER_GRANT = text(
    "select 1 from ssc.app_grant where org_id = :org and environment_id = :env and user_id = :user"
)


async def grant_owner_preview(uow: UnitOfWork, app_id: str, owner: str) -> bool:
    """A builder grant for ``owner`` on the app's preview unless they hold one (GA-12 F9b)."""
    env = (await uow.conn.execute(LOCK_PREVIEW, {"org": uow.org_id, "app": app_id})).scalar_one()
    params = {"org": uow.org_id, "env": env, "user": owner}
    if (await uow.conn.execute(_USER_GRANT, params)).first() is not None:
        return False
    gid = new_id("gnt")
    await uow.conn.execute(
        _INSERT_GRANT,
        {
            "id": gid,
            "org": uow.org_id,
            "env": env,
            "role": "builder",
            "kind": "user",
            "user": owner,
            "group": None,
            "by": owner,
        },
    )
    await uow.conn.execute(_BUMP_GRANTS, {"org": uow.org_id, "env": env})
    await uow.audit(
        AuditAction.GRANT_ADDED,
        target_kind="app_grant",
        target_id=gid,
        after={
            "environment_id": env,
            "role": "builder",
            "subject_kind": "user",
            "subject_id": owner,
        },
    )
    return True


def _grant_out(row: dict[str, Any]) -> GrantOut:
    return GrantOut(
        id=str(row["id"]),
        role=row["role"],
        subject_kind=row["subject_kind"],
        subject_id=row["user_id"] or row["group_id"],
    )


def _grant_key(g: GrantIn | GrantOut) -> GrantKey:
    return (g.role, g.subject_kind, g.subject_id)


async def missing_subjects(
    uow: UnitOfWork, subjects: Iterable[tuple[str, str | None]]
) -> list[str]:
    """The user and group ids among ``(subject_kind, subject_id)`` pairs not in the caller's org."""
    wanted = {(kind, sid) for kind, sid in subjects if sid is not None}
    if not wanted:
        return []
    params = {
        "org": uow.org_id,
        "users": [sid for kind, sid in wanted if kind == "user"],
        "groups": [sid for kind, sid in wanted if kind == "group"],
    }
    found = set((await uow.conn.execute(_SELECT_SUBJECTS, params)).scalars())
    return sorted({sid for _, sid in wanted} - found)


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
    via_agent: bool,
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
        via_agent=via_agent,
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
) -> list[tuple[Requirement, ApprovalRow]]:
    """Open (or find) the approval request for each open requirement."""
    asked: list[tuple[Requirement, ApprovalRow]] = []
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
        asked.append((req, row))
    return asked


async def _write(  # noqa: PLR0913  (keyword-only)
    uow: UnitOfWork,
    *,
    app_id: str,
    environment_id: str,
    environment_name: str,
    existing: dict[GrantKey, GrantOut],
    desired: dict[GrantKey, GrantIn],
    needed: list[Requirement],
    found: dict[Requirement, ApprovalRow],
    exceeded: tuple[str, ...],
    by: str,
    via_agent: bool,
    source_tool: str | None,
) -> int:
    """Replace the environment's grants with ``desired`` in the caller's transaction, once every
    requirement in ``needed`` is approved in ``found``. Returns the new ``grants_version``."""
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
                source_tool=source_tool,
                properties={
                    "environment": environment_name,
                    "role": new.role,
                    "subject_kind": new.subject_kind,
                    "via_agent": via_agent,
                },
            )
    bumped = (
        await uow.conn.execute(_BUMP_GRANTS, {"org": uow.org_id, "env": environment_id})
    ).scalar_one()
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
    return int(bumped)


async def lock_environment(uow: UnitOfWork, row: ApprovalRow) -> None:
    """Take the request's environment ``FOR UPDATE``, the order :func:`put_grants` locks in:
    environment before app, everywhere; an insert with a foreign key to a row takes a KEY SHARE
    lock on it and counts."""
    params = {"org": uow.org_id, "app": row.app_id, "env": row.environment_id}
    await uow.conn.execute(_LOCK_ENV, params)


def _stored_grants(row: ApprovalRow) -> dict[GrantKey, GrantIn] | None:
    try:
        grants = GrantsIn.model_validate({"grants": row.payload.get("grants")}).grants
    except ValidationError:
        return None
    return {_grant_key(g): g for g in grants}


async def _blocked(
    uow: UnitOfWork, row: ApprovalRow, desired: dict[GrantKey, GrantIn] | None
) -> str | None:
    """Why an approved change may not be written now: nothing stored to write, the sharing of
    a quarantined app, or of one being deleted, is frozen, or the requester may no longer build
    the environment."""
    if desired is None:
        return "payload_invalid"
    params = {"org": uow.org_id, "app": row.app_id}
    if (await uow.conn.execute(_SHARE_APP_STATUS, params)).scalar() in _SHARING_FROZEN:
        return "app_not_active"
    if not await can_build(uow.conn, uow.org_id, row.requested_by_user_id, row.environment_id):
        return "requester_cannot_build"
    return None


async def _settles_flag(uow: UnitOfWork, row: ApprovalRow, same_grants: bool) -> bool:
    """Clear the flag an approved ``exceed_ceiling`` for the current grants answers; whether it
    did."""
    name = row.connection
    if name is None or not same_grants:
        return False
    links = await connections.links(uow.conn, uow.org_id, row.environment_id)
    if not any(k.connection.name == name and k.over_ceiling_since is not None for k in links):
        return False
    await connections.settle_environment(
        uow.conn, uow.org_id, row.environment_id, approved={name}, actor=actor_of(uow.principal)
    )
    return True


async def link_approved(uow: UnitOfWork, row: ApprovalRow) -> tuple[ApplyOutcome, str | None]:
    """Link an approved ``connect_data_source`` request's environment to its connection."""
    connection = await connections.get(uow.conn, uow.org_id, row.subject_key)
    if connection is None:
        return "not_applied", "connection_missing"
    held = await connections.links(uow.conn, uow.org_id, row.environment_id)
    if any(link.connection.name == row.subject_key for link in held):
        return "applied", None
    grants = await connections.environment_grants(uow.conn, uow.org_id, row.environment_id)
    if await connections.over_ceiling(uow.conn, uow.org_id, connection, grants):
        return "not_applied", "ceiling"
    await connections.grant(
        uow.conn,
        org_id=uow.org_id,
        actor=actor_of(uow.principal),
        connection=connection,
        environment_id=row.environment_id,
        limits={},
        by_user_id=row.decided_by_user_id or uow.principal.subject,
        policy_decision_id=row.policy_decision_id,
    )
    return "applied", None


async def apply_approved(uow: UnitOfWork, row: ApprovalRow) -> tuple[ApplyOutcome, str | None]:
    """After ``row`` was approved: write its stored grant set when every requirement for that
    exact set is now approved, in the caller's transaction. ``waiting`` while another is open;
    ``not_applied`` with the reason when the world moved on (version, floors, ceilings,
    quarantine, the requester's right to build); ``not_applicable`` for the kinds that apply
    nothing. The approval stands either way. An ``exceed_ceiling`` approval for exactly the
    current grants of an environment flagged over that connection's ceiling (a directory change
    opened it, GA-5.3) clears the flag and is ``applied``. An approved ``connect_data_source``
    links its environment unless the connection is gone or the audience exceeds its ceiling."""
    if row.state != "approved":
        return "not_applicable", None
    if row.kind is RequirementKind.CONNECT_DATA_SOURCE:
        return await link_approved(uow, row)
    if row.kind in _SHARING:
        return await _apply_grants(uow, row)
    return "not_applicable", None


async def _apply_grants(uow: UnitOfWork, row: ApprovalRow) -> tuple[ApplyOutcome, str | None]:
    desired = _stored_grants(row)
    params = {"org": uow.org_id, "app": row.app_id, "env": row.environment_id}
    env = (await uow.conn.execute(_LOCK_ENV, params)).one()
    blocked = await _blocked(uow, row, desired)
    if blocked is not None or desired is None:
        return "not_applied", blocked
    requester = row.requested_by_user_id
    current = int(env[1])
    existing = {
        _grant_key(g): g for g in (await _grants_out(uow, row.environment_id, current)).grants
    }
    ceilings = await connections.ceilings(uow.conn, uow.org_id, row.environment_id, set(desired))
    try:
        exceeded = _check_rules(
            grant_rules.SharingTarget(row.environment_id, str(env[3]), str(env[2])),
            desired,
            ceilings,
        )
    except Refusal:
        return "not_applied", "rules"
    needed, found = await _approvals_for(
        uow,
        environment_id=row.environment_id,
        profile=str(env[2]),
        version=current,
        existing=set(existing),
        desired=desired,
        exceeded=exceeded,
        via_agent=row.requested_via_agent,
    )
    if row.requirement not in needed:
        settled = await _settles_flag(uow, row, set(desired) == set(existing))
        return ("applied", None) if settled else ("not_applied", "stale")
    if any(r not in found or found[r].state != "approved" for r in needed):
        return "waiting", None
    await _write(
        uow,
        app_id=row.app_id,
        environment_id=row.environment_id,
        environment_name=str(env[3]),
        existing=existing,
        desired=desired,
        needed=needed,
        found=found,
        exceeded=exceeded,
        by=requester,
        via_agent=row.requested_via_agent,
        source_tool=None,
    )
    return "applied", None


@dataclass(frozen=True)
class _Assessed:
    """A change to one environment's grants, checked, and what it needs approved."""

    name: str
    current: int
    existing: dict[GrantKey, GrantOut]
    desired: dict[GrantKey, GrantIn]
    exceeded: tuple[str, ...]
    needed: list[Requirement]
    found: dict[Requirement, ApprovalRow]

    @property
    def open_(self) -> list[Requirement]:
        """The requirements without an approved request."""
        return [r for r in self.needed if r not in self.found or self.found[r].state != "approved"]

    def out(self, req: Requirement, row: ApprovalRow | None) -> RequirementOut:
        names = {exceed_subject_key(n, set(self.desired)): n for n in self.exceeded}
        return RequirementOut(
            kind=req.kind.value,
            connection=names.get(req.subject_key)
            if req.kind is RequirementKind.EXCEED_CEILING
            else None,
            approval_id=None if row is None else row.id,
            state=None if row is None else row.state,
        )


async def _assess(
    uow: UnitOfWork,
    app_id: str,
    environment_id: str,
    grants: list[GrantIn],
    *,
    expected: int | None,
) -> _Assessed:
    """The part of a grants change that ``PUT`` and ``POST .../requirements`` share, so the two
    never drift: who may, the freeze, the floors and ceilings, and the approvals needed. With
    ``expected`` (the PUT) the environment is locked ``FOR UPDATE`` and must be at that version;
    without it the rows are read without a lock."""
    for g in grants:
        if (g.subject_kind == "org") != (g.subject_id is None):
            raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"grant": g.model_dump()})
    params = {"org": uow.org_id, "app": app_id, "env": environment_id}
    env = (await uow.conn.execute(_READ_ENV if expected is None else _LOCK_ENV, params)).first()
    if env is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    await require_builder(uow, environment_id)
    if missing := await missing_subjects(uow, ((g.subject_kind, g.subject_id) for g in grants)):
        raise Refusal(ErrorCode.REFERENCE_NOT_FOUND, evidence={"subject_ids": missing})
    # Sharing freeze (SSC-025). FOR SHARE waits for a kill switch pulled at the same moment,
    # or a removal asked for (GA-12).
    app_status = _APP_STATUS if expected is None else _SHARE_APP_STATUS
    status = (await uow.conn.execute(app_status, params)).scalar()
    if status in _SHARING_FROZEN:
        raise Refusal(ErrorCode.APP_NOT_ACTIVE, evidence={"app_id": app_id, "status": status})
    current = int(env[1])
    if expected is not None and current != expected:
        raise Refusal(
            ErrorCode.PRECONDITION_STALE, evidence={"expected": expected, "current": current}
        )
    existing = {_grant_key(g): g for g in (await _grants_out(uow, environment_id, current)).grants}
    desired = {_grant_key(g): g for g in grants}
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
        via_agent=uow.principal.is_agent,
    )
    return _Assessed(str(env[3]), current, existing, desired, exceeded, needed, found)


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
    for one subject is ``VALIDATION_FAILED``, and one naming a user or group the org does not have
    ``REFERENCE_NOT_FOUND``. A quarantined app's sharing is frozen: ``APP_NOT_ACTIVE``.

    A change that needs approval is not applied. An agent session gets ``202`` and the pending
    approval ids (asked for here). A person gets ``409 APPROVAL_REQUIRED``, whose body names
    nothing (the ``Problem`` shape is fixed): they ask ``POST .../grants/requirements`` with the
    same grants which approvals are needed, open one ``POST /v1/approvals`` per requirement
    (``widen_audience`` with ``payload.grants``; ``exceed_ceiling`` with
    ``payload.{connection, grants}``), and repeat this ``PUT`` once all are approved."""
    expected = parse_if_match(if_match)
    by = require_user(uow)
    change = await _assess(uow, app_id, environment_id, body.grants, expected=expected)
    current, open_ = change.current, change.open_
    if open_ and uow.principal.is_agent:
        asked = await _ask(
            uow,
            environment_id,
            open_,
            desired=change.desired,
            version=current,
            exceeded=change.exceeded,
        )
        pending = GrantsPending(
            environment_id=environment_id,
            grants_version=current,
            approval_ids=[row.id for _, row in asked],
            requirements=[change.out(req, row) for req, row in asked],
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
                        "approval_id": change.found[r].id if r in change.found else None,
                        "state": change.found[r].state if r in change.found else None,
                    }
                    for r in open_
                ],
            },
        )
    version = await _write(
        uow,
        app_id=app_id,
        environment_id=environment_id,
        environment_name=change.name,
        existing=change.existing,
        desired=change.desired,
        needed=change.needed,
        found=change.found,
        exceeded=change.exceeded,
        by=by,
        via_agent=uow.principal.is_agent,
        source_tool=source_tool_of(uow.principal, source_tool),
    )
    return uow.reply(await _grants_out(uow, environment_id, version), headers={ETAG: etag(version)})


@router.post(
    "/apps/{app_id}/environments/{environment_id}/grants/requirements",
    response_model=GrantsRequirements,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.NOT_FOUND,
        ErrorCode.FORBIDDEN,
        ErrorCode.REFERENCE_NOT_FOUND,
        ErrorCode.APP_NOT_ACTIVE,
    ),
)
async def grants_requirements(
    app_id: Id, environment_id: Id, body: GrantsIn, uow: UserUoW
) -> Response:
    """Which approvals replacing the environment's grants with ``grants`` needs, before
    ``PUT .../grants`` would apply it. Same rules, refusals and computation as that ``PUT``
    (without ``If-Match``); it writes nothing and opens no approval. Each requirement not yet
    approved is listed with the newest request for it, if any; ask for the others with
    ``POST /v1/approvals``. An agent session gets the same answer."""
    require_user(uow)
    change = await _assess(uow, app_id, environment_id, body.grants, expected=None)
    answer = GrantsRequirements(
        environment_id=environment_id,
        grants_version=change.current,
        requirements=[change.out(r, change.found.get(r)) for r in change.open_],
    )
    return uow.reply(answer)
