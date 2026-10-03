"""SSC-043: a rollback warns when the database shape changed, and a production deployment records
where its database was first. Uses test_deploy's bench and test_app_databases' cell.

Ticket "done when" checks that run here (the runbook is rehearsed live in SSC-086 T5):
  * the warning appears in a test app after a
    forward migration                              -> test_a_rollback_past_a_forward_migration_...
Plus: no warning without a database or with a release made before migrations were recorded, the
recovery point of a production deployment (from the fake, through the cell agent on postgres:18
set up the way Cloud SQL is, and when the agent cannot say), the agent's answer checked, and
migration 0026.
"""

from __future__ import annotations

import importlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx2
import psycopg
import pytest
import test_app_databases
import test_deploy
from sqlalchemy.engine import make_url
from ssc_testkit import Dsns, assert_problem, new_key
from test_app_databases import STATEFUL, Cell, database_ready, run_through_the_cell
from test_deploy import (
    AGENT,
    Bench,
    agent_token,
    audit_of,
    build_release,
    get,
    manifest_of,
    operation,
    post,
    rows_of,
    run,
    seed_prod_build,
    start_build,
    start_deploy,
    stored_fixture,
)

from ssc_contracts.app_database import LSN
from ssc_contracts.errors import CATALOGUE, ErrorCode
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.db import MIGRATE_ROLE, bind_org_sync, downgrade, upgrade
from ssc_control.deploy.builds import run_build
from ssc_control.runtime.app_databases import (
    AppDatabaseError,
    CellAppDatabases,
    FakeAppDatabases,
    RecoveryPoint,
)
from ssc_control.runtime.driver import service_name
from ssc_control.worker import Ports

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b
instance = test_app_databases.instance
cell = test_app_databases.cell

INIT = "20261001090000_init"
ADD_TOTAL = "20261002090000_add_order_total"


def app_source(root: Path, migrations: Sequence[str], *, postgres: bool = True) -> Path:
    """A small Node app with Prisma migrations, as its author's repository holds it."""
    source = root / "source"
    folder = source / "prisma" / "migrations"
    folder.mkdir(parents=True)
    state = "\n[state]\npostgres = true\n" if postgres else ""
    (source / "ssc.toml").write_text(
        f'schema = "ssc/v1"\n\n[runtime]\nstart = "node server.js"\n{state}'
    )
    (source / "package.json").write_text(
        json.dumps({"name": "orders", "version": "1.0.0", "scripts": {"start": "node server.js"}})
    )
    (source / "server.js").write_text(
        "require('http').createServer((_q, s) => s.end('ok')).listen(process.env.PORT || 8080);\n"
    )
    (folder / "migration_lock.toml").write_text('provider = "postgresql"\n')
    for name in migrations:
        (folder / name).mkdir()
        (folder / name / "migration.sql").write_text(f"-- {name}\nselect 1;\n")
    return source


async def release_from(
    b: Bench, root: Path, migrations: Sequence[str], env: str | None = None, **kw: bool
) -> str:
    """The release a build of ``app_source`` makes for ``env`` (preview by default)."""
    ports, bundle = await stored_fixture(b, app_source(root, migrations, **kw), root)
    env = env or b.w.preview
    if env == b.w.prod:
        build = seed_prod_build(b, bundle)
    else:
        r = start_build(b, env, bundle)
        assert r.status_code == 202, r.text
        build = r.json()["build_id"]
    assert await run_build(ports, org_id=b.w.org, build_id=build) == "succeeded"
    return str(get(b, f"/v1/builds/{build}").json()["release_id"])


def rollback(b: Bench, env: str, release: str, *, confirm: bool = False, **headers: str) -> Any:
    path = f"/v1/apps/{b.w.app}/environments/{env}/deployments"
    body: dict[str, Any] = {"release_id": release, "kind": "rollback"}
    if confirm:
        body["confirm"] = True
    return post(b, path, body, None, **headers)


def ahead(b: Bench, env: str, release: str) -> list[dict[str, Any]]:
    path = f"/v1/apps/{b.w.app}/environments/{env}/migrations-ahead?release_id={release}"
    r = get(b, path)
    assert r.status_code == 200, r.text
    assert (r.json()["environment_id"], r.json()["release_id"]) == (env, release)
    return list(r.json()["ledgers"])


def seen(b: Bench, env: str) -> Any:
    (row,) = rows_of(
        b.dsn, b.w.org, "select migrations from ssc.app_database where environment_id = %s", env
    )
    return row["migrations"]


async def deployed(b: Bench, env: str, release: str, ports: Ports) -> str:
    r = start_deploy(b, env, release)
    assert r.status_code == 202, r.text
    op = str(r.json()["operation_id"])
    assert await run(b, op, ports) == "healthy"
    return op


async def test_a_rollback_past_a_forward_migration_warns_and_needs_confirm(
    b: Bench, dsns: Dsns, tmp_path: Path
) -> None:
    database_ready(dsns, b.w.org)
    ports = replace(b.ports, app_databases=FakeAppDatabases())
    env = b.w.preview
    first = await release_from(b, tmp_path / "r1", [INIT])
    shown = get(b, f"/v1/apps/{b.w.app}/releases/{first}").json()
    assert shown["latest_migrations"] == {"prisma": INIT}
    await deployed(b, env, first, ports)
    assert seen(b, env) == {"prisma": [INIT]}

    second = await release_from(b, tmp_path / "r2", [INIT, ADD_TOTAL])
    assert rows_of(b.dsn, b.w.org, "select migrations from ssc.release where id = %s", second) == [
        {"migrations": {"prisma": [INIT, ADD_TOTAL]}}
    ]
    await deployed(b, env, second, ports)
    assert seen(b, env) == {"prisma": [INIT, ADD_TOTAL]}
    assert ahead(b, env, second) == []

    key = new_key()
    refused = rollback(b, env, first, **{IDEMPOTENCY_HEADER: key})
    assert_problem(refused, ErrorCode.SCHEMA_AHEAD)
    assert ADD_TOTAL not in refused.text
    assert refused.json()["detail"] == CATALOGUE[ErrorCode.SCHEMA_AHEAD].detail
    assert rows_of(b.dsn, b.w.org, "select id from ssc.deployment where kind = 'rollback'") == []
    assert ahead(b, env, first) == [{"ledger": "prisma", "names": [ADD_TOTAL]}]

    r = rollback(b, env, first, confirm=True, **{IDEMPOTENCY_HEADER: key})
    assert r.status_code == 202, r.text
    op = str(r.json()["operation_id"])
    assert await run(b, op, ports) == "healthy"
    started = audit_of(b, op)[0]
    assert started["action"] == "rollback.started"
    assert started["after"]["migrations_ahead"] == [f"prisma:{ADD_TOTAL}"]
    assert started["after"]["confirmed"] is True

    assert seen(b, env) == {"prisma": [INIT, ADD_TOTAL]}
    assert_problem(rollback(b, env, first), ErrorCode.SCHEMA_AHEAD)
    plain = start_deploy(b, env, second, "rollback")
    assert plain.status_code == 202, plain.text


async def test_a_deploy_and_other_ledgers_are_not_checked(
    b: Bench, dsns: Dsns, tmp_path: Path
) -> None:
    database_ready(dsns, b.w.org)
    ports = replace(b.ports, app_databases=FakeAppDatabases())
    env = b.w.preview
    first = await release_from(b, tmp_path / "r1", [INIT])
    second = await release_from(b, tmp_path / "r2", [INIT, ADD_TOTAL])
    await deployed(b, env, second, ports)
    op = await deployed(b, env, first, ports)
    assert audit_of(b, op)[0]["action"] == "deploy.started"
    assert seen(b, env) == {"prisma": [INIT, ADD_TOTAL]}


async def test_no_warning_without_a_database_or_with_unknown_migrations(
    b: Bench, dsns: Dsns, tmp_path: Path
) -> None:
    first = await release_from(b, tmp_path / "r1", [INIT], postgres=False)
    second = await release_from(b, tmp_path / "r2", [INIT, ADD_TOTAL], postgres=False)
    for release in (first, second):
        await deployed(b, b.w.preview, release, b.ports)
    assert ahead(b, b.w.preview, first) == []
    back = start_deploy(b, b.w.preview, first, "rollback")
    assert back.status_code == 202, back.text
    assert await run(b, back.json()["operation_id"]) == "healthy"

    database_ready(dsns, b.w.org)
    ports = replace(b.ports, app_databases=FakeAppDatabases())
    unknown = await build_release(b, b.w.preview, manifest_of(**STATEFUL))
    assert get(b, f"/v1/apps/{b.w.app}/releases/{unknown}").json()["latest_migrations"] is None
    await deployed(b, b.w.preview, unknown, ports)
    later = await release_from(b, tmp_path / "r3", [INIT, ADD_TOTAL])
    await deployed(b, b.w.preview, later, ports)
    assert ahead(b, b.w.preview, unknown) == []
    assert rollback(b, b.w.preview, unknown).status_code == 202


async def test_migrations_ahead_needs_a_release_of_the_app_and_a_builder(
    b: Bench, tmp_path: Path
) -> None:
    release = await release_from(b, tmp_path / "r1", [INIT])
    path = f"/v1/apps/{b.w.app}/environments/{b.w.preview}/migrations-ahead"
    assert_problem(get(b, f"{path}?release_id=rel_{'a' * 20}"), ErrorCode.NOT_FOUND)
    assert_problem(get(b, f"{path}?release_id={release}", b.t.member), ErrorCode.FORBIDDEN)
    assert get(b, path).status_code == 422


class Unreachable(FakeAppDatabases):
    async def recovery_point(self, service: str) -> RecoveryPoint:
        raise AppDatabaseError("DATABASE_UNAVAILABLE", "the agent is down")


async def test_a_production_deployment_records_its_recovery_point_once(
    b: Bench, dsns: Dsns, tmp_path: Path
) -> None:
    database_ready(dsns, b.w.org)
    fake = FakeAppDatabases()
    ports = replace(b.ports, app_databases=fake)
    first = await release_from(b, tmp_path / "r1", [INIT], b.w.prod)
    op = await deployed(b, b.w.prod, first, ports)
    point = operation(b, op)["recovery_point"]
    assert point["lsn"] == "0/16B3848"
    assert point["at"] is not None
    assert await run(b, op, ports) == "healthy"
    assert fake.calls.count(("recovery_point", service_name(b.w.prod))) == 1
    history = get(b, f"/v1/apps/{b.w.app}/environments/{b.w.prod}/deployments").json()
    assert history["items"][0]["recovery_point"] == point

    second = await release_from(b, tmp_path / "r2", [INIT, ADD_TOTAL], b.w.prod)
    later = await deployed(b, b.w.prod, second, ports)
    assert operation(b, later)["recovery_point"]["lsn"] == "0/16B3948"
    back = rollback(b, b.w.prod, first, confirm=True)
    assert back.status_code == 202, back.text
    assert await run(b, back.json()["operation_id"], ports) == "healthy"
    assert operation(b, back.json()["operation_id"])["recovery_point"]["lsn"] == "0/16B3A48"

    preview = await release_from(b, tmp_path / "r3", [ADD_TOTAL])
    op = await deployed(b, b.w.preview, preview, ports)
    assert operation(b, op)["recovery_point"] is None
    assert ("recovery_point", service_name(b.w.preview)) not in fake.calls


async def test_a_recovery_point_the_agent_cannot_give_is_the_control_planes_time(
    b: Bench, dsns: Dsns
) -> None:
    database_ready(dsns, b.w.org)
    ports = replace(b.ports, app_databases=Unreachable())
    release = await build_release(b, b.w.prod, manifest_of(**STATEFUL))
    op = await deployed(b, b.w.prod, release, ports)
    point = operation(b, op)["recovery_point"]
    assert point["lsn"] is None
    assert point["at"] is not None


async def test_a_recovery_point_through_the_cell_agent(b: Bench, cell: Cell, dsns: Dsns) -> None:
    database_ready(dsns, b.w.org)
    release = await build_release(b, b.w.prod, manifest_of(**STATEFUL))
    op = start_deploy(b, b.w.prod, release).json()["operation_id"]
    assert await run_through_the_cell(b, cell, op) == "healthy"
    point = operation(b, op)["recovery_point"]
    assert LSN.fullmatch(point["lsn"]) is not None
    assert point["at"].endswith("Z")


@dataclass
class Answer:
    body: dict[str, Any]

    def __call__(self, _request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=self.body)


@pytest.mark.parametrize(
    "body",
    [
        {"at": "2026-10-03T09:00:00Z"},
        {"at": "2026-10-03T09:00:00Z", "lsn": 23017288},
        {"at": "2026-10-03T09:00:00Z", "lsn": "0/16b3748; drop"},
        {"at": "2026-10-03T09:00:00", "lsn": "0/16B3748"},
        {"at": "yesterday", "lsn": "0/16B3748"},
    ],
)
async def test_the_agents_recovery_point_is_checked(body: dict[str, Any]) -> None:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(Answer(body)))
    databases = CellAppDatabases(AGENT, agent_token, client=client)
    with pytest.raises(AppDatabaseError) as raised:
        await databases.recovery_point("ssc-env-x")
    assert raised.value.code == "DATABASE_UNAVAILABLE"
    good = {"at": "2026-10-03T09:00:00.123456Z", "lsn": "0/16B3748"}
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(Answer(good)))
    point = await CellAppDatabases(AGENT, agent_token, client=client).recovery_point("ssc-env-x")
    assert (point.at.isoformat(), point.lsn) == ("2026-10-03T09:00:00.123456+00:00", "0/16B3748")


async def test_a_recovery_log_position_needs_a_time_and_its_shape(b: Bench) -> None:
    release = await build_release(b, b.w.preview)
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    for sets in ("recovery_lsn = '0/16B3748'", "recovery_at = now(), recovery_lsn = 'nonsense'"):
        with psycopg.connect(b.dsn) as conn, pytest.raises(psycopg.errors.CheckViolation):
            bind_org_sync(conn, b.w.org)
            conn.execute(f"update ssc.deployment set {sets} where id = %s", (op,))


def test_0026_downgrades_and_upgrades(dsns: Dsns) -> None:
    rev = importlib.import_module("ssc_control.db.migrations.versions.0026_migrations")
    name = f"m{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database=name).render_as_string(hide_password=False)
    columns = (
        "select table_name || '.' || column_name from information_schema.columns "
        "where table_schema = 'ssc' and (column_name = 'migrations' "
        "or column_name like 'recovery%%') order by 1"
    )

    def present() -> list[str]:
        with psycopg.connect(dsn) as conn:
            return [r[0] for r in conn.execute(columns).fetchall()]

    made = [
        "app_database.migrations",
        "build.migrations",
        "deployment.recovery_at",
        "deployment.recovery_lsn",
        "release.migrations",
    ]
    upgrade(dsn)
    assert present() == made
    downgrade(dsn, rev.down_revision)
    assert present() == []
    upgrade(dsn)
    assert present() == made
