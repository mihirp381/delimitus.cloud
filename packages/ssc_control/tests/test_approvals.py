"""SSC-045: approvals, operator-recorded, against postgres:18.

Ticket "done when" checks:
  * self-approval is refused             -> test_self_approval_is_refused_by_the_api_and_database
  * an agent-session approval is refused -> test_agent_session_decisions_are_refused
  * a production deploy waits on an open approval
                                         -> test_production_deploy_waits_on_an_open_approval
Plus: the fail-closed gate, the ProdGate port over recorded and manifest capabilities, widening
a data-connected app, agent grant changes (agent_share), the operator-only decision endpoint,
who sees which request and what the deployment policy shows (SSC-093), operator.access, and the
0005 migration.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from ssc_testkit import ISSUER, Dsns, SigningKey, assert_problem, auth, mint, new_key

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.capabilities import (
    CapabilityChange,
    CapabilityDiff,
    EnvironmentCapabilities,
    diff_capabilities,
)
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_contracts.manifest import Manifest, load_manifest
from ssc_control.api import Settings, create_app
from ssc_control.api.dberrors import classify
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.approvals import service
from ssc_control.approvals.capabilities import (
    ManifestCapabilities,
    requested_by_diff,
    requested_by_manifest,
)
from ssc_control.approvals.gate import GATE_ACTION, ApprovalsProdGate, production_gate
from ssc_control.approvals.service import ApprovalRefusedError, Decider
from ssc_control.audit import Actor
from ssc_control.db import (
    MIGRATE_ROLE,
    NewOrg,
    bind_org_sync,
    bound_org,
    create_org,
    downgrade,
    make_engine,
    upgrade,
)
from ssc_control.db.errors import CHECK_VIOLATION
from ssc_control.domain.approval_rules import (
    RequestedCapabilities,
    Requirement,
    RequirementKind,
    agent_share_subject_key,
    share_subject_key,
)
from ssc_control.ports import GateResult

DIGEST = "sha256:" + "a" * 64
OPERATOR_SUB = "op_ada"
OPERATOR = Actor(ActorKind.OPERATOR, OPERATOR_SUB)
FINANCE = RequestedCapabilities(connections=frozenset({"finance"}))
HOST = RequestedCapabilities(egress_hosts=frozenset({"api.example.com"}))

# ── world ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class World:
    org: str
    admin: str  # the org's first admin, the app's owner, and the usual requester
    approver: str  # a second active admin
    member: str  # an active member with no grant
    builder: str  # an active member with a builder grant on prod
    retired: str  # a deactivated admin
    app: str
    prod: str
    preview: str
    release: str


def add_account(conn: psycopg.Connection[Any], org: str, role: str, status: str) -> str:
    uid = new_id("usr")
    conn.execute(
        "insert into ssc.user_account (id, org_id, display_name, email, role, status, "
        "deactivated_at) values (%s, %s, 'Some One', 'someone@example.com', %s, %s, "
        "case when %s = 'deactivated' then now() end)",
        (uid, org, role, status, status),
    )
    return uid


def add_release(
    conn: psycopg.Connection[Any], org: str, app: str, number: int, kind: str, actor: str
) -> str:
    rid = new_id("rel")
    conn.execute(
        "insert into ssc.release (id, org_id, app_id, number, image_digest, manifest_digest, "
        "source_digest, actor_kind, actor_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (rid, org, app, number, DIGEST, DIGEST, DIGEST, kind, actor),
    )
    return rid


async def make_world(dsn: str, name: str = "Approvals") -> World:
    engine = make_engine(dsn)
    try:
        spec = NewOrg(name, "Ada Admin", "ada@example.com", ISSUER, new_id("usr"))
        created = await create_org(engine, spec)
    finally:
        await engine.dispose()
    org, admin = created.org_id, created.admin_user_id
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        approver = add_account(conn, org, "admin", "active")
        member = add_account(conn, org, "member", "active")
        builder = add_account(conn, org, "member", "active")
        retired = add_account(conn, org, "admin", "deactivated")
        app, prod, preview = new_id("app"), new_id("env"), new_id("env")
        conn.execute(
            "insert into ssc.app (id, org_id, slug, owner_user_id) values (%s, %s, 'ledger', %s)",
            (app, org, admin),
        )
        for env, env_name in ((prod, "prod"), (preview, "preview")):
            conn.execute(
                "insert into ssc.environment (id, org_id, app_id, name) values (%s, %s, %s, %s)",
                (env, org, app, env_name),
            )
        conn.execute(
            "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, user_id, "
            "granted_by_user_id) values (%s, %s, %s, 'builder', 'user', %s, %s)",
            (new_id("gnt"), org, prod, builder, admin),
        )
        release = add_release(conn, org, app, 1, "user", admin)
    return World(org, admin, approver, member, builder, retired, app, prod, preview, release)


def approvals_of(dsn: str, org: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        bind_org_sync(conn, org)
        return conn.execute("select * from ssc.approval_request order by created_at, id").fetchall()


def policies_of(dsn: str, org: str, action: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        bind_org_sync(conn, org)
        return conn.execute(
            "select * from ssc.policy_decision where action = %s order by at, id", (action,)
        ).fetchall()


def events_of(dsn: str, org: str, action: AuditAction) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        bind_org_sync(conn, org)
        return conn.execute(
            "select actor_kind, actor_id, actor_via_agent, target_kind, target_id, before, after, "
            "policy_decision_id from ssc.audit_event where action = %s order by seq",
            (action.value,),
        ).fetchall()


async def in_org(dsn: str, org: str, work: Any) -> Any:
    """Run ``work(conn)`` in one org-bound transaction that commits."""
    engine = make_engine(dsn)
    try:
        async with bound_org(engine, org) as conn:
            return await work(conn)
    finally:
        await engine.dispose()


async def gate(
    dsn: str,
    w: World,
    caps: RequestedCapabilities | None,
    *,
    env: str | None = None,
    by: Actor | None = None,
    app: str | None = None,
) -> GateResult:
    async def work(conn: AsyncConnection) -> GateResult:
        return await production_gate(
            conn,
            org_id=w.org,
            environment_id=env or w.prod,
            capabilities=caps,
            requested_by=by or Actor(ActorKind.USER, w.admin),
            app_id=app or w.app,
            release_id=w.release,
        )

    return await in_org(dsn, w.org, work)


def decider(
    user_id: str, outcome: str = "approved", *, via_agent: bool = False, reason: str = "By email."
) -> Decider:
    return Decider(
        user_id=user_id,
        via_agent=via_agent,
        recorded_by_operator=OPERATOR_SUB,
        channel="email",
        reason=reason,
        outcome="approved" if outcome == "approved" else "denied",
    )


async def decide(dsn: str, w: World, approval_id: str, who: Decider) -> service.ApprovalRow:
    async def work(conn: AsyncConnection) -> service.ApprovalRow:
        return await service.decide(
            conn, org_id=w.org, approval_id=approval_id, decider=who, actor=OPERATOR
        )

    return await in_org(dsn, w.org, work)


async def ask(
    dsn: str, w: World, requirement: Requirement, *, by: str | None = None
) -> service.ApprovalRow:
    async def work(conn: AsyncConnection) -> service.ApprovalRow:
        row, _ = await service.request(
            conn,
            org_id=w.org,
            environment_id=w.prod,
            requirement=requirement,
            requested_by=by or w.admin,
            via_agent=False,
            payload={},
            actor=Actor(ActorKind.USER, by or w.admin),
        )
        return row

    return await in_org(dsn, w.org, work)


# ── the production gate ──────────────────────────────────────────────────────


async def test_production_deploy_waits_on_an_open_approval(dsns: Dsns) -> None:
    w = await make_world(dsns.app)
    first = await gate(dsns.app, w, FINANCE)
    assert first.outcome == "waiting"
    (row,) = approvals_of(dsns.app, w.org)
    assert first.approval_ids == (row["id"],)
    assert (row["kind"], row["subject_key"], row["state"]) == (
        "connect_data_source",
        "finance",
        "pending",
    )
    assert row["requested_by_user_id"] == w.admin
    assert row["payload"] == {"release_id": w.release}
    # Asking again returns the same open request, never a second one.
    again = await gate(dsns.app, w, FINANCE)
    assert (again.outcome, again.approval_ids) == ("waiting", first.approval_ids)
    assert len(approvals_of(dsns.app, w.org)) == 1
    await decide(dsns.app, w, row["id"], decider(w.approver))
    clear = await gate(dsns.app, w, FINANCE)
    assert (clear.outcome, clear.approval_ids) == ("clear", first.approval_ids)
    # Every outcome is a policy decision; only clear allows.
    rows = policies_of(dsns.app, w.org, GATE_ACTION)
    assert [p["id"] for p in rows] == [
        first.policy_decision_id,
        again.policy_decision_id,
        clear.policy_decision_id,
    ]
    assert [(p["outcome"], p["reason"]) for p in rows] == [
        ("deny", "pending"),
        ("deny", "pending"),
        ("allow", "approved"),
    ]
    assert rows[2]["principal_kind"] == "user"
    assert rows[2]["principal_id"] == w.admin
    assert (rows[2]["target_kind"], rows[2]["target_id"]) == ("environment", w.prod)
    assert rows[2]["inputs"]["requirements"] == ["connect_data_source:finance"]
    assert rows[2]["inputs"]["approval_ids"] == [row["id"]]
    (requested,) = events_of(dsns.app, w.org, AuditAction.APPROVAL_REQUESTED)
    assert (requested["actor_kind"], requested["actor_id"]) == ("operator", "system:prod_gate")


async def test_a_denied_requirement_refuses_until_asked_again(dsns: Dsns) -> None:
    w = await make_world(dsns.app)
    waiting = await gate(dsns.app, w, HOST)
    (apr,) = waiting.approval_ids
    await decide(dsns.app, w, apr, decider(w.approver, "denied", reason="Not this host."))
    refused = await gate(dsns.app, w, HOST)
    assert (refused.outcome, refused.approval_ids) == ("refused", (apr,))
    assert len(approvals_of(dsns.app, w.org)) == 1  # a denial is not re-asked by the gate
    fresh = await ask(
        dsns.app, w, Requirement(RequirementKind.ENABLE_INTERNET_HOSTS, "api.example.com")
    )
    assert fresh.id != apr
    assert fresh.state == "pending"
    after = await gate(dsns.app, w, HOST)
    assert (after.outcome, after.approval_ids) == ("waiting", (fresh.id,))


async def test_every_requirement_must_be_approved(dsns: Dsns) -> None:
    w = await make_world(dsns.app)
    both = RequestedCapabilities(
        connections=frozenset({"finance"}), egress_hosts=frozenset({"api.example.com"})
    )
    waiting = await gate(dsns.app, w, both)
    assert waiting.outcome == "waiting"
    assert len(waiting.approval_ids) == 2
    await decide(dsns.app, w, waiting.approval_ids[0], decider(w.approver))
    half = await gate(dsns.app, w, both)
    assert (half.outcome, half.approval_ids) == ("waiting", waiting.approval_ids[1:])
    await decide(dsns.app, w, waiting.approval_ids[1], decider(w.approver))
    assert (await gate(dsns.app, w, both)).outcome == "clear"


async def test_preview_is_clear_and_asks_nothing(dsns: Dsns) -> None:
    w = await make_world(dsns.app)
    result = await gate(dsns.app, w, FINANCE, env=w.preview)
    assert (result.outcome, result.approval_ids) == ("clear", ())
    assert result.policy_decision_id is not None
    assert approvals_of(dsns.app, w.org) == []
    (pol,) = policies_of(dsns.app, w.org, GATE_ACTION)
    assert (pol["outcome"], pol["reason"]) == ("allow", "not_production")


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        pytest.param("missing_environment", "environment_not_found", id="missing environment"),
        pytest.param("other_app", "environment_not_of_app", id="environment of another app"),
        pytest.param("unknown_capabilities", "capabilities_unknown", id="capabilities unknown"),
        pytest.param("unknown_kind", "unknown_capability", id="an unknown capability kind"),
        pytest.param("workload", "no_user_requester", id="no user behind the requester"),
    ],
)
async def test_the_gate_fails_closed(dsns: Dsns, case: str, reason: str) -> None:
    w = await make_world(dsns.app)
    caps: RequestedCapabilities | None = FINANCE
    env, app, by = w.prod, w.app, Actor(ActorKind.USER, w.admin)
    match case:
        case "missing_environment":
            env = new_id("env")
        case "other_app":
            app = new_id("app")
        case "unknown_capabilities":
            caps = None
        case "unknown_kind":
            caps = RequestedCapabilities(unknown=frozenset({"smtp"}))
        case _:
            by = Actor(ActorKind.WORKLOAD, w.prod)
    result = await gate(dsns.app, w, caps, env=env, app=app, by=by)
    assert (result.outcome, result.approval_ids) == ("refused", ())
    assert approvals_of(dsns.app, w.org) == []
    (pol,) = policies_of(dsns.app, w.org, GATE_ACTION)
    assert (pol["id"], pol["outcome"], pol["reason"]) == (result.policy_decision_id, "deny", reason)


class _RollbackError(Exception):
    pass


async def test_an_unknown_profile_refuses(dsns: Dsns) -> None:
    """The CHECK allows only 'internal'; lift it in a transaction that rolls back, to prove the
    gate refuses a profile it has no rule for rather than letting the deploy through."""
    w = await make_world(dsns.app)
    engine = make_engine(dsns.superuser)
    try:
        with pytest.raises(_RollbackError):
            async with bound_org(engine, w.org) as conn:
                await conn.execute(
                    text("alter table ssc.environment drop constraint environment_profile_check")
                )
                await conn.execute(
                    text("update ssc.environment set profile = 'public' where id = :env"),
                    {"env": w.prod},
                )
                for caps in (FINANCE, RequestedCapabilities()):
                    result = await production_gate(
                        conn,
                        org_id=w.org,
                        environment_id=w.prod,
                        capabilities=caps,
                        requested_by=Actor(ActorKind.USER, w.admin),
                    )
                    assert (result.outcome, result.approval_ids) == ("refused", ())
                reason = (
                    await conn.execute(
                        text("select reason from ssc.policy_decision where id = :id"),
                        {"id": result.policy_decision_id},
                    )
                ).scalar_one()
                assert reason == "unknown_profile"
                raise _RollbackError
    finally:
        await engine.dispose()


async def test_prod_gate_port_reads_the_recorded_capabilities(dsns: Dsns) -> None:
    w = await make_world(dsns.app)
    port = ApprovalsProdGate()

    async def check(release: str) -> GateResult:
        async def work(conn: AsyncConnection) -> GateResult:
            return await port.check(
                conn, org_id=w.org, app_id=w.app, environment_id=w.prod, release_id=release
            )

        return await in_org(dsns.app, w.org, work)

    # Nothing declared for the environment: nothing to approve.
    assert (await check(w.release)).outcome == "clear"
    declared = await ask(
        dsns.app, w, Requirement(RequirementKind.CONNECT_DATA_SOURCE, "finance"), by=w.builder
    )
    waiting = await check(w.release)
    assert (waiting.outcome, waiting.approval_ids) == ("waiting", (declared.id,))
    await decide(dsns.app, w, declared.id, decider(w.approver))
    assert (await check(w.release)).outcome == "clear"
    # A release that is not this app's is unknown, and unknown refuses.
    assert (await check(new_id("rel"))).outcome == "refused"


async def test_prod_gate_port_over_manifests_asks_as_the_release_author(dsns: Dsns) -> None:
    w = await make_world(dsns.app)
    manifest = load_manifest(
        'schema = "ssc/v1"\n'
        '[connections]\nnames = ["finance"]\n'
        '[egress]\nhosts = ["api.example.com"]\n'
    )
    assert requested_by_manifest(manifest) == RequestedCapabilities(
        connections=frozenset({"finance"}), egress_hosts=frozenset({"api.example.com"})
    )
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, w.org)
        by_workload = add_release(conn, w.org, w.app, 2, "workload", w.prod)
        by_builder = add_release(conn, w.org, w.app, 3, "user", w.builder)
    manifests: dict[str, Manifest | None] = {
        w.release: manifest,
        by_workload: manifest,
        by_builder: None,
    }

    async def load(conn: AsyncConnection, org_id: str, release_id: str) -> Manifest | None:
        return manifests[release_id]

    port = ApprovalsProdGate(ManifestCapabilities(load))

    async def check(release: str) -> GateResult:
        async def work(conn: AsyncConnection) -> GateResult:
            return await port.check(
                conn, org_id=w.org, app_id=w.app, environment_id=w.prod, release_id=release
            )

        return await in_org(dsns.app, w.org, work)

    waiting = await check(by_workload)
    assert waiting.outcome == "waiting"
    rows = approvals_of(dsns.app, w.org)
    assert {(r["kind"], r["subject_key"]) for r in rows} == {
        ("connect_data_source", "finance"),
        ("enable_internet_hosts", "api.example.com"),
    }
    # No person wrote that release, so the app's owner is the requester.
    assert {r["requested_by_user_id"] for r in rows} == {w.admin}
    # An unreadable manifest is unknown, and unknown refuses.
    assert (await check(by_builder)).outcome == "refused"


def test_a_capability_diff_maps_fail_closed() -> None:
    manifest = load_manifest(
        'schema = "ssc/v1"\n'
        "[state]\npostgres = true\n"
        '[connections]\nnames = ["finance", "hr"]\n'
        '[egress]\nhosts = ["api.example.com"]\n'
    )
    granted = EnvironmentCapabilities(connections=frozenset({"hr"}))
    assert requested_by_diff(diff_capabilities(manifest, granted)) == RequestedCapabilities(
        connections=frozenset({"finance"}), egress_hosts=frozenset({"api.example.com"})
    )
    listed = diff_capabilities(manifest, EnvironmentCapabilities())
    # A kind the rules do not map, and a diff that does not list every change, are unknown.
    odd = CapabilityChange.model_construct(
        severity="high", kind="smtp_missing", subject="smtp", consequence="x", approver=None
    )
    grown = CapabilityDiff.model_construct(changes=(*listed.changes, odd), total=4, blocks=False)
    assert requested_by_diff(grown).unknown == frozenset({"smtp_missing"})
    cut = CapabilityDiff.model_construct(changes=listed.changes, total=99, blocks=False)
    assert requested_by_diff(cut).unknown == frozenset({"summarised_diff"})


async def test_two_askers_at_once_share_one_request(dsns: Dsns) -> None:
    w = await make_world(dsns.app)
    req = Requirement(RequirementKind.CONNECT_DATA_SOURCE, "finance")
    engine = make_engine(dsns.app)
    inserted = asyncio.Event()
    try:

        async def first() -> str:
            async with bound_org(engine, w.org) as conn:
                row, created = await service.request(
                    conn,
                    org_id=w.org,
                    environment_id=w.prod,
                    requirement=req,
                    requested_by=w.admin,
                    via_agent=False,
                    payload={},
                    actor=Actor(ActorKind.USER, w.admin),
                )
                assert created
                inserted.set()
                await asyncio.sleep(0.3)  # hold the uncommitted row while the second one asks
                return row.id

        async def second() -> tuple[str, bool]:
            await inserted.wait()
            async with bound_org(engine, w.org) as conn:
                row, created = await service.request(
                    conn,
                    org_id=w.org,
                    environment_id=w.prod,
                    requirement=req,
                    requested_by=w.builder,
                    via_agent=False,
                    payload={},
                    actor=Actor(ActorKind.USER, w.builder),
                )
                return row.id, created

        one, (two, created) = await asyncio.gather(first(), second())
    finally:
        await engine.dispose()
    assert two == one
    assert not created
    assert len(approvals_of(dsns.app, w.org)) == 1


# ── decisions: the service and the database ─────────────────────────────────


async def test_decide_refuses_in_order(dsns: Dsns) -> None:
    w = await make_world(dsns.app)
    apr = (await ask(dsns.app, w, Requirement(RequirementKind.CONNECT_DATA_SOURCE, "hr"))).id
    for who, reason in [
        (decider(w.approver, via_agent=True), "agent_session"),
        (decider(w.admin, via_agent=True), "agent_session"),
        (decider(w.admin), "self_approval"),
        (decider(w.member), "not_eligible"),
        (decider(w.retired), "not_eligible"),
        (decider(new_id("usr")), "not_eligible"),
    ]:
        with pytest.raises(ApprovalRefusedError) as e:
            await decide(dsns.app, w, apr, who)
        assert e.value.reason == reason
    with pytest.raises(ApprovalRefusedError) as e:
        await decide(dsns.app, w, new_id("apr"), decider(w.approver))
    assert e.value.reason == "not_found"
    decided = await decide(dsns.app, w, apr, decider(w.approver))
    assert (decided.state, decided.decided_by_user_id) == ("approved", w.approver)
    with pytest.raises(ApprovalRefusedError) as e:
        await decide(dsns.app, w, apr, decider(w.approver, "denied"))
    assert e.value.reason == "not_pending"


async def test_the_database_names_each_refused_decision(dsns: Dsns) -> None:
    w = await make_world(dsns.app)
    apr = (await ask(dsns.app, w, Requirement(RequirementKind.CONNECT_DATA_SOURCE, "hr"))).id
    engine = make_engine(dsns.app)
    base = (
        "update ssc.approval_request set state = :state, decided_by_user_id = :by, "
        "decided_at = now(), decision_reason = 'x', decided_via_agent = :agent where id = :id"
    )
    try:
        for state, by, agent, constraint, code in [
            (
                "approved",
                w.admin,
                False,
                "approval_request_not_self",
                ErrorCode.SELF_APPROVAL_REFUSED,
            ),
            (
                "denied",
                w.admin,
                False,
                "approval_request_not_self",
                ErrorCode.SELF_APPROVAL_REFUSED,
            ),
            (
                "approved",
                w.approver,
                True,
                "approval_request_decided_via_agent_check",
                ErrorCode.AGENT_SESSION_REFUSED,
            ),
        ]:
            with pytest.raises(DBAPIError) as e:
                async with bound_org(engine, w.org) as conn:
                    await conn.execute(
                        text(base), {"state": state, "by": by, "agent": agent, "id": apr}
                    )
            orig = e.value.orig
            assert isinstance(orig, psycopg.Error)
            assert (orig.sqlstate, orig.diag.constraint_name) == (CHECK_VIOLATION, constraint)
            assert classify(e.value)[0] is code
        with pytest.raises(DBAPIError) as e:
            async with bound_org(engine, w.org) as conn:
                await conn.execute(
                    text(
                        "insert into ssc.approval_request (id, org_id, environment_id, kind, "
                        "subject_key, requested_by_user_id) values (:id, :org, :env, "
                        "'connect_data_source', 'hr', :by)"
                    ),
                    {"id": new_id("apr"), "org": w.org, "env": w.prod, "by": w.builder},
                )
        assert classify(e.value)[0] is ErrorCode.ALREADY_EXISTS
    finally:
        await engine.dispose()
    (row,) = approvals_of(dsns.app, w.org)
    assert row["state"] == "pending"


def test_kind_check_matches_the_rule_kinds(dsns: Dsns) -> None:
    with psycopg.connect(dsns.migrate) as conn:
        (definition,) = conn.execute(
            "select pg_get_constraintdef(oid) from pg_constraint "
            "where conrelid = 'ssc.approval_request'::regclass "
            "and conname = 'approval_request_kind_check'"
        ).fetchone() or (None,)
    assert definition is not None
    import re

    assert set(re.findall(r"'([a-z_]+)'", definition)) == {k.value for k in RequirementKind}


async def test_environment_profile_defaults_to_internal_and_is_closed(dsns: Dsns) -> None:
    w = await make_world(dsns.app)
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, w.org)
        profiles = conn.execute(
            "select profile from ssc.environment where app_id = %s", (w.app,)
        ).fetchall()
        assert profiles == [("internal",), ("internal",)]
        with pytest.raises(psycopg.Error) as e:
            conn.execute("update ssc.environment set profile = 'public' where id = %s", (w.prod,))
        assert e.value.sqlstate == CHECK_VIOLATION
        assert e.value.diag.constraint_name == "environment_profile_check"


def test_0005_renames_the_self_check_and_downgrade_restores_it(dsns: Dsns) -> None:
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database approvals05 owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database="approvals05").render_as_string(hide_password=False)

    def checks() -> dict[str, str]:
        with psycopg.connect(dsn) as conn:
            rows = conn.execute(
                "select conname, pg_get_constraintdef(oid) from pg_constraint "
                "where conrelid = 'ssc.approval_request'::regclass and contype = 'c'"
            ).fetchall()
        return {str(n): str(d) for n, d in rows}

    def has_profile() -> bool:
        with psycopg.connect(dsn) as conn:
            return conn.execute(
                "select count(*) from information_schema.columns where table_schema = 'ssc' "
                "and table_name = 'environment' and column_name = 'profile'"
            ).fetchone() == (1,)

    upgrade(dsn, "0003_lane_vocab")
    before = checks()
    (old_self,) = [n for n, d in before.items() if "IS DISTINCT FROM requested_by_user_id" in d]
    assert old_self != "approval_request_not_self"
    upgrade(dsn, "0005_approvals")
    after = checks()
    assert old_self not in after
    assert "approval_request_not_self" in after
    assert "'denied'" in after["approval_request_not_self"]
    assert has_profile()
    downgrade(dsn, "0003_lane_vocab")
    assert sorted(checks().values()) == sorted(before.values())
    assert not has_profile()
    upgrade(dsn)
    assert "approval_request_not_self" in checks()


# ── the API ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client(dsns: Dsns, signing_key: SigningKey) -> Iterator[TestClient]:
    settings = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
    )
    with TestClient(create_app(settings)) as c:
        yield c


@dataclass(frozen=True)
class Tokens:
    admin: str
    admin_agent: str
    approver: str
    member: str
    builder: str
    operator: str
    operator_agent: str
    workload: str


@pytest.fixture
def world(dsns: Dsns) -> World:
    return asyncio.run(make_world(dsns.app))


@pytest.fixture
def tokens(world: World, signing_key: SigningKey) -> Tokens:
    def token(sub: str, **claims: Any) -> str:
        return mint(signing_key, org=world.org, sub=sub, jti=f"cred_{new_key()[:16]}", **claims)

    return Tokens(
        admin=token(world.admin),
        admin_agent=token(world.admin, agent=True, client_id="agent-x"),
        approver=token(world.approver),
        member=token(world.member),
        builder=token(world.builder),
        operator=token(OPERATOR_SUB, kind="operator"),
        operator_agent=token(OPERATOR_SUB, kind="operator", agent=True, client_id="agent-x"),
        workload=token(world.prod, kind="workload"),
    )


def post(
    client: TestClient, path: str, token: str, body: dict[str, Any], key: str | None = None
) -> Any:
    return client.post(
        path, json=body, headers=auth(token, **{IDEMPOTENCY_HEADER: key or new_key()})
    )


def ask_api(
    client: TestClient,
    token: str,
    env: str,
    kind: str = "connect_data_source",
    subject: str | None = "finance",
    payload: dict[str, Any] | None = None,
) -> Any:
    body: dict[str, Any] = {"environment_id": env, "kind": kind, "payload": payload or {}}
    if subject is not None:
        body["subject_key"] = subject
    return post(client, "/v1/approvals", token, body)


def decision(approver: str, outcome: str = "approved") -> dict[str, Any]:
    return {
        "outcome": outcome,
        "approver_user_id": approver,
        "channel": "email",
        "reason": "Yes, by email on 29 Sep.",
    }


def test_self_approval_is_refused_by_the_api_and_database(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    r = ask_api(client, tokens.admin, world.prod)
    assert r.status_code == 201, r.text
    apr = r.json()["id"]
    refused = post(client, f"/v1/approvals/{apr}/decision", tokens.operator, decision(world.admin))
    assert_problem(refused, ErrorCode.SELF_APPROVAL_REFUSED)
    got = client.get(f"/v1/approvals/{apr}", headers=auth(tokens.admin))
    assert got.json()["state"] == "pending"
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, world.org)
        with pytest.raises(psycopg.Error) as e:
            conn.execute(
                "update ssc.approval_request set state = 'approved', decided_at = now(), "
                "decided_by_user_id = requested_by_user_id, decision_reason = 'x' where id = %s",
                (apr,),
            )
    assert e.value.diag.constraint_name == "approval_request_not_self"


def test_agent_session_decisions_are_refused(
    client: TestClient, world: World, tokens: Tokens
) -> None:
    apr = ask_api(client, tokens.admin, world.prod).json()["id"]
    refused = post(
        client, f"/v1/approvals/{apr}/decision", tokens.operator_agent, decision(world.approver)
    )
    assert_problem(refused, ErrorCode.AGENT_SESSION_REFUSED)
    assert (
        client.get(f"/v1/approvals/{apr}", headers=auth(tokens.admin)).json()["state"] == "pending"
    )


def test_only_an_operator_records_decisions(
    client: TestClient, world: World, tokens: Tokens
) -> None:
    apr = ask_api(client, tokens.admin, world.prod).json()["id"]
    for token in (tokens.approver, tokens.admin, tokens.workload, tokens.admin_agent):
        assert_problem(
            post(client, f"/v1/approvals/{apr}/decision", token, decision(world.approver)),
            ErrorCode.FORBIDDEN,
        )
    for approver in (world.member, world.retired, new_id("usr")):
        r = post(client, f"/v1/approvals/{apr}/decision", tokens.operator, decision(approver))
        assert_problem(r, ErrorCode.APPROVER_NOT_ELIGIBLE)
    missing = post(
        client, f"/v1/approvals/{new_id('apr')}/decision", tokens.operator, decision(world.approver)
    )
    assert_problem(missing, ErrorCode.NOT_FOUND)


def test_a_decision_is_recorded_once_and_replays(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    apr = ask_api(client, tokens.builder, world.prod).json()["id"]
    key = new_key()
    first = post(
        client, f"/v1/approvals/{apr}/decision", tokens.operator, decision(world.approver), key
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["state"] == "approved"
    assert body["decided_by_user_id"] == world.approver
    assert body["requested_by_user_id"] == world.builder
    assert body["recorded_by_operator"] == OPERATOR_SUB
    assert body["decision_channel"] == "email"
    assert body["decision_reason"] == "Yes, by email on 29 Sep."
    assert body["policy_decision_id"].startswith("pol_")
    replay = post(
        client, f"/v1/approvals/{apr}/decision", tokens.operator, decision(world.approver), key
    )
    assert (replay.status_code, replay.content) == (200, first.content)
    again = post(
        client, f"/v1/approvals/{apr}/decision", tokens.operator, decision(world.approver, "denied")
    )
    assert_problem(again, ErrorCode.APPROVAL_NOT_PENDING)
    (pol,) = policies_of(dsns.app, world.org, "approval.decide")
    assert pol["id"] == body["policy_decision_id"]
    assert (pol["principal_kind"], pol["principal_id"], pol["outcome"]) == (
        "user",
        world.approver,
        "allow",
    )
    assert (pol["target_kind"], pol["target_id"]) == ("approval_request", apr)
    assert pol["inputs"]["recorded_by_operator"] == OPERATOR_SUB
    (decided,) = events_of(dsns.app, world.org, AuditAction.APPROVAL_DECIDED)
    assert (decided["actor_kind"], decided["actor_id"]) == ("operator", OPERATOR_SUB)
    assert decided["policy_decision_id"] == body["policy_decision_id"]
    assert decided["before"]["state"] == "pending"
    assert decided["after"]["state"] == "approved"
    assert decided["after"]["decided_by_user_id"] == world.approver
    accesses = events_of(dsns.app, world.org, AuditAction.OPERATOR_ACCESS)
    assert [(a["target_kind"], a["target_id"]) for a in accesses] == [("approval_request", apr)]


def test_asking_for_an_approval(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    first = ask_api(client, tokens.builder, world.prod)
    assert first.status_code == 201, first.text
    body = first.json()
    assert (body["kind"], body["subject_key"], body["state"]) == (
        "connect_data_source",
        "finance",
        "pending",
    )
    assert (body["app_id"], body["environment_id"]) == (world.app, world.prod)
    assert body["requested_via_agent"] is False
    same = ask_api(client, tokens.admin, world.prod)
    assert (same.status_code, same.json()["id"]) == (200, body["id"])
    (requested,) = events_of(dsns.app, world.org, AuditAction.APPROVAL_REQUESTED)
    assert (requested["actor_id"], requested["target_id"]) == (world.builder, body["id"])
    assert requested["after"]["subject_key"] == "finance"
    agent = ask_api(
        client, tokens.admin_agent, world.prod, "enable_internet_hosts", "api.example.com"
    )
    assert agent.status_code == 201, agent.text
    assert agent.json()["requested_via_agent"] is True
    for token in (tokens.member, tokens.operator, tokens.workload):
        assert_problem(ask_api(client, token, world.prod), ErrorCode.FORBIDDEN)
    assert_problem(ask_api(client, tokens.builder, world.preview), ErrorCode.FORBIDDEN)
    assert_problem(ask_api(client, tokens.admin, new_id("env")), ErrorCode.NOT_FOUND)
    for kind, subject, payload in [
        ("connect_data_source", "Finance!", {}),
        ("connect_data_source", None, {}),
        ("connect_data_source", "finance", {"note": "please"}),
        ("enable_internet_hosts", "https://api.example.com", {}),
        ("enable_internet_hosts", "10.0.0.1", {}),
        (
            "widen_audience",
            None,
            {"grants": [{"role": "user", "subject_kind": "org", "subject_id": world.member}]},
        ),
        ("widen_audience", "sha256:" + "0" * 64, {"grants": []}),
        ("agent_share", None, {"grants": []}),
    ]:
        assert_problem(
            ask_api(client, tokens.admin, world.prod, kind, subject, payload),
            ErrorCode.VALIDATION_FAILED,
        )
    grants = [{"role": "user", "subject_kind": "org", "subject_id": None}]
    widen = ask_api(client, tokens.admin, world.prod, "widen_audience", None, {"grants": grants})
    assert widen.status_code == 201, widen.text
    assert widen.json()["subject_key"] == share_subject_key([("user", "org", None)])
    stale = ask_api(
        client,
        tokens.admin,
        world.prod,
        "agent_share",
        None,
        {"grants_version": 9, "grants": grants},
    )
    assert_problem(stale, ErrorCode.PRECONDITION_STALE)
    share = ask_api(
        client,
        tokens.admin,
        world.prod,
        "agent_share",
        None,
        {"grants_version": 1, "grants": grants},
    )
    assert share.json()["subject_key"] == agent_share_subject_key(1, [("user", "org", None)])


def test_who_sees_which_request(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns, signing_key: SigningKey
) -> None:
    mine = ask_api(client, tokens.builder, world.prod, subject="finance").json()["id"]
    theirs = ask_api(client, tokens.admin, world.prod, subject="hr").json()["id"]
    newest = ask_api(client, tokens.admin, world.prod, subject="payroll").json()["id"]

    def ids(token: str, query: str = "") -> list[str]:
        r = client.get(f"/v1/approvals{query}", headers=auth(token))
        assert r.status_code == 200, r.text
        return [a["id"] for a in r.json()["approvals"]]

    assert ids(tokens.approver) == [newest, theirs, mine]
    assert ids(tokens.builder) == [mine]
    assert ids(tokens.member) == []
    assert ids(tokens.operator) == [newest, theirs, mine]
    page = client.get("/v1/approvals?limit=2", headers=auth(tokens.admin)).json()
    assert ([a["id"] for a in page["approvals"]], page["next_before"]) == ([newest, theirs], theirs)
    rest = client.get(f"/v1/approvals?limit=2&before={theirs}", headers=auth(tokens.admin)).json()
    assert ([a["id"] for a in rest["approvals"]], rest["next_before"]) == ([mine], None)
    assert ids(tokens.admin, "?state=approved") == []
    assert ids(tokens.admin, f"?environment_id={world.preview}") == []
    bad_cursor = client.get(f"/v1/approvals?before={theirs}", headers=auth(tokens.builder))
    assert_problem(bad_cursor, ErrorCode.VALIDATION_FAILED)
    assert client.get(f"/v1/approvals/{mine}", headers=auth(tokens.builder)).status_code == 200
    assert_problem(
        client.get(f"/v1/approvals/{theirs}", headers=auth(tokens.builder)), ErrorCode.NOT_FOUND
    )
    assert_problem(
        client.get(f"/v1/approvals/{theirs}", headers=auth(tokens.member)), ErrorCode.NOT_FOUND
    )
    assert_problem(client.get("/v1/approvals", headers=auth(tokens.workload)), ErrorCode.FORBIDDEN)
    assert client.get(f"/v1/approvals/{theirs}", headers=auth(tokens.operator)).status_code == 200
    accesses = events_of(dsns.app, world.org, AuditAction.OPERATOR_ACCESS)
    assert [(a["target_kind"], a["target_id"]) for a in accesses] == [
        ("org", world.org),
        ("approval_request", theirs),
    ]
    other = asyncio.run(make_world(dsns.app, "Other"))
    stranger = mint(signing_key, org=other.org, sub=other.admin, jti=f"cred_{new_key()[:16]}")
    assert_problem(
        client.get(f"/v1/approvals/{theirs}", headers=auth(stranger)), ErrorCode.NOT_FOUND
    )
    assert client.get("/v1/approvals", headers=auth(stranger)).json()["approvals"] == []


def test_the_deployment_policy_shows_only_what_the_caller_may_see(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns, signing_key: SigningKey
) -> None:
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, world.org)
        for name, classification in (
            ("finance", "confidential"),
            ("hr", "restricted"),
            ("payroll", "internal"),
        ):
            conn.execute(
                "insert into ssc.connection (id, org_id, name, kind, classification, host, port, "
                "database_name, address) values (%s, %s, %s, 'postgres', %s, 'db.corp.internal', "
                "5432, 'warehouse', jsonb_build_object('host', 'db.corp.internal', 'port', 5432, "
                "'database', 'warehouse'))",
                (new_id("con"), world.org, name, classification),
            )
        conn.execute(
            "insert into ssc.app_database (org_id, environment_id, host, port, connection_limit) "
            "values (%s, %s, '10.21.0.3', 5432, 2)",
            (world.org, world.prod),
        )
    asks = [
        ask_api(client, tokens.builder, world.prod, subject="finance"),
        ask_api(client, tokens.admin, world.prod, subject="hr"),
        ask_api(client, tokens.builder, world.prod, "enable_internet_hosts", "api.mine.example"),
        ask_api(client, tokens.admin, world.prod, "enable_internet_hosts", "api.theirs.example"),
    ]
    for r in asks:
        apr = r.json()["id"]
        decided = post(
            client, f"/v1/approvals/{apr}/decision", tokens.operator, decision(world.approver)
        )
        assert decided.status_code == 200, decided.text
    ask_api(client, tokens.builder, world.prod, "enable_internet_hosts", "api.pending.example")

    def policy(token: str) -> dict[str, Any]:
        r = client.get("/v1/org/deployment-policy", headers=auth(token))
        assert r.status_code == 200, r.text
        return r.json()

    def seen(token: str) -> tuple[str, list[str], list[str]]:
        p = policy(token)
        return p["scope"], [c["name"] for c in p["connections"]], [h["host"] for h in p["hosts"]]

    everything = ("org", ["finance", "hr", "payroll"], ["api.mine.example", "api.theirs.example"])
    assert seen(tokens.approver) == everything
    assert seen(tokens.admin_agent) == everything
    assert seen(tokens.builder) == ("own", ["finance"], ["api.mine.example"])
    assert seen(tokens.member) == ("own", [], [])
    full = policy(tokens.operator)
    assert full["connections"][0] == {
        "name": "finance",
        "kind": "postgres",
        "classification": "confidential",
        "owner_user_id": None,
        "ceiling": {"audience": "org", "subjects": []},
    }
    assert full["hosts"][0] == {
        "host": "api.mine.example",
        "app_id": world.app,
        "environment_id": world.prod,
    }
    assert "db.corp.internal" not in str(full)
    assert "warehouse" not in str(full)
    assert full["database"] == {"places_used": 1, "places_total": 10, "room": True}
    assert {a["kind"] for a in full["approvals"]} == {k.value for k in RequirementKind}
    assert "never through an agent" in full["approver"]
    assert "poppler-utils" in full["approved_packages"]
    assert "SSC support" in full["how_to_ask_for_a_package"]
    accesses = events_of(dsns.app, world.org, AuditAction.OPERATOR_ACCESS)
    assert ("org", world.org) in [(a["target_kind"], a["target_id"]) for a in accesses]
    assert_problem(
        client.get("/v1/org/deployment-policy", headers=auth(tokens.workload)), ErrorCode.FORBIDDEN
    )
    other = asyncio.run(make_world(dsns.app, "Other"))
    stranger = mint(signing_key, org=other.org, sub=other.admin, jti=f"cred_{new_key()[:16]}")
    assert seen(stranger) == ("org", [], [])
    assert policy(stranger)["database"]["places_used"] == 0


# ── sharing rules ────────────────────────────────────────────────────────────


def grants_path(w: World, env: str) -> str:
    return f"/v1/apps/{w.app}/environments/{env}/grants"


def put(
    client: TestClient, w: World, env: str, token: str, grants: list[dict[str, Any]], version: int
) -> Any:
    return client.put(
        grants_path(w, env),
        json={"grants": grants},
        headers=auth(token, **{"If-Match": f'"{version}"'}),
    )


def current(client: TestClient, w: World, env: str, token: str) -> dict[str, Any]:
    r = client.get(grants_path(w, env), headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def keyed(grants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: g[k] for k in ("role", "subject_kind", "subject_id")} for g in grants]


def test_widening_a_data_connected_app_needs_approval(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    before = keyed(current(client, world, world.prod, tokens.admin)["grants"])
    org_wide = [*before, {"role": "user", "subject_kind": "org", "subject_id": None}]
    # Not data-connected yet: widening applies at once (preview is for builders).
    preview_wide = [*before, {"role": "builder", "subject_kind": "org", "subject_id": None}]
    assert put(client, world, world.preview, tokens.admin, preview_wide, 1).status_code == 200
    # The app now asks for company data in prod.
    ask_api(client, tokens.builder, world.prod, subject="finance")
    refused = put(client, world, world.prod, tokens.admin, org_wide, 1)
    assert_problem(refused, ErrorCode.APPROVAL_REQUIRED)
    assert current(client, world, world.prod, tokens.admin)["grants_version"] == 1
    widen = ask_api(client, tokens.admin, world.prod, "widen_audience", None, {"grants": org_wide})
    assert widen.status_code == 201, widen.text
    assert_problem(
        put(client, world, world.prod, tokens.admin, org_wide, 1), ErrorCode.APPROVAL_REQUIRED
    )
    decided = post(
        client,
        f"/v1/approvals/{widen.json()['id']}/decision",
        tokens.operator,
        decision(world.approver),
    )
    assert decided.status_code == 200, decided.text
    applied = put(client, world, world.prod, tokens.admin, org_wide, 1)
    assert applied.status_code == 200, applied.text
    assert applied.json()["grants_version"] == 2
    added = [
        e
        for e in events_of(dsns.app, world.org, AuditAction.GRANT_ADDED)
        if e["after"]["environment_id"] == world.prod
    ]
    assert [e["policy_decision_id"] for e in added] == [decided.json()["policy_decision_id"]]
    # Narrowing never waits.
    assert put(client, world, world.prod, tokens.admin, before, 2).status_code == 200


def test_agent_grant_changes_wait_for_another_admin(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    before = keyed(current(client, world, world.prod, tokens.admin)["grants"])
    wider = [*before, {"role": "user", "subject_kind": "user", "subject_id": world.member}]
    pending = put(client, world, world.prod, tokens.admin_agent, wider, 1)
    assert pending.status_code == 202, pending.text
    assert pending.headers["ETag"] == '"1"'
    body = pending.json()
    assert (body["environment_id"], body["grants_version"]) == (world.prod, 1)
    (apr,) = body["approval_ids"]
    assert keyed(current(client, world, world.prod, tokens.admin)["grants"]) == before
    again = put(client, world, world.prod, tokens.admin_agent, wider, 1)
    assert (again.status_code, again.json()["approval_ids"]) == (202, [apr])
    (row,) = approvals_of(dsns.app, world.org)
    assert (row["kind"], row["requested_by_user_id"], row["requested_via_agent"]) == (
        "agent_share",
        world.admin,
        True,
    )
    assert row["payload"]["grants_version"] == 1
    # The requester, in their own session, still cannot approve their agent's change.
    assert_problem(
        post(client, f"/v1/approvals/{apr}/decision", tokens.operator, decision(world.admin)),
        ErrorCode.SELF_APPROVAL_REFUSED,
    )
    decided = post(
        client, f"/v1/approvals/{apr}/decision", tokens.operator, decision(world.approver)
    )
    assert decided.status_code == 200, decided.text
    applied = put(client, world, world.prod, tokens.admin_agent, wider, 1)
    assert applied.status_code == 200, applied.text
    assert applied.json()["grants_version"] == 2
    (added,) = events_of(dsns.app, world.org, AuditAction.GRANT_ADDED)
    assert added["policy_decision_id"] == decided.json()["policy_decision_id"]
    assert added["actor_via_agent"] is True
    # The approval covered one change at one version; the next agent change asks again.
    narrower = put(client, world, world.prod, tokens.admin_agent, before, 2)
    assert narrower.status_code == 202, narrower.text
    assert narrower.json()["approval_ids"] != [apr]
    # A person's change applies directly.
    assert put(client, world, world.prod, tokens.admin, before, 2).status_code == 200
