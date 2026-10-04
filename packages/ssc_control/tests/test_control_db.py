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

import hashlib
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from ssc_testkit import Dsns, make_org

import ssc_control.db
from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.ids import PREFIXES, new_id
from ssc_control.db import (
    APP_ROLE,
    MIGRATE_ROLE,
    SqlState,
    bind_org_sync,
    bound_org,
    catalog,
    check_org_id,
    downgrade,
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
from ssc_control.db.migrate import alembic_config

DB_DIR = Path(ssc_control.db.__file__).parent


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
    head = ScriptDirectory.from_config(alembic_config(dsns.migrate)).get_current_head()
    assert run(dsns.migrate, None, "select version_num from ssc.alembic_version") == [(head,)]


def test_migration_chain_has_one_head_and_short_revision_ids() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("postgresql://unused/ssc"))
    assert len(scripts.get_heads()) == 1, scripts.get_heads()
    revisions = [s.revision for s in scripts.walk_revisions()]
    assert "0001_control_schema" in revisions
    # ssc.alembic_version.version_num is varchar(32).
    assert [r for r in revisions if len(r) > 32] == []


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


BUNDLE_INSERT = (
    "insert into ssc.bundle (id, org_id, app_id, digest, size_bytes, actor_kind, actor_id) "
    "values (%s, %s, %s, %s, 10, 'user', %s)"
)


def test_bundles_stay_in_their_org_and_app(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    a, b = orgs
    bid = new_id("bdl")
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        conn.execute(BUNDLE_INSERT, (bid, a.org, a.app, digest(bid), a.admin))
        with pytest.raises(psycopg.Error) as e, conn.transaction():
            conn.execute(BUNDLE_INSERT, (new_id("bdl"), a.org, a.app, digest(bid), a.admin))
        assert sqlstate(e) == UNIQUE_VIOLATION  # one row per (org, app, digest)
    assert run(dsns.app, b.org, "select count(*) from ssc.bundle where id = %s", (bid,)) == [(0,)]
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, b.org)
        cur = conn.execute("update ssc.bundle set size_bytes = 11 where id = %s", (bid,))
        assert cur.rowcount == 0
    sneaky = (new_id("bdl"), a.org, a.app, digest("sneaky"), b.admin)
    assert refused(dsns.app, b.org, BUNDLE_INSERT, sneaky) == INSUFFICIENT_PRIVILEGE
    theirs = (new_id("bdl"), b.org, a.app, digest("theirs"), b.admin)  # alpha's app from beta
    assert refused(dsns.app, b.org, BUNDLE_INSERT, theirs) == FOREIGN_KEY_VIOLATION
    delete = "delete from ssc.bundle where id = %s"
    assert refused(dsns.app, a.org, delete, (bid,)) == INSUFFICIENT_PRIVILEGE
    assert run(dsns.app, a.org, "select size_bytes from ssc.bundle where id = %s", (bid,)) == [
        (10,)
    ]


def test_a_stored_bundle_has_its_manifest(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    a, _ = orgs
    bid = new_id("bdl")
    store = (
        "update ssc.bundle set state = 'stored', manifest = %s::jsonb, manifest_digest = %s, "
        "file_count = 1, stored_at = now() where id = %s"
    )
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        conn.execute(BUNDLE_INSERT, (bid, a.org, a.app, digest(bid), a.admin))
        for sql, params in (
            ("update ssc.bundle set state = 'stored', stored_at = now() where id = %s", (bid,)),
            (store, ('["not an object"]', digest("m"), bid)),
            ("update ssc.bundle set stored_at = now() where id = %s", (bid,)),
        ):
            with pytest.raises(psycopg.Error) as e, conn.transaction():
                conn.execute(sql, params)
            assert sqlstate(e) == CHECK_VIOLATION, sql
        conn.execute(store, ('{"schema": "ssc/v1"}', digest("m"), bid))
        assert conn.execute("select state from ssc.bundle where id = %s", (bid,)).fetchone() == (
            "stored",
        )


BUILD_INSERT = (
    "insert into ssc.build (id, org_id, app_id, environment_id, bundle_id, actor_kind, actor_id) "
    "values (%s, %s, %s, %s, %s, 'user', %s)"
)


def add_bundle(conn: psycopg.Connection[Any], a: SeededOrg) -> str:
    bid = new_id("bdl")
    conn.execute(BUNDLE_INSERT, (bid, a.org, a.app, digest(bid), a.admin))
    return bid


def test_builds_stay_in_their_org_and_app(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    a, b = orgs
    build = new_id("bld")
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        bundle = add_bundle(conn, a)
        conn.execute(BUILD_INSERT, (build, a.org, a.app, a.env, bundle, a.admin))
    assert run(dsns.app, b.org, "select count(*) from ssc.build where id = %s", (build,)) == [(0,)]
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, b.org)
        cur = conn.execute("update ssc.build set driver_ref = 'x' where id = %s", (build,))
        assert cur.rowcount == 0
    sneaky = (new_id("bld"), a.org, a.app, a.env, bundle, b.admin)
    assert refused(dsns.app, b.org, BUILD_INSERT, sneaky) == INSUFFICIENT_PRIVILEGE
    theirs = (new_id("bld"), b.org, a.app, a.env, bundle, b.admin)  # alpha's rows from beta
    assert refused(dsns.app, b.org, BUILD_INSERT, theirs) == FOREIGN_KEY_VIOLATION
    delete = "delete from ssc.build where id = %s"
    assert refused(dsns.app, a.org, delete, (build,)) == INSUFFICIENT_PRIVILEGE
    assert run(dsns.app, a.org, "select state from ssc.build where id = %s", (build,)) == [
        ("queued",)
    ]


def test_a_build_row_is_consistent_and_one_is_in_flight(
    dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]
) -> None:
    a, _ = orgs
    build = new_id("bld")
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        bundle = add_bundle(conn, a)
        conn.execute(BUILD_INSERT, (build, a.org, a.app, a.env, bundle, a.admin))
        with pytest.raises(psycopg.Error) as e, conn.transaction():
            conn.execute(BUILD_INSERT, (new_id("bld"), a.org, a.app, a.env, bundle, a.admin))
        assert sqlstate(e) == UNIQUE_VIOLATION
        assert "build_one_in_flight" in str(e.value)
        for sql, params in (
            ("update ssc.build set state = 'running' where id = %s", (build,)),
            ("update ssc.build set started_at = now() where id = %s", (build,)),
            (
                "update ssc.build set state = 'succeeded', started_at = now(), "
                "finished_at = now() where id = %s",
                (build,),
            ),
            (
                "update ssc.build set state = 'failed', started_at = now(), "
                "finished_at = now() where id = %s",
                (build,),
            ),
            (
                "update ssc.build set state = 'failed', failure_code = 'BUILD_TIMED_OUT', "
                "started_at = now() where id = %s",
                (build,),
            ),
            (
                "update ssc.build set state = 'running', started_at = now(), "
                "failure_code = 'BUILD_TIMED_OUT' where id = %s",
                (build,),
            ),
            (
                "update ssc.build set state = 'running', started_at = now(), "
                "release_id = %s where id = %s",
                (a.release, build),
            ),
            (
                "update ssc.build set state = 'running', started_at = now(), "
                "failure_code = 'lower case' where id = %s",
                (build,),
            ),
        ):
            with pytest.raises(psycopg.Error) as e, conn.transaction():
                conn.execute(sql, params)
            assert sqlstate(e) == CHECK_VIOLATION, sql
        conn.execute(
            "update ssc.build set state = 'failed', failure_code = 'BUILD_TIMED_OUT', "
            "started_at = now(), finished_at = now() where id = %s",
            (build,),
        )
        # Once it finished, the bundle may build again for that environment.
        again = new_id("bld")
        conn.execute(BUILD_INSERT, (again, a.org, a.app, a.env, bundle, a.admin))
        conn.execute(
            "update ssc.build set state = 'succeeded', release_id = %s, started_at = now(), "
            "finished_at = now() where id = %s",
            (a.release, again),
        )
        # A release is made by at most one build.
        other = new_id("bld")
        conn.execute(BUILD_INSERT, (other, a.org, a.app, a.env, bundle, a.admin))
        with pytest.raises(psycopg.Error) as e, conn.transaction():
            conn.execute(
                "update ssc.build set state = 'succeeded', release_id = %s, started_at = now(), "
                "finished_at = now() where id = %s",
                (a.release, other),
            )
        assert sqlstate(e) == UNIQUE_VIOLATION


KILL_INSERT = (
    "insert into ssc.kill_switch_run (id, org_id, app_id, mode, actor_kind, actor_id) "
    "values (%s, %s, %s, 'disable', 'user', %s)"
)


def test_kill_switch_runs_stay_in_their_org(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    a, b = orgs
    kil = new_id("kil")
    done = (
        "insert into ssc.kill_switch_run (id, org_id, app_id, mode, state, actor_kind, actor_id, "
        "finished_at) values (%s, %s, %s, 'quarantine', 'completed', 'user', %s, now())"
    )
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        conn.execute(done, (kil, a.org, a.app, a.admin))
    count = "select count(*) from ssc.kill_switch_run where id = %s"
    assert run(dsns.app, b.org, count, (kil,)) == [(0,)]
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, b.org)
        cur = conn.execute("update ssc.kill_switch_run set steps = '[]' where id = %s", (kil,))
        assert cur.rowcount == 0
    sneaky = (new_id("kil"), a.org, a.app, b.admin)
    assert refused(dsns.app, b.org, KILL_INSERT, sneaky) == INSUFFICIENT_PRIVILEGE
    theirs = (new_id("kil"), b.org, a.app, b.admin)  # alpha's app from beta
    assert refused(dsns.app, b.org, KILL_INSERT, theirs) == FOREIGN_KEY_VIOLATION
    delete = "delete from ssc.kill_switch_run where id = %s"
    assert refused(dsns.app, a.org, delete, (kil,)) == INSUFFICIENT_PRIVILEGE
    assert run(dsns.app, a.org, count, (kil,)) == [(1,)]


def test_one_kill_switch_run_per_app_and_a_run_is_consistent(
    dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]
) -> None:
    a, _ = orgs
    kil = new_id("kil")
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        conn.execute(KILL_INSERT, (kil, a.org, a.app, a.admin))
        with pytest.raises(psycopg.Error) as e, conn.transaction():
            conn.execute(KILL_INSERT, (new_id("kil"), a.org, a.app, a.admin))
        assert sqlstate(e) == UNIQUE_VIOLATION
        assert "kill_switch_one_running" in str(e.value)
        for sql in (
            "update ssc.kill_switch_run set state = 'completed' where id = %s",
            "update ssc.kill_switch_run set finished_at = now() where id = %s",
            "update ssc.kill_switch_run set resumed_at = now() where id = %s",
            "update ssc.kill_switch_run set mode = 'stop' where id = %s",
            "update ssc.kill_switch_run set steps = '{}' where id = %s",
            "update ssc.kill_switch_run set paused_schedule_ids = '\"x\"' where id = %s",
        ):
            with pytest.raises(psycopg.Error) as e, conn.transaction():
                conn.execute(sql, (kil,))
            assert sqlstate(e) == CHECK_VIOLATION, sql
        conn.execute(
            "update ssc.kill_switch_run set state = 'failed', finished_at = now(), "
            "resumed_at = now() where id = %s",
            (kil,),
        )
        # Once it finished, the app may be stopped again.
        conn.execute(
            "insert into ssc.kill_switch_run (id, org_id, app_id, mode, state, actor_kind, "
            "actor_id, finished_at) values (%s, %s, %s, 'disable', 'completed', 'user', %s, now())",
            (new_id("kil"), a.org, a.app, a.admin),
        )


def test_only_a_failed_deployment_has_a_failure_code(
    dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]
) -> None:
    a, _ = orgs
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        dep = add_deployment(conn, a.org, a.app, a.env, a.release, "pending")
        for sql in (
            "update ssc.deployment set failure_code = 'HEALTH_CHECK_FAILED' where id = %s",
            "update ssc.deployment set state = 'healthy', finished_at = now(), "
            "failure_code = 'HEALTH_CHECK_FAILED' where id = %s",
            "update ssc.deployment set state = 'failed', finished_at = now(), "
            "failure_code = 'not a code' where id = %s",
        ):
            with pytest.raises(psycopg.Error) as e, conn.transaction():
                conn.execute(sql, (dep,))
            assert sqlstate(e) == CHECK_VIOLATION, sql
        conn.execute(
            "update ssc.deployment set state = 'failed', finished_at = now(), "
            "failure_code = 'HEALTH_CHECK_FAILED' where id = %s",
            (dep,),
        )


def test_last_active_admin_cannot_be_removed(dsns: Dsns) -> None:
    org = make_org(dsns.app, "Solo")
    for sql in (
        "update ssc.user_account set role = 'member' where id = %s",
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


def test_deactivating_the_last_admin_is_allowed(dsns: Dsns) -> None:
    # Directory sync is never blocked (SSC-019, revision 0014); an operator restores an admin.
    org = make_org(dsns.app, "Leaver")
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, org.org_id)
        conn.execute(
            "update ssc.user_account set status = 'deactivated', deactivated_at = now() "
            "where id = %s",
            (org.admin_user_id,),
        )
        conn.execute(
            "update ssc.user_account set status = 'active', deactivated_at = null where id = %s",
            (org.admin_user_id,),
        )
        conn.execute(
            "update ssc.user_account set role = 'member', status = 'deactivated', "
            "deactivated_at = now() where id = %s",
            (org.admin_user_id,),
        )


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
        head = conn.execute("select seq, hash from ssc.audit_head where org_id = %s", (a.org,))
        seq, prev = cast(tuple[int, bytes], head.fetchone())
        assert seq >= 1  # create_org's org.created
        conn.execute(insert, (a.org, seq + 1, AuditAction.USER_CREATED, a.org, b"{}", prev, h1))
        conn.execute(
            "update ssc.audit_head set seq = %s, hash = %s where org_id = %s", (seq + 1, h1, a.org)
        )
    # A fork (two events claiming the same predecessor) cannot be stored.
    fork = refused(
        dsns.app,
        a.org,
        insert,
        (a.org, seq + 2, AuditAction.LOGIN_SUCCEEDED, a.org, b"{}", prev, h2),
    )
    assert fork == UNIQUE_VIOLATION
    # Actions come from the closed list.
    made_up = refused(dsns.app, a.org, insert, (a.org, seq + 2, "made.up", a.org, b"{}", h1, h2))
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
        "insert into ssc.approval_request (id, org_id, environment_id, kind, subject_key, "
        "requested_by_user_id, state, decided_by_user_id, decided_at, decided_via_agent, "
        "decision_reason) values (%s, %s, %s, %s, 'finance', %s, %s, %s, %s, %s, %s)"
    )
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, org.org_id)
        approver = add_user(conn, org.org_id, role="admin")
        me = org.admin_user_id
        env = add_env(conn, org.org_id, add_app(conn, org.org_id, me, "approvals"))
        at = "2026-09-28T00:00:00Z"
        kind = "connect_data_source"
        bad = (
            (kind, "approved", me, at, False, "approval_request_not_self"),
            (kind, "denied", me, at, False, "approval_request_not_self"),
            (kind, "approved", approver, at, True, "approval_request_decided_via_agent_check"),
            (kind, "pending", approver, at, False, None),  # pending with a decider
            (kind, "approved", approver, None, False, None),  # approved without a time
            ("connection", "approved", approver, at, False, "approval_request_kind_check"),
        )
        for kind_, state, decider, decided_at, via_agent, constraint in bad:
            reason = None if state == "pending" else "Asked by email."
            row = (new_id("apr"), org.org_id, env, kind_, me, state, decider, decided_at)
            with pytest.raises(psycopg.Error) as e, conn.transaction():
                conn.execute(insert, (*row, via_agent, reason))
            assert sqlstate(e) == CHECK_VIOLATION, (state, decider, decided_at, via_agent)
            if constraint is not None:
                assert e.value.diag.constraint_name == constraint
        ok = (new_id("apr"), org.org_id, env, kind, me, "approved", approver, at, False, "Yes.")
        conn.execute(insert, ok)
        # The requester may withdraw their own request.
        mine = (new_id("apr"), org.org_id, env, kind, me, "cancelled", me, at, False, "Withdrawn.")
        conn.execute(insert, mine)


def test_deleted_schedule_is_terminal(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    a, _ = orgs
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        sid = new_id("sch")
        conn.execute(
            "insert into ssc.schedule (id, org_id, environment_id, name, cron, path, state, "
            "pause_reason, declared_by_user_id) "
            "values (%s, %s, %s, 'nightly', '0 2 * * *', '/tick', 'paused', 'manual', %s)",
            (sid, a.org, a.env, a.admin),
        )
        conn.execute(
            "update ssc.schedule set state = 'deleted', pause_reason = null where id = %s", (sid,)
        )
        for sql in (
            "update ssc.schedule set state = 'active' where id = %s",
            "update ssc.schedule set cron = '* * * * *' where id = %s",
        ):
            with pytest.raises(psycopg.Error) as e, conn.transaction():
                conn.execute(sql, (sid,))
            assert sqlstate(e) == SqlState.SCHEDULE_DELETED


# ── lazy cell resources (SSC-087, revision 0020) ─────────────────────────────

CELL_RESOURCE = (
    "insert into ssc.cell_resource (org_id, resource, state, cause, actor_kind, actor_id, "
    "started_at, ready_at) values (%s, %s, 'ready', 'admin', 'user', %s, now(), now())"
)


def test_a_ready_cell_resource_is_never_turned_off(
    dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]
) -> None:
    a, b = orgs
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        conn.execute(CELL_RESOURCE, (a.org, "egress", a.admin))
    where = " where org_id = %s and resource = 'egress'"
    back = "update ssc.cell_resource set state = 'requested', ready_at = null" + where
    failed = (
        "update ssc.cell_resource set state = 'failed', ready_at = null, failed_at = now(), "
        "failure_code = 'CELL_DEPLOYER_FAILED'" + where
    )
    delete = "delete from ssc.cell_resource" + where
    assert refused(dsns.app, a.org, back, (a.org,)) == SqlState.CELL_RESOURCE_READY
    assert refused(dsns.app, a.org, failed, (a.org,)) == SqlState.CELL_RESOURCE_READY
    assert refused(dsns.app, a.org, delete, (a.org,)) == INSUFFICIENT_PRIVILEGE
    assert refused(dsns.app, a.org, "truncate ssc.cell_resource cascade") == (
        INSUFFICIENT_PRIVILEGE
    )
    assert refused(dsns.migrate, a.org, back, (a.org,)) == SqlState.CELL_RESOURCE_READY
    assert refused(dsns.migrate, a.org, delete, (a.org,)) == SqlState.CELL_RESOURCE_READY
    assert refused(dsns.migrate, a.org, "truncate ssc.cell_resource cascade") == (
        SqlState.TRUNCATE_REFUSED
    )
    assert run(dsns.app, b.org, "select resource from ssc.cell_resource") == []
    assert refused(dsns.app, b.org, CELL_RESOURCE, (a.org, "database", a.admin)) == (
        INSUFFICIENT_PRIVILEGE
    )


def test_a_cell_resource_row_is_consistent(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    a, _ = orgs
    columns = "insert into ssc.cell_resource (org_id, resource, state, cause, actor_kind, actor_id"
    for sql in (
        columns + ", started_at) values (%s, 'database', 'ready', 'admin', 'user', %s, now())",
        columns + ", started_at) values (%s, 'database', 'failed', 'admin', 'user', %s, now())",
        columns + ") values (%s, 'database', 'creating', 'admin', 'user', %s)",
        columns + ") values (%s, 'disk', 'requested', 'admin', 'user', %s)",
        columns + ") values (%s, 'database', 'requested', 'whim', 'user', %s)",
    ):
        assert refused(dsns.app, a.org, sql, (a.org, a.admin)) == CHECK_VIOLATION


# ── timers (SSC-041, revision 0013) ──────────────────────────────────────────

ARMED_SCHEDULE = (
    "insert into ssc.schedule (id, org_id, environment_id, name, cron, path, state, next_run_at, "
    "declared_by_user_id) values (%s, %s, %s, %s, '*/5 * * * *', '/tick', 'active', now(), %s)"
)
RUNNING_TIMER = (
    "insert into ssc.timer_run (id, org_id, schedule_id, trigger, scheduled_for, state, "
    "started_at) values (%s, %s, %s, 'schedule', now(), 'running', now())"
)


def test_timer_runs_stay_in_their_org(dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]) -> None:
    a, b = orgs
    sch, tmr = new_id("sch"), new_id("tmr")
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        conn.execute(ARMED_SCHEDULE, (sch, a.org, a.env, "isolated", a.admin))
        conn.execute(RUNNING_TIMER, (tmr, a.org, sch))
    count = "select count(*) from ssc.timer_run where id = %s"
    assert run(dsns.app, b.org, count, (tmr,)) == [(0,)]
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, b.org)
        cur = conn.execute(
            "update ssc.timer_run set state = 'timed_out', error = 'abandoned', "
            "finished_at = now() where id = %s",
            (tmr,),
        )
        assert cur.rowcount == 0
    sneaky = (new_id("tmr"), a.org, sch)
    assert refused(dsns.app, b.org, RUNNING_TIMER, sneaky) == INSUFFICIENT_PRIVILEGE
    theirs = (new_id("tmr"), b.org, sch)  # alpha's schedule from beta
    assert refused(dsns.app, b.org, RUNNING_TIMER, theirs) == FOREIGN_KEY_VIOLATION
    declarer = (new_id("sch"), b.org, b.env, "borrowed", a.admin)  # alpha's user declares in beta
    assert refused(dsns.app, b.org, ARMED_SCHEDULE, declarer) == FOREIGN_KEY_VIOLATION
    delete = "delete from ssc.timer_run where id = %s"
    assert refused(dsns.app, a.org, delete, (tmr,)) == INSUFFICIENT_PRIVILEGE
    assert run(dsns.app, a.org, count, (tmr,)) == [(1,)]


def test_a_schedule_says_why_it_is_paused_and_its_live_name_is_unique(
    dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]
) -> None:
    a, _ = orgs
    insert = (
        "insert into ssc.schedule (id, org_id, environment_id, name, cron, path, state, "
        "pause_reason, next_run_at, declared_by_user_id) "
        "values (%s, %s, %s, %s, '0 3 * * *', %s, %s, %s, %s, %s)"
    )
    armed = datetime(2026, 9, 29, 3, tzinfo=UTC)
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        for path, state, reason, at, constraint in (
            ("/t", "active", None, None, "schedule_active_is_armed"),
            ("/t", "paused", None, None, "schedule_paused_has_reason"),
            ("/t", "active", "manual", armed, "schedule_paused_has_reason"),
            ("/t", "paused", "manual", armed, "schedule_active_is_armed"),
            ("/t", "paused", "tired", None, None),
            ("t", "paused", "manual", None, None),
            ("/a b", "paused", "manual", None, None),
            ("/" + "a" * 512, "paused", "manual", None, None),
        ):
            row = (new_id("sch"), a.org, a.env, "bad", path, state, reason, at, a.admin)
            with pytest.raises(psycopg.Error) as e, conn.transaction():
                conn.execute(insert, row)
            assert sqlstate(e) == CHECK_VIOLATION, (path, state, reason)
            if constraint is not None:
                assert e.value.diag.constraint_name == constraint
        first = new_id("sch")
        conn.execute(
            insert, (first, a.org, a.env, "nightly", "/t?x=1", "active", None, armed, a.admin)
        )
        again = (new_id("sch"), a.org, a.env, "nightly", "/t", "paused", "manual", None, a.admin)
        with pytest.raises(psycopg.Error) as e, conn.transaction():
            conn.execute(insert, again)
        assert sqlstate(e) == UNIQUE_VIOLATION
        assert e.value.diag.constraint_name == "schedule_live_name"
        # Deleted, the name may be declared again.
        conn.execute(
            "update ssc.schedule set state = 'deleted', next_run_at = null where id = %s", (first,)
        )
        conn.execute(insert, again)


def test_a_timer_run_is_consistent_and_never_overlaps(
    dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]
) -> None:
    a, _ = orgs
    t = datetime(2026, 9, 29, 10, 5, tzinfo=UTC)
    later = t.replace(minute=10)
    insert = (
        "insert into ssc.timer_run (id, org_id, schedule_id, trigger, scheduled_for, "
        "requested_by_user_id, state, error, http_status, started_at, finished_at) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    sch = new_id("sch")

    def row(  # noqa: PLR0913
        trigger: str,
        by: str | None,
        state: str,
        error: str | None = None,
        status: int | None = None,
        started: datetime | None = None,
        finished: datetime | None = None,
        at: datetime = t,
    ) -> tuple[object, ...]:
        return (new_id("tmr"), a.org, sch, trigger, at, by, state, error, status, started, finished)

    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, a.org)
        conn.execute(ARMED_SCHEDULE, (sch, a.org, a.env, "consistent", a.admin))
        for bad in (
            row("schedule", a.admin, "running", started=t),  # a scheduled run has no requester
            row("manual", None, "queued"),  # a manual run always has one
            row("schedule", None, "queued"),  # only a manual run waits
            row("schedule", None, "running"),  # a running run has started
            row("schedule", None, "skipped", "overlap", started=t, finished=t),  # never started
            row("schedule", None, "succeeded", status=200, started=t),  # a finished run has ended
            row("schedule", None, "failed", status=500, started=t, finished=t),  # says why
            row("schedule", None, "succeeded", "http_error", 200, t, t),  # no error on success
            row("schedule", None, "succeeded", None, 503, t, t),  # a success is 2xx
            row("schedule", None, "failed", "boom", None, t, t),  # a known error
            row("schedule", None, "failed", "http_error", 600, t, t),  # an HTTP status
        ):
            with pytest.raises(psycopg.Error) as e, conn.transaction():
                conn.execute(insert, bad)
            assert sqlstate(e) == CHECK_VIOLATION, bad
        conn.execute(insert, row("schedule", None, "running", started=t))
        for dup, index in (
            (row("schedule", None, "skipped", "overlap", finished=t), "timer_run_once_per_instant"),
            (row("manual", a.admin, "running", started=t, at=later), "timer_run_one_running"),
        ):
            with pytest.raises(psycopg.Error) as e, conn.transaction():
                conn.execute(insert, dup)
            assert sqlstate(e) == UNIQUE_VIOLATION
            assert e.value.diag.constraint_name == index
        conn.execute(insert, row("manual", a.admin, "queued", at=later))
        with pytest.raises(psycopg.Error) as e, conn.transaction():
            conn.execute(insert, row("manual", a.admin, "queued", at=later))
        assert sqlstate(e) == UNIQUE_VIOLATION
        assert e.value.diag.constraint_name == "timer_run_one_queued"


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
    assert tables == catalog.TABLES | set(catalog.UNSCOPED_TABLES) | {catalog.MIGRATION_LEDGER}
    assert catalog.TABLES.isdisjoint(catalog.UNSCOPED_TABLES)
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
    for t in catalog.TABLES - {"org"} - catalog.UNKEYED_TABLES:
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
    expected = catalog.TABLES - catalog.UNKEYED_TABLES
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
    assert {r[0] for r in rows} == catalog.TABLES | set(catalog.UNSCOPED_TABLES)
    for name, enabled, forced, policies in rows:
        expected = (False, False, 0) if name in catalog.UNSCOPED_TABLES else (True, True, 1)
        assert (enabled, forced, policies) == expected, name
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
    tables = len(catalog.TABLES) + len(catalog.UNSCOPED_TABLES)
    queue_schema = "select count(*) from pg_namespace where nspname = %s"
    upgrade(dsn)
    assert run(dsn, None, count) == [(tables,)]
    assert run(dsn, None, queue_schema, (catalog.QUEUE_SCHEMA,)) == [(1,)]
    downgrade(dsn)
    assert run(dsn, None, count) == [(0,)]
    assert run(dsn, None, queue_schema, (catalog.QUEUE_SCHEMA,)) == [(0,)]
    assert run(
        dsn,
        None,
        "select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = 'ssc'",
    ) == [(0,)]
    upgrade(dsn)
    assert run(dsn, None, count) == [(tables,)]


LANE_ACTIONS = frozenset(
    {
        "audit.exported",
        "audit.reanchored",
        "user.updated",
        "schedule.updated",
        "schedule.run_requested",
        "bundle.stored",
        "build.started",
        "build.failed",
        "release.created",
    }
)


def action_check(dsn: str) -> tuple[str, bool]:
    ((definition, validated),) = run(
        dsn,
        None,
        "select pg_get_constraintdef(oid), convalidated from pg_constraint "
        "where conrelid = 'ssc.audit_event'::regclass and conname = 'audit_event_action_check'",
    )
    return definition, validated


def test_lane_vocab_revision_widens_the_action_check_and_downgrade_restores_it(
    dsns: Dsns,
) -> None:
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database vocab owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database="vocab").render_as_string(hide_password=False)
    upgrade(dsn, "0002_idempotency")
    before = action_check(dsn)
    upgrade(dsn, "0003_lane_vocab")
    after = action_check(dsn)
    old = set(re.findall(r"'([a-z_]+\.[a-z_]+)'", before[0]))
    assert set(re.findall(r"'([a-z_]+\.[a-z_]+)'", after[0])) == old | LANE_ACTIONS
    assert old.isdisjoint(LANE_ACTIONS)
    assert after[1] is True
    downgrade(dsn, "0002_idempotency")
    assert action_check(dsn) == before
    upgrade(dsn, "0003_lane_vocab")
    assert action_check(dsn) == after


IDENTITY_ACTIONS = {"directory.connected", "directory.frozen", "identity.linked"}


def test_identity_revision_widens_the_action_check_and_downgrade_restores_it(
    dsns: Dsns,
) -> None:
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database identity owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database="identity").render_as_string(hide_password=False)
    upgrade(dsn, "0013_timers")
    before = action_check(dsn)
    upgrade(dsn, "0014_identity")
    after = action_check(dsn)
    old = set(re.findall(r"'([a-z_]+\.[a-z_]+)'", before[0]))
    assert set(re.findall(r"'([a-z_]+\.[a-z_]+)'", after[0])) == old | IDENTITY_ACTIONS
    assert {a.value for a in AuditAction} >= old | IDENTITY_ACTIONS
    downgrade(dsn, "0013_timers")
    # The audit chain is append-only, so the old vocabulary comes back unvalidated.
    assert action_check(dsn) == (f"{before[0]} NOT VALID", False)
    upgrade(dsn, "0014_identity")
    assert action_check(dsn) == after


CELL_ACTIONS = {"cell.resource_requested", "cell.resource_ready", "cell.resource_failed"}


def test_cell_resources_revision_widens_the_action_check_and_downgrade_restores_it(
    dsns: Dsns,
) -> None:
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database cellres owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database="cellres").render_as_string(hide_password=False)
    upgrade(dsn, "0014_identity")
    before = action_check(dsn)
    upgrade(dsn, "0020_cell_resources")
    after = action_check(dsn)
    old = set(re.findall(r"'([a-z_]+\.[a-z_]+)'", before[0]))
    assert set(re.findall(r"'([a-z_]+\.[a-z_]+)'", after[0])) == old | CELL_ACTIONS
    assert {a.value for a in AuditAction} >= old | CELL_ACTIONS
    downgrade(dsn, "0014_identity")
    assert action_check(dsn) == (f"{before[0]} NOT VALID", False)
    upgrade(dsn, "0020_cell_resources")
    assert action_check(dsn) == after


def test_agent_interface_revision_adds_its_columns_and_downgrade_removes_them(
    dsns: Dsns,
) -> None:
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database agentif owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database="agentif").render_as_string(hide_password=False)
    upgrade(dsn, "0026_migrations")
    before = action_check(dsn)
    upgrade(dsn, "0027_agent_interface")
    after = action_check(dsn)
    old = set(re.findall(r"'([a-z_]+\.[a-z_]+)'", before[0]))
    assert set(re.findall(r"'([a-z_]+\.[a-z_]+)'", after[0])) == old | {"org.updated"}
    assert {a.value for a in AuditAction} == old | {"org.updated"}
    added = "select table_name, column_name, column_default from information_schema.columns " + (
        "where table_schema = 'ssc' and column_name in ('agent_logs', 'agent_client_id') "
        "order by table_name"
    )
    assert run(dsn, None, added) == [
        ("auth_session", "agent_client_id", None),
        ("device_grant", "agent_client_id", None),
        ("org", "agent_logs", "true"),
    ]
    downgrade(dsn, "0026_migrations")
    assert action_check(dsn) == (f"{before[0]} NOT VALID", False)
    assert run(dsn, None, added) == []
    upgrade(dsn)
    assert action_check(dsn) == after


def test_only_a_cli_session_names_an_agent_and_the_name_is_checked(dsns: Dsns) -> None:
    created = make_org(dsns.app, "Agent sessions")
    org, user = created.org_id, created.admin_user_id
    insert = (
        "insert into ssc.auth_session (id, org_id, user_id, kind, connection_id, expires_at, "
        "agent_client_id) values (%s, %s, %s, %s, 'conn_x', now() + interval '1 hour', %s) "
        "returning id"
    )
    run(dsns.app, org, insert, (new_id("ses"), org, user, "cli", "claude-code"))
    browser = (new_id("ses"), org, user, "browser", "claude-code")
    assert refused(dsns.app, org, insert, browser) == "23514"
    spaced = (new_id("ses"), org, user, "cli", "Claude Code")
    assert refused(dsns.app, org, insert, spaced) == "23514"


# ── org_index: the single unscoped table (decision 009 amendment, revision 0006) ─────────────


def test_org_index_holds_org_ids_only(dsns: Dsns) -> None:
    assert catalog.UNSCOPED_TABLES == ("org_index",)
    columns = catalog_rows(
        dsns,
        "select column_name from information_schema.columns "
        "where table_schema = 'ssc' and table_name = 'org_index'",
    )
    assert {c for (c,) in columns} == {"org_id", "created_at"}
    defs = {
        d
        for (d,) in catalog_rows(
            dsns,
            "select pg_get_constraintdef(oid) from pg_constraint "
            "where conrelid = 'ssc.org_index'::regclass",
        )
    }
    assert "PRIMARY KEY (org_id)" in defs
    assert "FOREIGN KEY (org_id) REFERENCES ssc.org(id)" in defs


def test_org_index_row_is_written_in_the_creating_transaction(dsns: Dsns) -> None:
    created = make_org(dsns.app, "Indexed")
    rows = run(
        dsns.superuser,
        None,
        "select o.xmin::text, i.xmin::text, h.xmin::text from ssc.org o "
        "join ssc.org_index i on i.org_id = o.id join ssc.audit_head h on h.org_id = o.id "
        "where o.id = %s",
        (created.org_id,),
    )
    assert len(rows) == 1 and len(set(rows[0])) == 1, rows  # one transaction wrote all three


def test_failed_create_org_leaves_no_index_row(dsns: Dsns) -> None:
    before = run(dsns.superuser, None, "select count(*) from ssc.org_index")
    with pytest.raises(IntegrityError):
        make_org(dsns.app, name="")
    assert run(dsns.superuser, None, "select count(*) from ssc.org_index") == before


def test_app_role_reads_org_index_unbound_but_never_changes_it(
    dsns: Dsns, orgs: tuple[SeededOrg, SeededOrg]
) -> None:
    a, b = orgs
    ids = {r[0] for r in run(dsns.app, None, "select org_id from ssc.org_index")}
    assert {a.org, b.org} <= ids
    for sql in (
        "update ssc.org_index set created_at = now() where org_id = %s",
        "delete from ssc.org_index where org_id = %s",
    ):
        assert refused(dsns.app, None, sql, (a.org,)) == INSUFFICIENT_PRIVILEGE, sql
        assert refused(dsns.app, a.org, sql, (a.org,)) == INSUFFICIENT_PRIVILEGE, sql
    assert refused(dsns.app, None, "truncate ssc.org_index") == INSUFFICIENT_PRIVILEGE
    insert = "insert into ssc.org_index (org_id) values (%s)"
    assert refused(dsns.app, None, insert, (new_id("org"),)) == FOREIGN_KEY_VIOLATION
    assert refused(dsns.app, None, insert, (a.org,)) == UNIQUE_VIOLATION
    # Reading the index grants nothing else: the org rows themselves stay behind RLS.
    assert refused(dsns.app, None, "select count(*) from ssc.org") == SqlState.NO_ORG_BOUND


def test_org_index_backfills_orgs_created_before_it(dsns: Dsns) -> None:
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database backfill owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database="backfill").render_as_string(hide_password=False)
    app_dsn = make_url(dsns.app).set(database="backfill").render_as_string(hide_password=False)
    upgrade(dsn, "0005_approvals")
    old = new_id("org")
    with psycopg.connect(app_dsn) as conn:
        bind_org_sync(conn, old)
        conn.execute("insert into ssc.org (id, name) values (%s, 'Before 0006')", (old,))
    upgrade(dsn)
    assert run(app_dsn, None, "select org_id from ssc.org_index") == [(old,)]
    forced = "select relforcerowsecurity from pg_class where oid = 'ssc.org'::regclass"
    assert run(dsn, None, forced) == [(True,)]  # FORCE is back on after the back-fill


def test_queue_schema_privileges_match_the_declared_matrix(dsns: Dsns) -> None:
    acl = catalog_rows(
        dsns,
        "select c.relname, a.grantee::regrole::text, a.privilege_type "
        "from pg_class c join pg_namespace n on n.oid = c.relnamespace, aclexplode(c.relacl) a "
        "where n.nspname = %s and c.relkind in ('r', 'S')",
        (catalog.QUEUE_SCHEMA,),
    )
    assert {g for _, g, _ in acl} == {MIGRATE_ROLE, APP_ROLE}  # nothing to PUBLIC
    app_privs: dict[str, set[str]] = {}
    for name, grantee, priv in acl:
        if grantee == APP_ROLE:
            app_privs.setdefault(name, set()).add(priv)
    assert app_privs == {t: set(p) for t, p in catalog.QUEUE_APP_PRIVILEGES.items()}
    owners = catalog_rows(
        dsns, "select tableowner from pg_tables where schemaname = %s", (catalog.QUEUE_SCHEMA,)
    )
    assert len(owners) == 4 and {o for (o,) in owners} == {MIGRATE_ROLE}
    ((create, usage),) = catalog_rows(
        dsns,
        "select has_schema_privilege(%s, %s, 'CREATE'), has_schema_privilege(%s, %s, 'USAGE')",
        (APP_ROLE, catalog.QUEUE_SCHEMA, APP_ROLE, catalog.QUEUE_SCHEMA),
    )
    assert (create, usage) == (False, True)
