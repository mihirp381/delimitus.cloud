"""The production gate: nothing boots in production before its approvals are decided.

Fail closed, the inverse of Delimitus' publish gate: ``clear`` is returned only when every rule
was evaluated and every requirement is approved. A missing environment, an unknown profile,
environment name or capability, unknown capabilities, or a requester with no user behind it
give ``refused``. Preview is ``clear``. In production, an approved requirement passes, a pending
one waits, a denied one refuses, and a missing one is opened as a pending request, then waits.

Every outcome writes a ``policy_decision``. A new pending request is written in the caller's
transaction, so a caller that answers ``waiting`` with a refusal (and rolls back) loses it; the
deploy job records its failure and commits.
"""

from collections.abc import Mapping
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import ActorKind
from ssc_control.approvals.capabilities import CapabilitySource, RecordedCapabilities
from ssc_control.approvals.policy import PolicyPrincipalKind, record_policy_decision
from ssc_control.approvals.service import ApprovalRow, newest, request
from ssc_control.audit import Actor
from ssc_control.domain.approval_rules import (
    PROFILES,
    GateOutcome,
    NoRuleError,
    RequestedCapabilities,
    gate_outcome,
    required_for_deploy,
)
from ssc_control.ports import GateResult, ProdGate

GATE_ACTION: Final = "deploy.production_gate"
# Requests the gate opens on a requester's behalf are audited as the platform's doing.
GATE_ACTOR: Final = Actor(ActorKind.OPERATOR, "system:prod_gate")

_PRINCIPAL: Final[Mapping[ActorKind, PolicyPrincipalKind]] = {
    ActorKind.USER: "user",
    ActorKind.WORKLOAD: "workload",
    ActorKind.SCHEDULE: "schedule",
    ActorKind.OPERATOR: "operator",
    ActorKind.INTEGRATION: "integration",
}

_SELECT_ENV = text(
    "select name, profile, app_id from ssc.environment where org_id = :org and id = :env"
)
# The release's author when they are a user of the org, else the app's owner.
_REQUESTER = text(
    "select u.id, r.actor_via_agent, r.actor_client_id, a.owner_user_id from ssc.release r "
    "join ssc.app a on a.org_id = r.org_id and a.id = r.app_id "
    "left join ssc.user_account u on r.actor_kind = 'user' "
    "and u.org_id = r.org_id and u.id = r.actor_id "
    "where r.org_id = :org and r.app_id = :app and r.id = :rel"
)


def _screen(  # noqa: PLR0911  (one return per rule, in order)
    env: tuple[str, str, str] | None,
    app_id: str | None,
    capabilities: RequestedCapabilities | None,
    requested_by: Actor,
) -> tuple[GateOutcome, str] | None:
    """The outcome decided before any approval is read, or None to read them."""
    if env is None:
        return "refused", "environment_not_found"
    name, profile, env_app = env
    if app_id is not None and env_app != app_id:
        return "refused", "environment_not_of_app"
    if profile not in PROFILES:
        return "refused", "unknown_profile"
    if name == "preview":
        return "clear", "not_production"
    if name != "prod":
        return "refused", "unknown_environment"
    if capabilities is None:
        return "refused", "capabilities_unknown"
    if requested_by.kind is not ActorKind.USER:
        return "refused", "no_user_requester"
    return None


async def _evaluate(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    environment_id: str,
    app_id: str | None,
    capabilities: RequestedCapabilities | None,
    requested_by: Actor,
    release_id: str | None,
) -> tuple[GateOutcome, str, list[str], tuple[ApprovalRow, ...]]:
    """Outcome, reason, the requirements as strings, and the approvals behind the outcome."""
    row = (await conn.execute(_SELECT_ENV, {"org": org_id, "env": environment_id})).first()
    env = None if row is None else (str(row[0]), str(row[1]), str(row[2]))
    screened = _screen(env, app_id, capabilities, requested_by)
    if screened is not None or env is None or capabilities is None:
        outcome, reason = screened or ("refused", "unreachable")
        return outcome, reason, [], ()
    try:
        required = sorted(required_for_deploy(env[1], capabilities))
    except NoRuleError as e:
        return "refused", e.reason, [], ()
    names = [f"{r.kind.value}:{r.subject_key}" for r in required]
    found = await newest(conn, org_id=org_id, environment_id=environment_id, requirements=required)
    # A cancelled request was withdrawn: the requirement is asked again.
    current = {r: a for r, a in found.items() if a.state != "cancelled"}
    outcome = gate_outcome({r: (current[r].state if r in current else None) for r in required})
    if outcome == "refused":
        denied = tuple(a for a in current.values() if a.state == "denied")
        return "refused", "denied", names, denied
    if outcome == "clear":
        return "clear", "approved", names, tuple(current[r] for r in required)
    opened: list[ApprovalRow] = []
    for req in required:
        pending = current.get(req)
        if pending is None:
            pending, _ = await request(
                conn,
                org_id=org_id,
                environment_id=environment_id,
                requirement=req,
                requested_by=requested_by.id,
                via_agent=requested_by.via_agent,
                payload={"release_id": release_id} if release_id else {},
                actor=GATE_ACTOR,
            )
        if pending.state == "pending":
            opened.append(pending)
    return "waiting", "pending", names, tuple(opened)


async def production_gate(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    environment_id: str,
    capabilities: RequestedCapabilities | None,
    requested_by: Actor,
    app_id: str | None = None,
    release_id: str | None = None,
) -> GateResult:
    """May a release asking for ``capabilities`` run in this environment now?

    ``capabilities`` None means unknown. Runs in ``conn``'s org-bound transaction.
    """
    outcome, reason, names, approvals = await _evaluate(
        conn,
        org_id=org_id,
        environment_id=environment_id,
        app_id=app_id,
        capabilities=capabilities,
        requested_by=requested_by,
        release_id=release_id,
    )
    ids = tuple(a.id for a in approvals)
    pol_id = await record_policy_decision(
        conn,
        org_id=org_id,
        principal_kind=_PRINCIPAL[requested_by.kind],
        principal_id=requested_by.id,
        action=GATE_ACTION,
        target_kind="environment",
        target_id=environment_id,
        outcome="allow" if outcome == "clear" else "deny",
        reason=reason,
        inputs={
            "outcome": outcome,
            "app_id": app_id,
            "release_id": release_id,
            "requirements": names,
            "approval_ids": list(ids),
            "via_agent": requested_by.via_agent,
        },
    )
    return GateResult(outcome=outcome, approval_ids=ids, policy_decision_id=pol_id)


async def _requester(conn: AsyncConnection, *, org_id: str, app_id: str, release_id: str) -> Actor:
    row = (
        await conn.execute(_REQUESTER, {"org": org_id, "app": app_id, "rel": release_id})
    ).first()
    if row is None:
        return GATE_ACTOR  # no release: refused below, and the decision names the gate
    if row[0] is not None:
        client = None if row[2] is None else str(row[2])
        return Actor(ActorKind.USER, str(row[0]), via_agent=bool(row[1]), client_id=client)
    return Actor(ActorKind.USER, str(row[3]))


class ApprovalsProdGate(ProdGate):
    """:class:`ssc_control.ports.ProdGate` backed by approvals. ``source`` says what a release
    asks for; the default reads the environment's records until B4 supplies manifests."""

    def __init__(self, source: CapabilitySource | None = None) -> None:
        self._source: CapabilitySource = source or RecordedCapabilities()

    async def check(
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        app_id: str,
        environment_id: str,
        release_id: str,
    ) -> GateResult:
        requested_by = await _requester(conn, org_id=org_id, app_id=app_id, release_id=release_id)
        capabilities = await self._source.for_release(
            conn,
            org_id=org_id,
            app_id=app_id,
            environment_id=environment_id,
            release_id=release_id,
        )
        return await production_gate(
            conn,
            org_id=org_id,
            environment_id=environment_id,
            capabilities=capabilities,
            requested_by=requested_by,
            app_id=app_id,
            release_id=release_id,
        )
