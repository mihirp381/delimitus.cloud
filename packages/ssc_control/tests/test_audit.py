"""SSC-012: the audit log core, against postgres:18.

Ticket "done when" checks:
  * verify names the first broken link          -> test_verify_names_the_first_broken_link,
                                                   test_cli_exits_1_and_prints_the_broken_seq
  * an update by the app role fails             -> test_audit_log_is_append_only (test_control_db)
  * concurrent appends never share a prev_hash  -> test_a_waiting_append_links_to_the_holder,
                                                   test_concurrent_appends_stay_one_chain
Plus: views, the policy link, org.created at seq 1, admin-only search with keyset pages, and
audited, byte-stable CSV and JSON-lines exports that re-verify offline and never carry an IP.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from ssc_testkit import ISSUER, Dsns, SigningKey, assert_problem, auth, make_org, mint, new_key

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api import Settings, create_app
from ssc_control.api.auth import Principal, PrincipalKind
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.api.uow import UnitOfWork
from ssc_control.audit import (
    GENESIS_HASH,
    VIEWS,
    Actor,
    AuditViewError,
    NewEvent,
    append_event,
    check_view,
)
from ssc_control.audit.__main__ import main, run_verify
from ssc_control.audit.export import CSV_COLUMNS, render_csv, render_jsonl
from ssc_control.audit.verify import V1_KEYS, BrokenLink, VerifyReport
from ssc_control.audit.views import FILTER_KEYS
from ssc_control.db import (
    CreatedOrg,
    NewOrg,
    bind_org_sync,
    bound_org,
    create_org,
    make_engine,
    sqlalchemy_url,
)
from ssc_control.db.catalog import FORBIDDEN_COLUMN_NAMES, PII_COLUMN_NAMES

BASE = datetime(2026, 9, 1, tzinfo=UTC)

# ── helpers ──────────────────────────────────────────────────────────────────


def schedule_event(org: str, n: int, *, at: datetime | None = None) -> NewEvent:
    return NewEvent(
        org_id=org,
        action=AuditAction.SCHEDULE_CREATED,
        actor=Actor(ActorKind.USER, "usr_builder"),
        target_kind="schedule",
        target_id=f"sch_{n}",
        after={"name": f"job-{n}", "cron": "0 * * * *"},
        at=at,
    )


async def append_all(dsn: str, events: Iterable[NewEvent]) -> None:
    """Each event in its own transaction, like the API does."""
    engine = make_engine(dsn)
    try:
        for event in events:
            async with bound_org(engine, event.org_id) as conn:
                await append_event(conn, event)
    finally:
        await engine.dispose()


async def new_org(engine: AsyncEngine, name: str = "Audit") -> str:
    spec = NewOrg(name, "Ada Admin", "ada@example.com", ISSUER, new_id("usr"))
    return (await create_org(engine, spec)).org_id


def chain_rows(dsn: str, org: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        return conn.execute(
            "select seq, action, actor_kind, actor_id, target_kind, target_id, before, after, "
            "policy_decision_id, prev_hash, hash from ssc.audit_event order by seq"
        ).fetchall()


def tamper(dsns: Dsns, sql: str, params: dict[str, object]) -> None:
    """Rewrite history as the container superuser, with the append-only trigger switched off."""
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute("set session_replication_role = replica")
        conn.execute(sql, params)


async def rows_of(*rows: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    for row in rows:
        yield row


async def collect(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([c async for c in chunks])


# ── views ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        pytest.param("app", {"slug": "a", "display": "x"}, id="unlisted key"),
        pytest.param("release", {"number": 1.5}, id="float"),
        pytest.param("app", {"token": "t0ps3cret"}, id="token key"),
        pytest.param("user_account", {"email": "ada@example.com"}, id="personal data"),
        pytest.param("app", {"slug": {"nested": "x"}}, id="nested object"),
        pytest.param("user_group", {"added": [1, 2]}, id="list of numbers"),
        pytest.param("nothing", {"name": "x"}, id="unknown target kind"),
        pytest.param("audit", {"filters": {"password": "x"}}, id="unknown filter"),
        pytest.param("audit", {"filters": {"action": {"x": 1}}}, id="nested filter"),
        pytest.param("audit", {"filters": "action=x"}, id="filters not an object"),
    ],
)
def test_views_refuse_what_they_do_not_list(kind: str, data: dict[str, Any]) -> None:
    with pytest.raises(AuditViewError):
        check_view(kind, data)


def test_views_accept_every_shape_the_call_sites_write() -> None:
    check_view("app", {"slug": "a", "owner_user_id": "usr_x", "status": "active"})
    check_view(
        "app_grant",
        {"environment_id": "env_x", "role": "user", "subject_kind": "org", "subject_id": None},
    )
    check_view("deployment", {"environment_id": "env_x", "release_id": "rel_x"})
    check_view("user_group", {"directory_ref": "g1", "added": ["usr_a"], "removed": []})
    check_view("schedule", {"timeout_seconds": 30, "state": "paused"})
    check_view("audit", {"format": "csv", "filters": {"action": "app.created", "since": "x"}})
    check_view("org", {"name": "Acme"})
    check_view("app", None)


def test_views_name_no_secret_and_no_personal_data() -> None:
    keys = set().union(*VIEWS.values()) | FILTER_KEYS
    assert not keys & FORBIDDEN_COLUMN_NAMES
    assert not keys & PII_COLUMN_NAMES


async def test_append_refuses_a_bad_view_or_a_naive_time_and_writes_nothing(dsns: Dsns) -> None:
    engine = make_engine(dsns.app)
    try:
        org = await new_org(engine)
        bad_view = NewEvent(
            org_id=org,
            action=AuditAction.USER_UPDATED,
            actor=Actor(ActorKind.USER, "usr_x"),
            target_kind="user_account",
            target_id="usr_y",
            after={"role": "admin", "display_name": "Ada"},
        )
        with pytest.raises(AuditViewError):
            async with bound_org(engine, org) as conn:
                await append_event(conn, bad_view)
        naive = schedule_event(org, 1, at=datetime(2026, 9, 1))
        with pytest.raises(ValueError, match="UTC offset"):
            async with bound_org(engine, org) as conn:
                await append_event(conn, naive)
        assert (await run_verify(dsns.app, org)) == VerifyReport(True, 1, 1, None)
    finally:
        await engine.dispose()


# ── org.created and the policy link ──────────────────────────────────────────


async def test_org_created_is_seq_1_and_the_chain_verifies(dsns: Dsns) -> None:
    engine = make_engine(dsns.app)
    try:
        org = await new_org(engine, "Genesis")
        spec = NewOrg("By Hand", "Ada Admin", "ada@example.com", ISSUER, new_id("usr"))
        by_user = (await create_org(engine, spec, actor=Actor(ActorKind.USER, "usr_x"))).org_id
    finally:
        await engine.dispose()
    [first] = chain_rows(dsns.app, org)
    assert first[:9] == (
        1,
        "org.created",
        "operator",
        "system:create_org",
        "org",
        org,
        None,
        {"name": "Genesis"},
        None,
    )
    assert bytes(first[9]) == GENESIS_HASH
    assert (await run_verify(dsns.app, org)) == VerifyReport(True, 1, 1, None)
    assert chain_rows(dsns.app, by_user)[0][2:4] == ("user", "usr_x")


async def test_policy_decision_id_is_stored_and_chained(dsns: Dsns) -> None:
    engine = make_engine(dsns.app)
    try:
        org = await new_org(engine)
        pid = new_id("pol")
        principal = Principal(org, "usr_x", PrincipalKind.USER, "cred_policy")
        async with bound_org(engine, org) as conn:
            await conn.execute(
                text(
                    "insert into ssc.policy_decision (id, org_id, principal_kind, principal_id, "
                    "action, target_kind, target_id, outcome, reason) values (:id, :org, 'user', "
                    "'usr_x', 'deploy', 'environment', 'env_x', 'allow', 'test')"
                ),
                {"id": pid, "org": org},
            )
            uow = UnitOfWork(conn=conn, principal=principal, request_id="req")
            appended = await uow.audit(
                AuditAction.DEPLOY_STARTED,
                target_kind="deployment",
                target_id="dep_x",
                after={"environment_id": "env_x", "release_id": "rel_x"},
                policy_decision_id=pid,
            )
        async with bound_org(engine, org) as conn:
            stored, canonical = (
                await conn.execute(
                    text(
                        "select policy_decision_id, canonical from ssc.audit_event "
                        "where org_id = :org and seq = :seq"
                    ),
                    {"org": org, "seq": appended.seq},
                )
            ).one()
    finally:
        await engine.dispose()
    assert stored == pid
    assert json.loads(bytes(canonical))["policy_decision_id"] == pid
    assert (await run_verify(dsns.app, org)).ok


# ── verify ───────────────────────────────────────────────────────────────────

JUNK = hashlib.sha256(b"junk").digest()
BREAKS = {
    "canonical edited": (
        "update ssc.audit_event set canonical = canonical || '\\x20'::bytea "
        "where org_id = %(org)s and seq = 3",
        BrokenLink(3, "hash"),
    ),
    "after column edited": (
        'update ssc.audit_event set after = \'{"name": "renamed"}\' '
        "where org_id = %(org)s and seq = 3",
        BrokenLink(3, "fields"),
    ),
    "at column edited": (
        "update ssc.audit_event set at = at + interval '1 second' "
        "where org_id = %(org)s and seq = 3",
        BrokenLink(3, "fields"),
    ),
    "actor column edited": (
        "update ssc.audit_event set actor_via_agent = true where org_id = %(org)s and seq = 3",
        BrokenLink(3, "fields"),
    ),
    "row deleted": (
        "delete from ssc.audit_event where org_id = %(org)s and seq = 3",
        BrokenLink(3, "missing"),
    ),
    "prev_hash replaced": (
        "update ssc.audit_event set prev_hash = %(junk)s where org_id = %(org)s and seq = 3",
        BrokenLink(3, "prev_link"),
    ),
    "tail deleted": (
        "delete from ssc.audit_event where org_id = %(org)s and seq = 6",
        BrokenLink(6, "missing"),
    ),
    "head hash wrong": (
        "update ssc.audit_head set hash = %(junk)s where org_id = %(org)s",
        BrokenLink(6, "head"),
    ),
    "head behind": (
        "update ssc.audit_head set seq = 5 where org_id = %(org)s",
        BrokenLink(6, "head"),
    ),
    "head missing": (
        "delete from ssc.audit_head where org_id = %(org)s",
        BrokenLink(6, "head"),
    ),
}


def six_event_org(dsns: Dsns) -> str:
    """org.created plus five more: seq 1..6."""
    org = make_org(dsns.app, "Chain").org_id
    asyncio.run(append_all(dsns.app, [schedule_event(org, n) for n in range(5)]))
    return org


@pytest.mark.parametrize("case", list(BREAKS))
def test_verify_names_the_first_broken_link(dsns: Dsns, case: str) -> None:
    org = six_event_org(dsns)
    assert asyncio.run(run_verify(dsns.app, org)) == VerifyReport(True, 6, 6, None)
    sql, expected = BREAKS[case]
    tamper(dsns, sql, {"org": org, "junk": JUNK})
    report = asyncio.run(run_verify(dsns.app, org))
    assert not report.ok
    assert report.first_broken == expected
    assert report.checked == (expected.seq - 1 if expected.cause != "head" else expected.seq)


def test_cli_exits_1_and_prints_the_broken_seq(
    dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    org = six_event_org(dsns)
    monkeypatch.setenv("SSC_DATABASE_DSN", dsns.app)
    assert main(["verify", "--org", org]) == 0
    assert capsys.readouterr().out == "ok: 6 events, head at seq 6\n"
    sql, _ = BREAKS["canonical edited"]
    tamper(dsns, sql, {"org": org})
    done = subprocess.run(
        [sys.executable, "-m", "ssc_control.audit", "verify", "--org", org],
        env={**os.environ, "SSC_DATABASE_DSN": dsns.app},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (done.returncode, done.stdout) == (1, "broken: seq 3 cause hash\n"), done.stderr


# ── concurrency ──────────────────────────────────────────────────────────────


async def test_a_waiting_append_links_to_the_holder(dsns: Dsns) -> None:
    engine = make_engine(dsns.app)
    try:
        org = await new_org(engine)

        async def second() -> int:
            async with bound_org(engine, org) as conn:
                return (await append_event(conn, schedule_event(org, 2))).seq

        async with bound_org(engine, org) as holder:
            first = await append_event(holder, schedule_event(org, 1))
            waiting = asyncio.create_task(second())
            done, _ = await asyncio.wait({waiting}, timeout=0.2)
            assert not done, "the second append must wait for the head lock"
        seq = await waiting
    finally:
        await engine.dispose()
    assert seq == first.seq + 1
    rows = {r[0]: r for r in chain_rows(dsns.app, org)}
    assert bytes(rows[seq][9]) == first.hash


async def test_concurrent_appends_stay_one_chain(dsns: Dsns) -> None:
    engine = create_async_engine(sqlalchemy_url(dsns.app), pool_size=20, max_overflow=0)
    try:
        org = await new_org(engine)

        async def worker(w: int) -> None:
            for i in range(5):
                async with bound_org(engine, org) as conn:
                    await append_event(conn, schedule_event(org, w * 10 + i))

        await asyncio.gather(*(worker(w) for w in range(20)))
    finally:
        await engine.dispose()
    rows = chain_rows(dsns.app, org)
    assert [r[0] for r in rows] == list(range(1, 102))
    assert len({bytes(r[9]) for r in rows}) == 101
    assert (await run_verify(dsns.app, org)) == VerifyReport(True, 101, 101, None)


# ── export renderers ─────────────────────────────────────────────────────────

GOLDEN_ROW: dict[str, Any] = {
    "seq": 7,
    "at": datetime(2026, 9, 1, 12, 0, tzinfo=timezone(timedelta(hours=2))),
    "action": "schedule.created",
    "actor_kind": "user",
    "actor_id": "usr_a",
    "actor_via_agent": True,
    "actor_client_id": "@agent",
    "target_kind": "schedule",
    "target_id": "=1+2",
    "before": None,
    "after": {"name": "nightly", "cron": "0 3 * * *"},
    "policy_decision_id": None,
    "canonical": b'{"x":1}',
    "prev_hash": bytes(32),
    "hash": bytes([1]) * 32,
}


async def test_csv_rendering_is_fixed_and_escapes_formulas() -> None:
    body = await collect(render_csv(rows_of(GOLDEN_ROW)))
    assert body == (
        b"seq,at,action,actor_kind,actor_id,actor_via_agent,actor_client_id,target_kind,"
        b"target_id,before,after,policy_decision_id,prev_hash,hash\r\n"
        b"7,2026-09-01T10:00:00+00:00,schedule.created,user,usr_a,true,'@agent,schedule,'=1+2,,"
        b'"{""cron"":""0 3 * * *"",""name"":""nightly""}",,'
        + b"0" * 64
        + b","
        + b"01" * 32
        + b"\r\n"
    )


async def test_jsonl_rendering_is_fixed_and_carries_the_canonical_bytes() -> None:
    body = await collect(render_jsonl(rows_of(GOLDEN_ROW, GOLDEN_ROW)))
    line = (
        b'{"action":"schedule.created","actor":{"client_id":"@agent","id":"usr_a","kind":"user",'
        b'"via_agent":true},"after":{"cron":"0 3 * * *","name":"nightly"},'
        b'"at":"2026-09-01T10:00:00+00:00","before":null,"canonical":"eyJ4IjoxfQ==",'
        b'"hash":"' + b"01" * 32 + b'","policy_decision_id":null,'
        b'"prev_hash":"' + b"0" * 64 + b'","seq":7,"target":{"id":"=1+2","kind":"schedule"}}\n'
    )
    assert body == line * 2


# ── search and export over HTTP ──────────────────────────────────────────────


@dataclass(frozen=True)
class Api:
    dsns: Dsns
    client: TestClient
    org: CreatedOrg
    admin: str
    member: str
    retired_admin: str
    workload: str
    operator: str
    agent: str
    other_admin: str


def add_account(dsn: str, org: str, role: str, status: str) -> str:
    uid = new_id("usr")
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        conn.execute(
            "insert into ssc.user_account (id, org_id, display_name, email, role, status, "
            "deactivated_at) values (%s, %s, 'Some One', 'someone@example.com', %s, %s, "
            "case when %s = 'deactivated' then now() end)",
            (uid, org, role, status, status),
        )
    return uid


def api_events(org: str, admin: str) -> list[NewEvent]:
    """Seq 2..7 of the API org, at fixed times so filters and exports are deterministic."""
    user, other = Actor(ActorKind.USER, admin), Actor(ActorKind.USER, "usr_other")
    agent = Actor(ActorKind.USER, admin, via_agent=True, client_id="@evil")
    sched = {"cron": "0 1 * * *"}
    return (
        [
            NewEvent(
                org_id=org,
                action=AuditAction.SCHEDULE_CREATED,
                actor=actor,
                target_kind="schedule",
                target_id=target,
                after={"name": target, **sched},
                at=BASE + timedelta(minutes=n),
            )
            for n, (actor, target) in enumerate(
                [(user, "sch_one"), (user, "sch_two"), (agent, "=1+2")], start=1
            )
        ]
        + [
            NewEvent(
                org_id=org,
                action=AuditAction.APP_CREATED,
                actor=other,
                target_kind="app",
                target_id=f"app_{n}",
                after={"slug": f"app-{n}", "owner_user_id": "usr_other"},
                at=BASE + timedelta(minutes=n),
            )
            for n in (4, 5)
        ]
        + [
            NewEvent(
                org_id=org,
                action=AuditAction.GRANT_ADDED,
                actor=Actor(ActorKind.WORKLOAD, "env_x"),
                target_kind="app_grant",
                target_id="gnt_one",
                after={"environment_id": "env_x", "role": "user", "subject_kind": "org"},
                at=BASE + timedelta(minutes=6),
            )
        ]
    )


@pytest.fixture(scope="module")
def api(dsns: Dsns, signing_key: SigningKey) -> Iterator[Api]:
    settings = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
    )
    org, other = make_org(dsns.app, "Audit Org"), make_org(dsns.app, "Other Org")
    asyncio.run(append_all(dsns.app, api_events(org.org_id, org.admin_user_id)))
    member = add_account(dsns.app, org.org_id, "member", "active")
    retired = add_account(dsns.app, org.org_id, "admin", "deactivated")

    def token(jti: str, sub: str, **claims: Any) -> str:
        return mint(signing_key, org=org.org_id, sub=sub, jti=f"cred_audit_{jti}", **claims)

    with TestClient(create_app(settings)) as client:
        yield Api(
            dsns=dsns,
            client=client,
            org=org,
            admin=token("admin", org.admin_user_id),
            member=token("member", member),
            retired_admin=token("retired", retired),
            workload=token("workload", new_id("env"), kind="workload"),
            operator=token("operator", "op_1", kind="operator"),
            agent=token("agent", org.admin_user_id, agent=True, client_id="agent-x"),
            other_admin=mint(
                signing_key, org=other.org_id, sub=other.admin_user_id, jti="cred_audit_other"
            ),
        )


def search(api: Api, token: str | None = None, **params: object) -> Any:
    return api.client.get("/v1/audit", params=params, headers=auth(token or api.admin))


def seqs(body: dict[str, Any]) -> list[int]:
    return [e["seq"] for e in body["events"]]


def test_search_pages_newest_first_until_the_cursor_runs_out(api: Api) -> None:
    expected = [r[0] for r in reversed(chain_rows(api.dsns.app, api.org.org_id))]
    seen: list[int] = []
    before: int | None = None
    while True:
        r = search(api, limit=2, **({} if before is None else {"before_seq": before}))
        assert r.status_code == 200, r.text
        page = r.json()
        seen += seqs(page)
        before = page["next_before_seq"]
        if before is None:
            break
        assert before == seen[-1]
    assert seen == expected


def test_search_shows_the_chain_fields_and_never_an_ip(api: Api) -> None:
    [event] = search(api, target_id="=1+2").json()["events"]
    row = next(r for r in chain_rows(api.dsns.app, api.org.org_id) if r[5] == "=1+2")
    assert event == {
        "seq": row[0],
        "at": "2026-09-01T00:03:00Z",
        "action": "schedule.created",
        "actor": {
            "kind": "user",
            "id": api.org.admin_user_id,
            "via_agent": True,
            "client_id": "@evil",
        },
        "target": {"kind": "schedule", "id": "=1+2"},
        "before": None,
        "after": {"name": "=1+2", "cron": "0 1 * * *"},
        "policy_decision_id": None,
        "prev_hash": bytes(row[9]).hex(),
        "hash": bytes(row[10]).hex(),
    }


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"action": "schedule.created"}, [4, 3, 2]),
        ({"actor_kind": "user", "actor_id": "usr_other"}, [6, 5]),
        ({"actor_kind": "workload"}, [7]),
        ({"target_kind": "schedule", "target_id": "sch_two"}, [3]),
        ({"since": "2026-09-01T00:02:00Z", "until": "2026-09-01T00:04:00Z"}, [4, 3]),
        ({"since": "2026-09-01T02:02:00+02:00", "until": "2026-09-01T00:03:00Z"}, [3]),
        ({"action": "schedule.created", "before_seq": 4, "limit": 1}, [3]),
    ],
)
def test_search_filters(api: Api, params: dict[str, object], expected: list[int]) -> None:
    r = search(api, **params)
    assert r.status_code == 200, r.text
    assert seqs(r.json()) == expected


def test_search_sees_only_the_callers_org(api: Api) -> None:
    body = search(api, api.other_admin).json()
    assert [(e["seq"], e["action"]) for e in body["events"]] == [(1, "org.created")]


@pytest.mark.parametrize(
    "who", ["member", "retired_admin", "workload", "operator"], ids=lambda w: str(w)
)
def test_search_is_for_active_org_admins(api: Api, who: str) -> None:
    assert_problem(search(api, getattr(api, who)), ErrorCode.FORBIDDEN)


def test_search_needs_a_credential(api: Api) -> None:
    assert_problem(api.client.get("/v1/audit"), ErrorCode.UNAUTHENTICATED)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 501},
        {"before_seq": 0},
        {"since": "2026-09-01T00:00:00"},
        {"action": "made.up"},
        {"actor_kind": "robot"},
        {"unknown": "1"},
    ],
    ids=["limit 0", "limit 501", "before_seq 0", "naive since", "action", "actor kind", "extra"],
)
def test_search_validates_its_query(api: Api, params: dict[str, object]) -> None:
    assert_problem(search(api, **params), ErrorCode.VALIDATION_FAILED)


def export(api: Api, token: str | None = None, **params: object) -> Any:
    return api.client.get("/v1/audit/export", params=params, headers=auth(token or api.admin))


def exported_rows(api: Api) -> list[tuple[Any, ...]]:
    return [r for r in chain_rows(api.dsns.app, api.org.org_id) if r[1] == "audit.exported"]


def test_csv_export_is_stable_escaped_and_has_no_ip(api: Api) -> None:
    first = export(api, format="csv", target_kind="schedule")
    second = export(api, format="csv", target_kind="schedule")
    assert first.status_code == 200, first.text
    assert first.content == second.content
    assert first.headers["content-type"] == "text/csv; charset=utf-8"
    assert first.headers["content-disposition"] == (
        f'attachment; filename="audit-{api.org.org_id}.csv"'
    )
    assert first.headers["cache-control"] == "no-store"
    table = list(csv.reader(io.StringIO(first.content.decode())))
    assert tuple(table[0]) == CSV_COLUMNS
    assert "actor_ip" not in table[0]
    assert [row[0] for row in table[1:]] == ["2", "3", "4"]
    formula = table[3]
    assert formula[CSV_COLUMNS.index("target_id")] == "'=1+2"
    assert formula[CSV_COLUMNS.index("actor_client_id")] == "'@evil"


def test_jsonl_export_reverifies_offline_and_records_itself(api: Api) -> None:
    r = export(api, format="jsonl")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/x-ndjson"
    lines = [json.loads(line) for line in r.content.decode().splitlines()]
    prev = GENESIS_HASH
    for n, line in enumerate(lines, start=1):
        canonical = base64.b64decode(line["canonical"])
        assert line["seq"] == n
        assert bytes.fromhex(line["prev_hash"]) == prev
        assert hashlib.sha256(prev + canonical).hexdigest() == line["hash"]
        doc = json.loads(canonical)
        assert set(doc) == V1_KEYS
        assert doc["actor"]["ip"] is None
        assert "actor_ip" not in line
        prev = bytes.fromhex(line["hash"])
    assert lines[-1]["action"] == "audit.exported"
    assert lines[-1]["after"] == {"format": "jsonl", "filters": {}}


def test_export_is_recorded_with_its_filters(api: Api) -> None:
    before = len(exported_rows(api))
    r = export(api, format="csv", action="app.created", since="2026-09-01T00:00:00Z")
    assert r.status_code == 200, r.text
    rows = exported_rows(api)
    assert len(rows) == before + 1
    row = rows[-1]
    assert row[2:6] == ("user", api.org.admin_user_id, "audit", api.org.org_id)
    assert row[7] == {
        "format": "csv",
        "filters": {"action": "app.created", "since": "2026-09-01T00:00:00+00:00"},
    }


@pytest.mark.parametrize("who", ["agent", "member", "workload", "operator"], ids=lambda w: str(w))
def test_export_is_for_admins_in_their_own_session(api: Api, who: str) -> None:
    before = len(exported_rows(api))
    assert_problem(export(api, getattr(api, who), format="csv"), ErrorCode.FORBIDDEN)
    assert len(exported_rows(api)) == before


@pytest.mark.parametrize("params", [{}, {"format": "xml"}], ids=["no format", "xml"])
def test_export_validates_its_format(api: Api, params: dict[str, object]) -> None:
    assert_problem(export(api, **params), ErrorCode.VALIDATION_FAILED)


def test_grant_removed_records_the_view_not_the_row_id(api: Api) -> None:
    created = api.client.post(
        "/v1/apps",
        json={"slug": "audited-view"},
        headers=auth(api.admin, **{IDEMPOTENCY_HEADER: new_key()}),
    )
    assert created.status_code == 201, created.text
    app = created.json()
    env = next(e for e in app["environments"] if e["name"] == "prod")["id"]
    url = f"/v1/apps/{app['id']}/environments/{env}/grants"
    added = api.client.put(
        url,
        json={"grants": [{"role": "user", "subject_kind": "org"}]},
        headers=auth(api.admin, **{"If-Match": '"1"'}),
    )
    assert added.status_code == 200, added.text
    removed = api.client.put(
        url, json={"grants": []}, headers=auth(api.admin, **{"If-Match": added.headers["ETag"]})
    )
    assert removed.status_code == 200, removed.text
    grant_id = added.json()["grants"][0]["id"]
    [row] = [
        r
        for r in chain_rows(api.dsns.app, api.org.org_id)
        if r[1] == "grant.removed" and r[5] == grant_id
    ]
    assert row[6] == {
        "environment_id": env,
        "role": "user",
        "subject_kind": "org",
        "subject_id": None,
    }
    assert asyncio.run(run_verify(api.dsns.app, api.org.org_id)).ok
