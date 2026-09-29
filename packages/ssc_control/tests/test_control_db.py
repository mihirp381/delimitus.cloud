"""SSC-010: the control database enforces customer separation itself.

Everything here attacks the schema through raw SQL as the application role, because a rule that
holds only in application code holds until the first migration script. Connecting as a
non-owner role without BYPASSRLS is the whole value of the fixture: as the superuser every
isolation assertion would pass for the wrong reason.

Ticket "done when" checks:
  * no org bound gives SC001                      -> test_unbound_query_raises_sc001
  * a cross-org insert is refused                 -> test_cross_org_insert_is_refused
  * the app role cannot delete the migration ledger
                                  -> test_app_role_has_nothing_on_the_migration_ledger
  * a release update is refused                   -> test_release_rows_cannot_change
  * a second in-flight deployment is refused
                                  -> test_only_one_deployment_in_flight_per_environment
Plus the Delimitus adversarial set (last admin, scope leak past COMMIT, WITH CHECK misfile) and
catalog tests that pin the declared shape: RLS everywhere, the PL/pgSQL list, privileges, PII.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from testcontainers.postgres import PostgresContainer

import ssc_control.db
from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.ids import PREFIXES, new_id
from ssc_control.db import (
    APP_ROLE,
    MIGRATE_ROLE,
    CreatedOrg,
    NewOrg,
    SqlState,
    bind_org_sync,
    bound_org,
    catalog,
    check_org_id,
    create_org,
    downgrade,
    ensure_roles,
    make_engine,
    upgrade,
)
from ssc_control.db.errors import (
    CHECK_VIOLATION,
    FOREIGN_KEY_VIOLATION,
    INSUFFICIENT_PRIVILEGE,
    NOT_NULL_VIOLATION,
    UNIQUE_VIOLATION,
)
from ssc_control.db.orgs import GENESIS_HASH

DB_DIR = Path(ssc_control.db.__file__).parent


# ── fixture ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Dsns:
    superuser: str
    migrate: str
    app: str


def with_role(dsn: str, user: str, password: str) -> str:
    return make_url(dsn).set(username=user, password=password).render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def dsns() -> Iterator[Dsns]:
    with PostgresContainer("postgres:18", driver=None) as pg:
        su = pg.get_connection_url()
        with psycopg.connect(su, autocommit=True) as conn:
            ensure_roles(conn)
            conn.execute(f"alter role {MIGRATE_ROLE} login password 'migrate'")
            conn.execute(f"alter role {APP_ROLE} login password 'app'")
            conn.execute(f"grant create on database {pg.dbname} to {MIGRATE_ROLE}")
        d = Dsns(su, with_role(su, MIGRATE_ROLE, "migrate"), with_role(su, APP_ROLE, "app"))
        upgrade(d.migrate)
        yield d


def make_org(dsn: str, name: str = "Acme") -> CreatedOrg:
    async def go() -> CreatedOrg:
        engine = make_engine(dsn)
        try:
            spec = NewOrg(
                name, "Ada Admin", "ada@example.com", "https://idp.example", new_id("usr")
            )
            return await create_org(engine, spec)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def add_user(
    conn: psycopg.Connection[Any], org: str, role: str = "member", status: str = "active"
) -> str:
    uid = new_id("usr")
    conn.execute(
        "insert into ssc.user_account "
        "(id, org_id, display_name, email, role, status, deactivated_at) "
        "values (%s, %s, 'Some One', 'someone@example.com', %s, %s, "
        "case when %s = 'deactivated' then now() end)",
        (uid, org, role, status, status),
    )
    return uid


def add_app(conn: psycopg.Connection[Any], org: str, owner: str | None, slug: str) -> str:
    aid = new_id("app")
    conn.execute(
        "insert into ssc.app (id, org_id, slug, owner_user_id) values (%s, %s, %s, %s)",
        (aid, org, slug, owner),
    )
    return aid


def add_env(conn: psycopg.Connection[Any], org: str, app: str, name: str = "prod") -> str:
    eid = new_id("env")
    conn.execute(
        "insert into ssc.environment (id, org_id, app_id, name) values (%s, %s, %s, %s)",
        (eid, org, app, name),
    )
    return eid


def add_release(conn: psycopg.Connection[Any], org: str, app: str, number: int) -> str:
    rid = new_id("rel")
    conn.execute(
        "insert into ssc.release (id, org_id, app_id, number, image_digest, manifest_digest, "
        "source_digest, actor_kind, actor_id) values (%s, %s, %s, %s, %s, %s, %s, 'user', %s)",
        (
            rid,
            org,
            app,
            number,
            digest(f"img{rid}"),
            digest(f"man{rid}"),
            digest(f"src{rid}"),
            new_id("usr"),
        ),
    )
    return rid


def add_deployment(
    conn: psycopg.Connection[Any], org: str, app: str, env: str, rel: str, state: str = "pending"
) -> str:
    did = new_id("dep")
    conn.execute(
        "insert into ssc.deployment (id, org_id, app_id, environment_id, release_id, kind, state, "
        "config_version, grants_version, actor_kind, actor_id) "
        "values (%s, %s, %s, %s, %s, 'deploy', %s, 1, 1, 'user', %s)",
        (did, org, app, env, rel, state, new_id("usr")),
    )
    return did


@dataclass(frozen=True)
class SeededOrg:
    org: str
    admin: str
    app: str
    env: str
    release: str


def seed(dsn: str, name: str, slug: str) -> SeededOrg:
    created = make_org(dsn, name)
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, created.org_id)
        app = add_app(conn, created.org_id, created.admin_user_id, slug)
        env = add_env(conn, created.org_id, app)
        rel = add_release(conn, created.org_id, app, 1)
    return SeededOrg(created.org_id, created.admin_user_id, app, env, rel)


@pytest.fixture(scope="module")
def orgs(dsns: Dsns) -> tuple[SeededOrg, SeededOrg]:
    return seed(dsns.app, "Org A", "alpha"), seed(dsns.app, "Org B", "beta")


def run(
    dsn: str, org: str | None, sql: str, params: Sequence[object] = ()
) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        if org:
            bind_org_sync(conn, org)
        return conn.execute(sql, params).fetchall()


def refused(dsn: str, org: str | None, sql: str, params: Sequence[object] = ()) -> str:
    """Run one statement in a (bound) transaction and return the SQLSTATE it fails with."""
    with psycopg.connect(dsn) as conn:
        if org:
            bind_org_sync(conn, org)
        with pytest.raises(psycopg.Error) as e:
            conn.execute(sql, params)
        conn.rollback()
    assert e.value.sqlstate, e.value
    return e.value.sqlstate


def sqlstate(e: pytest.ExceptionInfo[psycopg.Error]) -> str:
    assert e.value.sqlstate, e.value
    return e.value.sqlstate


# ── isolation ────────────────────────────────────────────────────────────────


def test_unbound_query_raises_sc001(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    # Zero rows with HTTP 200 is the failure mode this guards against.
    assert refused(dsns.app, None, "select count(*) from ssc.app") == SqlState.NO_ORG_BOUND
    # FORCE means the owner is not exempt either.
    assert refused(dsns.migrate, None, "select count(*) from ssc.app") == SqlState.NO_ORG_BOUND


def test_bound_query_sees_only_its_own_org(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    a, b = orgs
    assert {r[0] for r in run(dsns.app, a.org, "select id from ssc.app")} == {a.app}
    assert {r[0] for r in run(dsns.app, b.org, "select id from ssc.app")} == {b.app}
    assert run(dsns.app, a.org, "select id from ssc.org") == [(a.org,)]
    assert run(dsns.app, b.org, "select count(*) from ssc.user_account") == [(1,)]


def test_cross_org_insert_is_refused(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    a, b = orgs
    code = refused(
        dsns.app,
        b.org,
        "insert into ssc.user_group (id, org_id, directory_ref, display_name) "
        "values (%s, %s, 'dir-x', 'Sneaky')",
        (new_id("grp"), a.org),
    )
    assert code == INSUFFICIENT_PRIVILEGE  # WITH CHECK: refused, not silently misfiled
    assert run(dsns.app, a.org, "select count(*) from ssc.user_group") == [(0,)]


def test_cross_org_update_touches_nothing(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    a, b = orgs
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, b.org)
        cur = conn.execute("update ssc.app set status = 'disabled' where id = %s", (a.app,))
        assert cur.rowcount == 0
    assert run(dsns.app, a.org, "select status from ssc.app where id = %s", (a.app,)) == [
        ("active",)
    ]


def test_bind_does_not_survive_the_transaction(
    dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]
) -> None:
    # set_config(..., true) dies with COMMIT. A session-scoped bind would hand this connection's
    # next borrower another customer's scope.
    a, _ = orgs
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        assert conn.execute("select count(*) from ssc.app").fetchone() == (1,)
        conn.commit()
        with pytest.raises(psycopg.Error) as e:
            conn.execute("select count(*) from ssc.app")
        assert sqlstate(e) == SqlState.NO_ORG_BOUND
        conn.rollback()


def test_bind_rejects_anything_but_an_org_id() -> None:
    with pytest.raises(ValueError):
        check_org_id(new_id("usr"))
    with pytest.raises(ValueError):
        check_org_id("org_notlongenough")
    with pytest.raises(ValueError):
        check_org_id("")


async def test_bound_org_scopes_queries_and_releases_the_bind(
    dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]
) -> None:
    a, b = orgs
    engine = make_engine(dsns.app)
    try:
        async with bound_org(engine, a.org) as conn:
            ids = set((await conn.execute(text("select id from ssc.app"))).scalars().all())
        assert a.app in ids and b.app not in ids
        async with engine.connect() as conn:  # the pooled connection comes back unbound
            with pytest.raises(DBAPIError) as e:
                await conn.execute(text("select count(*) from ssc.app"))
        assert getattr(e.value.orig, "sqlstate", None) == SqlState.NO_ORG_BOUND
    finally:
        await engine.dispose()


# ── the migration ledger ─────────────────────────────────────────────────────


def test_app_role_has_nothing_on_the_migration_ledger(dsns: Dsns) -> None:
    for sql in (
        "select * from ssc.alembic_version",
        "delete from ssc.alembic_version",
        "update ssc.alembic_version set version_num = 'x'",
        "insert into ssc.alembic_version values ('x')",
    ):
        assert refused(dsns.app, None, sql) == INSUFFICIENT_PRIVILEGE, sql
    assert run(dsns.migrate, None, "select version_num from ssc.alembic_version") == [
        ("0001_control_schema",)
    ]


# ── immutability and state rules ─────────────────────────────────────────────


def test_release_rows_cannot_change(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    a, _ = orgs
    update = "update ssc.release set number = 99 where id = %s"
    delete = "delete from ssc.release where id = %s"
    # First guard: the app role has no such privilege.
    assert refused(dsns.app, a.org, update, (a.release,)) == INSUFFICIENT_PRIVILEGE
    assert refused(dsns.app, a.org, delete, (a.release,)) == INSUFFICIENT_PRIVILEGE
    assert refused(dsns.app, a.org, "truncate ssc.release cascade") == INSUFFICIENT_PRIVILEGE
    # Second guard: the owner is refused by the trigger.
    assert refused(dsns.migrate, a.org, update, (a.release,)) == SqlState.RELEASE_IMMUTABLE
    assert refused(dsns.migrate, a.org, delete, (a.release,)) == SqlState.RELEASE_IMMUTABLE
    assert refused(dsns.migrate, a.org, "truncate ssc.release cascade") == SqlState.TRUNCATE_REFUSED
    assert run(dsns.app, a.org, "select number from ssc.release where id = %s", (a.release,)) == [
        (1,)
    ]


def test_only_one_deployment_in_flight_per_environment(
    dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]
) -> None:
    a, _ = orgs
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        first = add_deployment(conn, a.org, a.app, a.env, a.release, "pending")
        with pytest.raises(psycopg.Error) as e, conn.transaction():
            add_deployment(conn, a.org, a.app, a.env, a.release, "running")
        assert sqlstate(e) == UNIQUE_VIOLATION
        assert "deployment_one_in_flight" in str(e.value)
        # Once the first finishes, the next may start.
        conn.execute(
            "update ssc.deployment set state = 'healthy', finished_at = now() where id = %s",
            (first,),
        )
        second = add_deployment(conn, a.org, a.app, a.env, a.release, "pending")
        conn.execute(
            "update ssc.deployment set state = 'superseded', finished_at = now() where id = %s",
            (second,),
        )


def test_release_and_environment_must_belong_to_the_same_app(
    dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]
) -> None:
    a, _ = orgs
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        other_app = add_app(conn, a.org, a.admin, "gamma")
        other_env = add_env(conn, a.org, other_app)
        with pytest.raises(psycopg.Error) as e, conn.transaction():
            add_deployment(conn, a.org, other_app, other_env, a.release)  # alpha's release
        assert sqlstate(e) == FOREIGN_KEY_VIOLATION
        conn.execute("delete from ssc.app where id = %s", (other_app,))


def test_last_active_admin_cannot_be_removed(dsns: Dsns) -> None:
    org = make_org(dsns.app, "Solo")
    for sql in (
        "update ssc.user_account set role = 'member' where id = %s",
        "update ssc.user_account set status = 'deactivated', deactivated_at = now() where id = %s",
        "delete from ssc.user_account where id = %s",
    ):
        assert (
            refused(dsns.app, org.org_id, sql, (org.admin_user_id,)) == SqlState.LAST_ORG_ADMIN
        ), sql
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, org.org_id)
        second = add_user(conn, org.org_id, role="admin")
        conn.execute(
            "update ssc.user_account set role = 'member' where id = %s", (org.admin_user_id,)
        )
        with pytest.raises(psycopg.Error) as e, conn.transaction():
            conn.execute("delete from ssc.user_account where id = %s", (second,))
        assert sqlstate(e) == SqlState.LAST_ORG_ADMIN


def test_two_admins_cannot_remove_each_other_at_once(dsns: Dsns) -> None:
    # A read-then-write count is not a check: both would read "2 admins" and both proceed.
    # The trigger locks the org row, so the second waits for the first and then sees one admin.
    org = make_org(dsns.app, "Pair")
    with psycopg.connect(dsns.app) as setup:
        bind_org_sync(setup, org.org_id)
        second = add_user(setup, org.org_id, role="admin")

    first = psycopg.connect(dsns.app)
    bind_org_sync(first, org.org_id)
    first.execute("update ssc.user_account set role = 'member' where id = %s", (org.admin_user_id,))

    outcome: dict[str, str | None] = {}

    def demote_second() -> None:
        with psycopg.connect(dsns.app) as other:
            bind_org_sync(other, org.org_id)
            try:
                other.execute(
                    "update ssc.user_account set role = 'member' where id = %s", (second,)
                )
                other.commit()
                outcome["code"] = None
            except psycopg.Error as e:
                outcome["code"] = e.sqlstate

    t = threading.Thread(target=demote_second)
    t.start()
    t.join(timeout=2)
    assert t.is_alive(), "the second demotion did not wait for the first transaction's lock"
    first.commit()
    first.close()
    t.join(timeout=10)
    assert outcome["code"] == SqlState.LAST_ORG_ADMIN
    assert run(
        dsns.app, org.org_id, "select count(*) from ssc.user_account where role = 'admin'"
    ) == [(1,)]


def test_app_owner_must_be_an_active_member(dsns: Dsns) -> None:
    org = make_org(dsns.app, "Owners")
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, org.org_id)
        leaver = add_user(conn, org.org_id, status="deactivated")
        for owner, code in (
            (leaver, SqlState.OWNER_NOT_ACTIVE),
            (new_id("usr"), SqlState.OWNER_NOT_ACTIVE),
            (None, NOT_NULL_VIOLATION),
        ):
            with pytest.raises(psycopg.Error) as e, conn.transaction():
                add_app(conn, org.org_id, owner, "orphan")
            assert sqlstate(e) == code
        member = add_user(conn, org.org_id)
        app = add_app(conn, org.org_id, member, "kept")
        with pytest.raises(psycopg.Error) as e, conn.transaction():
            conn.execute("update ssc.app set owner_user_id = %s where id = %s", (leaver, app))
        assert sqlstate(e) == SqlState.OWNER_NOT_ACTIVE
        # Deactivating an owner later is allowed: directory sync must never be blocked.
        conn.execute(
            "update ssc.user_account set status = 'deactivated', deactivated_at = now() "
            "where id = %s",
            (member,),
        )
        assert conn.execute(
            "select owner_user_id from ssc.app where id = %s", (app,)
        ).fetchone() == (member,)


def test_audit_log_is_append_only(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    a, _ = orgs
    h1, h2 = hashlib.sha256(b"1").digest(), hashlib.sha256(b"2").digest()
    insert = (
        "insert into ssc.audit_event (org_id, seq, action, actor_kind, actor_id, target_kind, "
        "target_id, canonical, prev_hash, hash) "
        "values (%s, %s, %s, 'operator', 'op-1', 'org', %s, %s, %s, %s)"
    )
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        conn.execute(insert, (a.org, 1, AuditAction.ORG_CREATED, a.org, b"{}", GENESIS_HASH, h1))
        conn.execute("update ssc.audit_head set seq = 1, hash = %s where org_id = %s", (h1, a.org))
    # A fork (two events claiming the same predecessor) cannot be stored.
    fork = refused(
        dsns.app,
        a.org,
        insert,
        (a.org, 2, AuditAction.LOGIN_SUCCEEDED, a.org, b"{}", GENESIS_HASH, h2),
    )
    assert fork == UNIQUE_VIOLATION
    # Actions come from the closed list.
    made_up = refused(dsns.app, a.org, insert, (a.org, 2, "made.up", a.org, b"{}", h1, h2))
    assert made_up == CHECK_VIOLATION
    update = "update ssc.audit_event set action = 'login.failed' where org_id = %s and seq = 1"
    delete = "delete from ssc.audit_event where org_id = %s and seq = 1"
    assert refused(dsns.app, a.org, update, (a.org,)) == INSUFFICIENT_PRIVILEGE
    assert refused(dsns.app, a.org, delete, (a.org,)) == INSUFFICIENT_PRIVILEGE
    assert refused(dsns.app, a.org, "truncate ssc.audit_event") == INSUFFICIENT_PRIVILEGE
    assert refused(dsns.migrate, a.org, update, (a.org,)) == SqlState.AUDIT_IMMUTABLE
    assert refused(dsns.migrate, a.org, delete, (a.org,)) == SqlState.AUDIT_IMMUTABLE
    assert refused(dsns.migrate, a.org, "truncate ssc.audit_event") == SqlState.TRUNCATE_REFUSED
    assert refused(dsns.migrate, a.org, "truncate ssc.audit_head") == SqlState.TRUNCATE_REFUSED


def test_approvals_need_another_person_and_never_an_agent(dsns: Dsns) -> None:
    org = make_org(dsns.app, "Approvals")
    insert = (
        "insert into ssc.approval_request (id, org_id, kind, requested_by_user_id, state, "
        "decided_by_user_id, decided_at, decided_via_agent) "
        "values (%s, %s, 'connection', %s, %s, %s, %s, %s)"
    )
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, org.org_id)
        approver = add_user(conn, org.org_id, role="admin")
        me = org.admin_user_id
        at = "2026-09-28T00:00:00Z"
        bad = (
            ("approved", me, at, False),  # self-approval
            ("approved", approver, at, True),  # approved through an agent
            ("pending", approver, at, False),  # pending with a decider
            ("approved", approver, None, False),  # approved without a time
        )
        for state, decider, decided_at, via_agent in bad:
            with pytest.raises(psycopg.Error) as e, conn.transaction():
                conn.execute(
                    insert, (new_id("apr"), org.org_id, me, state, decider, decided_at, via_agent)
                )
            assert sqlstate(e) == CHECK_VIOLATION, (state, decider, decided_at, via_agent)
        conn.execute(insert, (new_id("apr"), org.org_id, me, "approved", approver, at, False))
        # The requester may withdraw their own request.
        conn.execute(insert, (new_id("apr"), org.org_id, me, "cancelled", me, at, False))


def test_deleted_schedule_is_terminal(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    a, _ = orgs
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        sid = new_id("sch")
        conn.execute(
            "insert into ssc.schedule (id, org_id, environment_id, name, cron) "
            "values (%s, %s, %s, 'nightly', '0 2 * * *')",
            (sid, a.org, a.env),
        )
        conn.execute("update ssc.schedule set state = 'paused' where id = %s", (sid,))
        conn.execute("update ssc.schedule set state = 'deleted' where id = %s", (sid,))
        for sql in (
            "update ssc.schedule set state = 'active' where id = %s",
            "update ssc.schedule set cron = '* * * * *' where id = %s",
        ):
            with pytest.raises(psycopg.Error) as e, conn.transaction():
                conn.execute(sql, (sid,))
            assert sqlstate(e) == SqlState.SCHEDULE_DELETED


def test_create_org_is_atomic(dsns: Dsns) -> None:
    before = run(
        dsns.superuser, None, "select count(*) from ssc.org, ssc.user_account, ssc.audit_head"
    )
    with pytest.raises(IntegrityError):
        make_org(dsns.app, name="")  # the org row itself violates its CHECK
    after = run(
        dsns.superuser, None, "select count(*) from ssc.org, ssc.user_account, ssc.audit_head"
    )
    assert before == after


# ── catalog: the declared shape is the real shape ────────────────────────────


def catalog_rows(dsns: Dsns, sql: str, params: Sequence[object] = ()) -> list[tuple[Any, ...]]:
    return run(dsns.superuser, None, sql, params)


def test_every_table_is_declared_and_org_scoped(dsns: Dsns) -> None:
    tables = {
        r[0] for r in catalog_rows(dsns, "select tablename from pg_tables where schemaname = 'ssc'")
    }
    assert tables == catalog.TABLES | {catalog.MIGRATION_LEDGER}
    columns = catalog_rows(
        dsns,
        "select table_name, column_name from information_schema.columns where table_schema = 'ssc'",
    )
    by_table: dict[str, set[str]] = {}
    for t, c in columns:
        by_table.setdefault(t, set()).add(c)
    for t in catalog.TABLES - {"org"}:
        assert "org_id" in by_table[t], t
    # Every table another table may point at exposes (org_id, id), so FKs are org-scoped pairs.
    constraints = catalog_rows(
        dsns,
        "select c.conrelid::regclass::text, pg_get_constraintdef(c.oid) from pg_constraint c "
        "join pg_namespace n on n.oid = c.connamespace where n.nspname = 'ssc'",
    )
    defs: dict[str, list[str]] = {}
    for rel, d in constraints:
        defs.setdefault(rel.removeprefix("ssc."), []).append(d)
    for t in catalog.TABLES - {"org", "group_member", "audit_event", "audit_head", "metrics_event"}:
        assert any(d in ("UNIQUE (org_id, id)", "PRIMARY KEY (org_id, id)") for d in defs[t]), t
    # Every foreign key that is not to org(id) carries org_id.
    for t, ds in defs.items():
        for d in ds:
            if d.startswith("FOREIGN KEY") and "REFERENCES ssc.org(id)" not in d:
                assert d.startswith("FOREIGN KEY (org_id,"), (t, d)


def test_ids_are_type_prefixed_per_table(dsns: Dsns) -> None:
    checks = catalog_rows(
        dsns,
        "select c.conrelid::regclass::text, pg_get_constraintdef(c.oid) from pg_constraint c "
        "join pg_namespace n on n.oid = c.connamespace where n.nspname = 'ssc' and c.contype = 'c'",
    )
    prefixed: dict[str, str] = {}
    for rel, d in checks:
        m = re.search(r"\(id ~ '\^([a-z]+)_\[a-z0-9\]\{20\}\$'", d)
        if m:
            prefixed[rel.removeprefix("ssc.")] = m.group(1)
    expected = catalog.TABLES - {"group_member", "audit_event", "audit_head", "metrics_event"}
    assert set(prefixed) == expected
    assert set(prefixed.values()) <= set(PREFIXES), prefixed


def test_rls_is_enabled_and_forced_everywhere(dsns: Dsns) -> None:
    rows = catalog_rows(
        dsns,
        "select c.relname, c.relrowsecurity, c.relforcerowsecurity, "
        "(select count(*) from pg_policy p where p.polrelid = c.oid) "
        "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'ssc' and c.relkind = 'r' and c.relname <> %s",
        (catalog.MIGRATION_LEDGER,),
    )
    assert {r[0] for r in rows} == catalog.TABLES
    for name, enabled, forced, policies in rows:
        assert (enabled, forced, policies) == (True, True, 1), name
    policies = catalog_rows(
        dsns,
        "select c.relname, p.polcmd, p.polroles, pg_get_expr(p.polqual, p.polrelid), "
        "pg_get_expr(p.polwithcheck, p.polrelid) "
        "from pg_policy p join pg_class c on c.oid = p.polrelid",
    )
    for name, cmd, roles, using, check in policies:
        column = "id" if name == "org" else "org_id"
        assert cmd == "*" and roles == [0], name  # ALL commands, applies to every role
        assert using == check == f"({column} = ssc.current_org())", name


def test_plpgsql_is_exactly_the_allowed_set(dsns: Dsns) -> None:
    rows = catalog_rows(
        dsns,
        "select p.proname, l.lanname, p.prosecdef, p.proowner::regrole::text from pg_proc p "
        "join pg_namespace n on n.oid = p.pronamespace join pg_language l on l.oid = p.prolang "
        "where n.nspname = 'ssc'",
    )
    assert {r[0] for r in rows} == catalog.PLPGSQL_FUNCTIONS
    assert len(rows) <= catalog.PLPGSQL_LIMIT
    doc = (DB_DIR / "PLPGSQL.md").read_text()
    for name, lang, secdef, owner in rows:
        assert lang == "plpgsql", (name, lang)  # LANGUAGE sql cannot raise a chosen SQLSTATE
        assert not secdef, name  # no privilege escalation through functions
        assert owner == MIGRATE_ROLE, name
        assert f"`{name}(" in doc, f"{name} is not documented in PLPGSQL.md"


def test_app_role_privileges_match_the_declared_matrix(dsns: Dsns) -> None:
    acl = catalog_rows(
        dsns,
        "select c.relname, c.relkind, a.grantee::regrole::text, a.privilege_type "
        "from pg_class c join pg_namespace n on n.oid = c.relnamespace, aclexplode(c.relacl) a "
        "where n.nspname = 'ssc' and c.relkind in ('r', 'S')",
    )
    grantees = {g for _, _, g, _ in acl}
    assert grantees == {MIGRATE_ROLE, APP_ROLE}, (
        grantees
    )  # nothing to PUBLIC, nothing to anyone else
    app_privs: dict[str, set[str]] = {}
    for name, kind, grantee, priv in acl:
        if grantee == APP_ROLE and kind == "r":
            app_privs.setdefault(name, set()).add(priv)
    assert app_privs == {t: set(p) for t, p in catalog.APP_ROLE_PRIVILEGES.items()}
    assert catalog.MIGRATION_LEDGER not in app_privs
    sequences = {name: privs for name, kind, g, privs in acl if kind == "S" and g == APP_ROLE}
    assert set(sequences) == {"metrics_event_id_seq"}
    owners = catalog_rows(
        dsns, "select tablename, tableowner from pg_tables where schemaname = 'ssc'"
    )
    assert {o for _, o in owners} == {MIGRATE_ROLE}
    roles = catalog_rows(
        dsns,
        "select rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
        "from pg_roles where rolname in (%s, %s)",
        (APP_ROLE, MIGRATE_ROLE),
    )
    assert len(roles) == 2
    for name, *flags in roles:
        assert flags == [False, False, False, False], name


def test_pii_columns_are_exactly_the_declared_ones(dsns: Dsns) -> None:
    columns = catalog_rows(
        dsns,
        "select table_name, column_name from information_schema.columns where table_schema = 'ssc'",
    )
    found = {(t, c) for t, c in columns if c in catalog.PII_COLUMN_NAMES}
    assert found == catalog.PII_COLUMNS
    assert {(t, c) for t, c in columns if c in catalog.FORBIDDEN_COLUMN_NAMES} == set()
    doc = (DB_DIR / "PII.md").read_text()
    for t, c in catalog.PII_COLUMNS:
        assert f"| `{t}` | `{c}` |" in doc, f"{t}.{c} is not documented in PII.md"


def test_closed_lists_match_the_contracts(dsns: Dsns) -> None:
    checks = catalog_rows(
        dsns,
        "select pg_get_constraintdef(oid) from pg_constraint "
        "where conrelid = 'ssc.audit_event'::regclass and contype = 'c'",
    )
    (action_check,) = [d for (d,) in checks if d.startswith("CHECK ((action ")]
    assert set(re.findall(r"'([a-z_]+\.[a-z_]+)'", action_check)) == {a.value for a in AuditAction}
    (actor_check,) = [d for (d,) in checks if d.startswith("CHECK ((actor_kind ")]
    assert set(re.findall(r"'([a-z]+)'", actor_check)) == {k.value for k in ActorKind}


def test_no_session_scoped_bind_anywhere() -> None:
    # set_config's third argument must be true (transaction-local) in every SQL we ship.
    for path in list(DB_DIR.rglob("*.py")) + list(DB_DIR.rglob("*.sql")):
        for m in re.finditer(r"set_config\(([^)]*)\)", path.read_text()):
            args = [a.strip() for a in m.group(1).split(",")]
            assert args[-1] == "true", (
                f"{path.name}: set_config({m.group(1)}) is not transaction-scoped"
            )


def test_downgrade_then_upgrade_round_trips(dsns: Dsns) -> None:
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database roundtrip owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database="roundtrip").render_as_string(hide_password=False)
    count = (
        "select count(*) from pg_tables where schemaname = 'ssc' and tablename <> 'alembic_version'"
    )
    upgrade(dsn)
    assert run(dsn, None, count) == [(len(catalog.TABLES),)]
    downgrade(dsn)
    assert run(dsn, None, count) == [(0,)]
    assert run(
        dsn,
        None,
        "select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = 'ssc'",
    ) == [(0,)]
    upgrade(dsn)
    assert run(dsn, None, count) == [(len(catalog.TABLES),)]
