"""The capability diff a build reports against its environment (GA-12 F20), on test_deploy's
bench."""

from __future__ import annotations

from typing import Any

import psycopg
import test_deploy
from test_deploy import Bench, get, manifest_of, seed_bundle, start_build

from ssc_contracts.capabilities import TIER_FULL_CONSEQUENCE
from ssc_contracts.ids import new_id
from ssc_control.api.routes.v1.deployments import PLACES_TOTAL
from ssc_control.db import bind_org_sync

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b

STATEFUL = {"state": {"postgres": True}}


def changes(b: Bench, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    bundle, _ = seed_bundle(b, manifest_of(**manifest))
    r = start_build(b, b.w.preview, bundle)
    assert r.status_code == 202, r.text
    started = r.json()["capability_diff"]["changes"]
    got = get(b, f"/v1/builds/{r.json()['build_id']}")
    assert got.status_code == 200, got.text
    assert got.json()["capability_diff"]["changes"] == started
    return started


def add_database(conn: psycopg.Connection[Any], org: str, env: str) -> None:
    conn.execute(
        "insert into ssc.app_database (org_id, environment_id, host, port, connection_limit) "
        "values (%s, %s, '10.0.0.5', 5432, 2)",
        (org, env),
    )


def fill_instance(b: Bench, *, leave: int = 0) -> None:
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        envs: list[str] = []
        while len(envs) < PLACES_TOTAL - leave:
            app = new_id("app")
            conn.execute(
                "insert into ssc.app (id, org_id, slug, owner_user_id) values (%s, %s, %s, %s)",
                (app, b.w.org, f"filler-{len(envs)}", b.w.admin),
            )
            for name in ("prod", "preview"):
                env = new_id("env")
                conn.execute(
                    "insert into ssc.environment (id, org_id, app_id, name) "
                    "values (%s, %s, %s, %s)",
                    (env, b.w.org, app, name),
                )
                envs.append(env)
        for env in envs[: PLACES_TOTAL - leave]:
            add_database(conn, b.w.org, env)


def kinds(found: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(c["kind"], c["subject"]) for c in found]


async def test_postgres_is_granted_while_the_instance_has_a_free_place(b: Bench) -> None:
    fill_instance(b, leave=1)
    assert kinds(changes(b, STATEFUL)) == []


async def test_an_environment_with_a_database_has_no_postgres_missing(b: Bench) -> None:
    fill_instance(b, leave=1)
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        add_database(conn, b.w.org, b.w.preview)
    assert kinds(changes(b, STATEFUL)) == []


async def test_a_full_instance_reports_postgres_missing_with_the_tier_full_text(
    b: Bench,
) -> None:
    fill_instance(b)
    (change,) = changes(b, STATEFUL)
    assert (change["kind"], change["severity"]) == ("postgres_missing", "high")
    assert change["consequence"] == TIER_FULL_CONSEQUENCE


async def test_an_unlinked_declared_connection_is_connection_missing(b: Bench) -> None:
    linked, elsewhere = new_id("con"), new_id("con")
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        for con, name in ((linked, "finance"), (elsewhere, "hr")):
            conn.execute(
                "insert into ssc.connection (id, org_id, name, kind, classification, host, port, "
                "database_name, address) values (%s, %s, %s, 'postgres', 'internal', "
                "'db.corp.internal', 5432, 'warehouse', jsonb_build_object('host', "
                "'db.corp.internal', 'port', 5432, 'database', 'warehouse'))",
                (con, b.w.org, name),
            )
        for con, env in ((linked, b.w.preview), (elsewhere, b.w.prod)):
            conn.execute(
                "insert into ssc.connection_grant (id, org_id, connection_id, environment_id, "
                "created_by_user_id) values (%s, %s, %s, %s, %s)",
                (new_id("cgr"), b.w.org, con, env, b.w.admin),
            )
    found = changes(b, {"connections": {"names": ["finance", "hr"]}})
    assert kinds(found) == [("connection_missing", "hr")]
