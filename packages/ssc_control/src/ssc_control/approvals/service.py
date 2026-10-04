"""Approval requests: open one, decide one, and find the newest answer to a question.

A question is ``(environment, kind, subject_key)``. At most one request per question is pending
(a partial unique index); asking again while one is pending or approved returns that request.
Deciding locks the request, checks the decider (never an agent session, never the requester,
an active org admin, or for ``exceed_ceiling`` the connection's active owner), writes a
``policy_decision`` and audits ``approval.decided``. The requester may withdraw (:func:`cancel`).
Each new request and each decision queues its mail (SSC-049, ``notifications``). An approved
internet host or data source turns on the cell's ``egress`` or ``connections`` (SSC-087), and an
approved internet host joins the org's egress allowlist (SSC-053).
"""

import json
import logging
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    column,
    exists,
    func,
    literal,
    or_,
    select,
    table,
    text,
    true,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import AuditAction
from ssc_contracts.ids import new_id
from ssc_control.approvals.capabilities import CapabilitySource
from ssc_control.approvals.policy import record_policy_decision
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.cell.resources import on_approval
from ssc_control.domain.approval_rules import (
    ApprovalState,
    GrantKey,
    Requirement,
    RequirementKind,
    agent_share_needs_approval,
    check_decider,
    exceed_subject_key,
    widening_needs_approval,
    widens,
)
from ssc_control.egress import allowlist
from ssc_control.notifications import service as notifications

log = logging.getLogger(__name__)

DecisionChannel = Literal["email", "chat", "console", "cli"]
DecisionOutcome = Literal["approved", "denied"]
RefusalReason = Literal[
    "not_found", "not_pending", "agent_session", "self_approval", "not_eligible", "not_requester"
]

DECIDE_ACTION: Final = "approval.decide"
_STATES: Final[frozenset[str]] = frozenset({"pending", "approved", "denied", "cancelled"})
_ATTEMPTS: Final = 3

_R: Final = table(
    "approval_request",
    column("org_id"),
    column("id"),
    column("environment_id"),
    column("kind"),
    column("subject_key"),
    column("payload"),
    column("state"),
    column("requested_by_user_id"),
    column("requested_via_agent"),
    column("decided_by_user_id"),
    column("decided_at"),
    column("decision_reason"),
    column("decision_channel"),
    column("recorded_by_operator"),
    column("policy_decision_id"),
    column("created_at"),
    schema="ssc",
)
_E: Final = table(
    "environment", column("org_id"), column("id"), column("app_id"), column("name"), schema="ssc"
)
_A: Final = table("app", column("org_id"), column("id"), column("slug"), schema="ssc")
_U: Final = table(
    "user_account", column("org_id"), column("id"), column("display_name"), schema="ssc"
)
_C: Final = table(
    "connection", column("org_id"), column("name"), column("owner_user_id"), schema="ssc"
)


def _select() -> Select[Any]:
    """Approval rows with the app, environment and requester names; callers add the ``org_id``
    filter and the rest."""
    r = _R.c
    return select(
        r.id,
        _E.c.app_id,
        _A.c.slug.label("app"),
        _E.c.name.label("environment"),
        _U.c.display_name.label("requested_by_name"),
        r.environment_id,
        r.kind,
        r.subject_key,
        r.payload,
        r.state,
        r.requested_by_user_id,
        r.requested_via_agent,
        r.decided_by_user_id,
        r.decided_at,
        r.decision_reason,
        r.decision_channel,
        r.recorded_by_operator,
        r.policy_decision_id,
        r.created_at,
    ).select_from(
        _R.join(_E, and_(_E.c.org_id == r.org_id, _E.c.id == r.environment_id))
        .join(_A, and_(_A.c.org_id == _E.c.org_id, _A.c.id == _E.c.app_id))
        .join(_U, and_(_U.c.org_id == r.org_id, _U.c.id == r.requested_by_user_id))
    )


_INSERT_PENDING: Final = text(
    "insert into ssc.approval_request (id, org_id, environment_id, kind, subject_key, payload, "
    "requested_by_user_id, requested_via_agent) values (:id, :org, :env, :kind, :key, "
    "cast(:payload as jsonb), :by, :via_agent) "
    "on conflict (org_id, environment_id, kind, subject_key) where state = 'pending' do nothing "
    "returning id"
)
_LOCK_DECIDER: Final = text(
    "select role, status from ssc.user_account where org_id = :org and id = :id for share"
)
_CONNECTION_OWNER: Final = text(
    "select owner_user_id from ssc.connection where org_id = :org and name = :name"
)
_DECIDE: Final = text(
    "update ssc.approval_request set state = :state, decided_by_user_id = :by, "
    "decided_at = now(), decided_via_agent = :via_agent, decision_reason = :reason, "
    "decision_channel = :channel, recorded_by_operator = :operator, policy_decision_id = :pol "
    "where org_id = :org and id = :id and state = 'pending'"
)


_CANCEL: Final = text(
    "update ssc.approval_request set state = 'cancelled', decided_by_user_id = :by, "
    "decided_at = now(), decision_reason = :reason, decision_channel = :channel "
    "where org_id = :org and id = :id and state = 'pending'"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRow:
    id: str
    app_id: str
    app: str
    environment_id: str
    environment: str
    kind: RequirementKind
    subject_key: str
    payload: dict[str, Any]
    state: ApprovalState
    requested_by_user_id: str
    requested_by_name: str
    requested_via_agent: bool
    decided_by_user_id: str | None
    decided_at: datetime | None
    decision_reason: str | None
    decision_channel: DecisionChannel | None
    recorded_by_operator: str | None
    policy_decision_id: str | None
    created_at: datetime

    @property
    def requirement(self) -> Requirement:
        return Requirement(self.kind, self.subject_key)

    @property
    def connection(self) -> str | None:
        """The connection an ``exceed_ceiling`` request names; None for every other kind."""
        name = self.payload.get("connection")
        return (
            name if self.kind is RequirementKind.EXCEED_CEILING and isinstance(name, str) else None
        )

    def view(self) -> dict[str, Any]:
        """The ``approval_request`` audit view of this row."""
        return {
            "kind": self.kind.value,
            "environment_id": self.environment_id,
            "subject_key": self.subject_key,
            "state": self.state,
            "requested_by_user_id": self.requested_by_user_id,
            "decided_by_user_id": self.decided_by_user_id,
            "decision_channel": self.decision_channel,
        }


def approval_row(row: Mapping[Any, Any]) -> ApprovalRow:
    """A database row to :class:`ApprovalRow`; an unknown kind or state raises ``ValueError``."""
    state = str(row["state"])
    if state not in _STATES:
        raise ValueError(f"unknown approval state {state!r}")
    channel = row["decision_channel"]
    return ApprovalRow(
        id=str(row["id"]),
        app_id=str(row["app_id"]),
        app=str(row["app"]),
        environment_id=str(row["environment_id"]),
        environment=str(row["environment"]),
        kind=RequirementKind(str(row["kind"])),
        subject_key=str(row["subject_key"]),
        payload=cast(dict[str, Any], row["payload"]),
        state=cast(ApprovalState, state),
        requested_by_user_id=str(row["requested_by_user_id"]),
        requested_by_name=str(row["requested_by_name"]),
        requested_via_agent=bool(row["requested_via_agent"]),
        decided_by_user_id=row["decided_by_user_id"],
        decided_at=row["decided_at"],
        decision_reason=row["decision_reason"],
        decision_channel=cast(DecisionChannel | None, channel),
        recorded_by_operator=row["recorded_by_operator"],
        policy_decision_id=row["policy_decision_id"],
        created_at=row["created_at"],
    )


class ApprovalRefusedError(Exception):
    """A request that cannot be decided as asked; ``reason`` says why."""

    def __init__(self, reason: RefusalReason) -> None:
        super().__init__(reason)
        self.reason: RefusalReason = reason


@dataclass(frozen=True, slots=True, kw_only=True)
class Decider:
    """Who decided, how the decision reached us, and what it was."""

    user_id: str
    via_agent: bool
    recorded_by_operator: str | None
    channel: DecisionChannel
    reason: str
    outcome: DecisionOutcome


async def get(
    conn: AsyncConnection, *, org_id: str, approval_id: str, lock: bool = False
) -> ApprovalRow | None:
    """One request of the org, or None. ``lock`` takes the row ``FOR UPDATE``."""
    query = _select().where(_R.c.org_id == org_id, _R.c.id == approval_id)
    if lock:
        query = query.with_for_update(of=_R)
    row = (await conn.execute(query)).mappings().first()
    return None if row is None else approval_row(row)


async def newest(
    conn: AsyncConnection,
    *,
    org_id: str,
    environment_id: str,
    requirements: Iterable[Requirement],
) -> dict[Requirement, ApprovalRow]:
    """The newest request for each requirement that has one."""
    wanted = set(requirements)
    if not wanted:
        return {}
    r = _R.c
    query = (
        _select()
        .where(
            r.org_id == org_id,
            r.environment_id == environment_id,
            r.kind.in_(sorted({w.kind.value for w in wanted})),
            r.subject_key.in_(sorted({w.subject_key for w in wanted})),
        )
        .distinct(r.kind, r.subject_key)
        # Newest first; a pending row wins a tie inside one transaction.
        .order_by(
            r.kind, r.subject_key, r.created_at.desc(), (r.state == "pending").desc(), r.id.desc()
        )
    )
    rows = await conn.execute(query)
    found = (approval_row(r) for r in rows.mappings())
    return {a.requirement: a for a in found if a.requirement in wanted}


async def request(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    environment_id: str,
    requirement: Requirement,
    requested_by: str,
    via_agent: bool,
    payload: Mapping[str, object],
    actor: Actor,
) -> tuple[ApprovalRow, bool]:
    """The pending or approved request for this question, or a new pending one (``True``).

    A new request is audited as ``approval.requested`` by ``actor``. Two callers asking at once
    get the same row: the loser's insert does nothing and it reads the winner's request.
    """
    for _ in range(_ATTEMPTS):
        found = (
            await newest(
                conn, org_id=org_id, environment_id=environment_id, requirements=[requirement]
            )
        ).get(requirement)
        if found is not None and found.state in ("pending", "approved"):
            return found, False
        apr_id = new_id("apr")
        inserted = (
            await conn.execute(
                _INSERT_PENDING,
                {
                    "id": apr_id,
                    "org": org_id,
                    "env": environment_id,
                    "kind": requirement.kind.value,
                    "key": requirement.subject_key,
                    "payload": json.dumps(dict(payload), sort_keys=True, ensure_ascii=False),
                    "by": requested_by,
                    "via_agent": via_agent,
                },
            )
        ).scalar_one_or_none()
        if inserted is None:
            continue  # another transaction holds the pending request; read it next time round
        row = await get(conn, org_id=org_id, approval_id=apr_id)
        if row is None:
            raise RuntimeError(f"approval request {apr_id} vanished inside its own transaction")
        await append_event(
            conn,
            NewEvent(
                org_id=org_id,
                action=AuditAction.APPROVAL_REQUESTED,
                actor=actor,
                target_kind="approval_request",
                target_id=apr_id,
                after=row.view(),
            ),
        )
        await notifications.arrived(
            conn,
            org_id=org_id,
            approval_id=apr_id,
            requester_id=requested_by,
            connection=row.connection,
        )
        return row, True
    raise RuntimeError("the pending approval request kept changing under concurrent writers")


async def connection_owner(conn: AsyncConnection, org_id: str, name: str) -> str | None:
    """The user who owns the connection called ``name``, or None."""
    params = {"org": org_id, "name": name}
    return (await conn.execute(_CONNECTION_OWNER, params)).scalar_one_or_none()


async def decide(
    conn: AsyncConnection, *, org_id: str, approval_id: str, decider: Decider, actor: Actor
) -> ApprovalRow:
    """Record a decision. Raises :class:`ApprovalRefusedError` in the order the API documents:
    agent session, missing, not pending, self-approval, not an active admin."""
    if decider.via_agent:
        raise ApprovalRefusedError("agent_session")
    row = await get(conn, org_id=org_id, approval_id=approval_id, lock=True)
    if row is None:
        raise ApprovalRefusedError("not_found")
    if row.state != "pending":
        raise ApprovalRefusedError("not_pending")
    # FOR SHARE: the approver cannot be demoted or deactivated until this decision commits.
    account = (await conn.execute(_LOCK_DECIDER, {"org": org_id, "id": decider.user_id})).first()
    owner = None if row.connection is None else await connection_owner(conn, org_id, row.connection)
    refusal = check_decider(
        row.requested_by_user_id,
        decider.user_id,
        None if account is None else str(account[0]),
        account is not None and account[1] == "active",
        decider.via_agent,
        connection_owner_id=owner,
    )
    if refusal is not None:
        raise ApprovalRefusedError(refusal)
    pol_id = await record_policy_decision(
        conn,
        org_id=org_id,
        principal_kind="user",
        principal_id=decider.user_id,
        action=DECIDE_ACTION,
        target_kind="approval_request",
        target_id=approval_id,
        outcome="allow",
        reason="other_active_admin",
        inputs={
            "outcome": decider.outcome,
            "kind": row.kind.value,
            "subject_key": row.subject_key,
            "environment_id": row.environment_id,
            "requested_by_user_id": row.requested_by_user_id,
            "channel": decider.channel,
            "recorded_by_operator": decider.recorded_by_operator,
        },
    )
    await conn.execute(
        _DECIDE,
        {
            "org": org_id,
            "id": approval_id,
            "state": decider.outcome,
            "by": decider.user_id,
            "via_agent": decider.via_agent,
            "reason": decider.reason,
            "channel": decider.channel,
            "operator": decider.recorded_by_operator,
            "pol": pol_id,
        },
    )
    decided = await get(conn, org_id=org_id, approval_id=approval_id)
    if decided is None:
        raise RuntimeError(f"approval request {approval_id} vanished while it was locked")
    if decided.state == "approved":
        await on_approval(
            conn, org_id=org_id, kind=decided.kind, actor=actor, policy_decision_id=pol_id
        )
        if decided.kind is RequirementKind.ENABLE_INTERNET_HOSTS:
            await _allow_host(conn, org_id, decided, actor, pol_id)
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.APPROVAL_DECIDED,
            actor=actor,
            target_kind="approval_request",
            target_id=approval_id,
            before=row.view(),
            after=decided.view(),
            policy_decision_id=pol_id,
        ),
    )
    await notifications.decided(
        conn, org_id=org_id, approval_id=approval_id, requester_id=row.requested_by_user_id
    )
    return decided


async def cancel(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    approval_id: str,
    user_id: str,
    channel: DecisionChannel,
    reason: str,
    actor: Actor,
) -> ApprovalRow:
    """The requester withdraws their own pending request. Raises :class:`ApprovalRefusedError`:
    missing, someone else's, no longer pending. Audited as ``approval.cancelled``."""
    row = await get(conn, org_id=org_id, approval_id=approval_id, lock=True)
    if row is None:
        raise ApprovalRefusedError("not_found")
    if row.requested_by_user_id != user_id:
        raise ApprovalRefusedError("not_requester")
    if row.state != "pending":
        raise ApprovalRefusedError("not_pending")
    await conn.execute(
        _CANCEL,
        {"org": org_id, "id": approval_id, "by": user_id, "reason": reason, "channel": channel},
    )
    cancelled = await get(conn, org_id=org_id, approval_id=approval_id)
    if cancelled is None:
        raise RuntimeError(f"approval request {approval_id} vanished while it was locked")
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.APPROVAL_CANCELLED,
            actor=actor,
            target_kind="approval_request",
            target_id=approval_id,
            before=row.view(),
            after=cancelled.view(),
        ),
    )
    return cancelled


async def _allow_host(
    conn: AsyncConnection, org_id: str, decided: ApprovalRow, actor: Actor, pol_id: str
) -> None:
    """An approved internet host joins the org's allowlist (SSC-053). One the allowlist cannot
    take (not a pattern it accepts, or the list is full) leaves the approval standing; the
    deploy's capability diff then still names it."""
    try:
        await allowlist.allow(
            conn,
            org_id=org_id,
            host=decided.subject_key,
            actor=actor,
            approval_request_id=decided.id,
            policy_decision_id=pol_id,
        )
    except allowlist.EgressHostError as exc:
        log.warning(
            "approved host not allowed", extra={"approval_id": decided.id, "error": str(exc)}
        )


async def data_connected(
    conn: AsyncConnection, *, org_id: str, environment_id: str, source: CapabilitySource
) -> bool:
    """Whether the environment's app reaches company data. Unknown counts as connected."""
    caps = await source.for_environment(conn, org_id=org_id, environment_id=environment_id)
    return caps is None or bool(caps.connections)


async def share_requirements(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    environment_id: str,
    profile: str,
    base_version: int,
    before: Collection[GrantKey],
    after: Collection[GrantKey],
    via_agent: bool,
    source: CapabilitySource,
    exceeded: Collection[str],
) -> list[Requirement]:
    """What replacing ``before`` with ``after`` needs approved: ``agent_share`` for any change
    through an agent credential, ``widen_audience`` for widening a data-connected app, and
    ``exceed_ceiling`` for each connection in ``exceeded`` (the ones whose audience ceiling
    ``after`` goes beyond) when the change widens."""
    out: list[Requirement] = []
    agent = agent_share_needs_approval(via_agent, base_version, before, after)
    if agent is not None:
        out.append(agent)
    connected = widens(before, after) and await data_connected(
        conn, org_id=org_id, environment_id=environment_id, source=source
    )
    widen = widening_needs_approval(profile, connected, before, after)
    if widen is not None:
        out.append(widen)
    if widens(before, after):
        out.extend(
            Requirement(RequirementKind.EXCEED_CEILING, exceed_subject_key(name, after))
            for name in sorted(exceeded)
        )
    return out


def _owns(user_id: str) -> ColumnElement[bool]:
    """An ``exceed_ceiling`` request on a connection this user owns."""
    r = _R.c
    return and_(
        r.kind == RequirementKind.EXCEED_CEILING.value,
        exists().where(
            _C.c.org_id == r.org_id,
            _C.c.name == func.jsonb_extract_path_text(r.payload, "connection"),
            _C.c.owner_user_id == user_id,
        ),
    )


async def search(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    visible_to: str | None,
    decidable_by: str | None = None,
    decider_is_admin: bool = False,
    state: ApprovalState | None,
    environment_id: str | None,
    before: ApprovalRow | None,
    limit: int,
) -> list[ApprovalRow]:
    """Newest first, keyed on ``(created_at, id)`` so a page boundary never skips a row.
    ``visible_to`` limits the page to one user's own requests and the ``exceed_ceiling`` requests
    on connections they own. ``decidable_by`` keeps the pending requests that user may decide:
    not their own, and any when ``decider_is_admin``, else only those on connections they own."""
    r = _R.c
    query = _select().where(r.org_id == org_id)
    if visible_to is not None:
        query = query.where(or_(r.requested_by_user_id == visible_to, _owns(visible_to)))
    if decidable_by is not None:
        query = query.where(
            r.state == "pending",
            r.requested_by_user_id != decidable_by,
            true() if decider_is_admin else _owns(decidable_by),
        )
    if state is not None:
        query = query.where(r.state == state)
    if environment_id is not None:
        query = query.where(r.environment_id == environment_id)
    if before is not None:
        query = query.where(
            tuple_(r.created_at, r.id) < tuple_(literal(before.created_at), literal(before.id))
        )
    query = query.order_by(r.created_at.desc(), r.id.desc()).limit(limit)
    return [approval_row(row) for row in (await conn.execute(query)).mappings()]
