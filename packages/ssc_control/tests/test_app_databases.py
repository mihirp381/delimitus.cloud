"""SSC-040: per-app Postgres through the control plane. A stateful deployment asks the cell agent
for its database; the agent makes it on a postgres:18 set up the way Cloud SQL is and writes the
password to the cell's Secret Manager; the control plane records references only.

Uses test_deploy's bench, test_secrets' Secret Manager emulator and spy, the Cloud Run emulator
and the cell agent in process.

Ticket "done when" checks that run here (the live run is SSC-086 T5):
  * the app gets DATABASE_URL as a pinned secret and
    the password reaches nothing in the control plane -> test_a_stateful_deploy_gets_its_own_...
  * the eleventh stateful environment is refused
    with DB_TIER_FULL and the upgrade is offered      -> test_the_eleventh_stateful_environment_...
  * usage in ``ssc status``                           -> test_a_stateful_deploy_gets_its_own_...
                                                         (and ssc_cli's test_status_shows_...)
Plus: rotation, the route refusals, no agent configured, the driver's environment, the platform
names refused by ``ssc secret set``, and migration 0022.
"""

from __future__ import annotations

import importlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import httpx2
import psycopg
import pytest
import test_deploy
from fastapi import FastAPI
from httpx import Response
from sqlalchemy.engine import make_url
from ssc_testkit import (
    CloudSqlLike,
    Dsns,
    LocalAdminSql,
    SigningKey,
    assert_problem,
    cloud_sql_like,
    find_secret_in,
    mint,
    new_key,
    with_api_cell,
    with_cell,
)
from test_deploy import (
    AGENT,
    CELL_RUNTIME,
    Bench,
    Clock,
    access_token,
    agent_token,
    build_release,
    get,
    manifest_of,
    operation,
    post,
    rows_of,
    run,
    start_deploy,
)
from test_secrets import SecretManagerEmulator, Spy, grant

from ssc_agent import app_database as agent_databases
from ssc_agent.app import create_app as create_agent
from ssc_agent.cloud_run import CloudRunDriver
from ssc_agent.secret_manager import CellSecretCustody, CellSecretWriter
from ssc_conformance.cloud_run_emulator import PROJECT, CloudRunEmulator
from ssc_contracts import app_database
from ssc_contracts.app_env import (
    DATABASE_CA,
    DATABASE_CA_PATH,
    DATABASE_URL,
    PGDATABASE,
    PGHOST,
    PGPASSWORD,
    PGPORT,
    PGSSLMODE,
    PGSSLROOTCERT,
    PGUSER,
)
from ssc_contracts.errors import CATALOGUE, ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.db import MIGRATE_ROLE, downgrade, upgrade
from ssc_control.deploy.deployments import HEALTH_POLL_SECONDS, HealthWait, run_deployment
from ssc_control.runtime.app_databases import CellAppDatabases, FakeAppDatabases
from ssc_control.runtime.cell_agent import CellAgentDriver
from ssc_control.runtime.driver import (
    DatabaseRow,
    EnvironmentRow,
    ReleaseRow,
    desired_for,
    service_name,
)
from ssc_control.runtime.reconciler import reconcile_env
from ssc_control.runtime.specs import BundleReleaseSpecs
from ssc_shared.runtime import ServiceSpec, database_name, secret_id

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b

STATEFUL = {"state": {"postgres": True}}


class Passwords:
    """Obviously fake passwords, one new one per call."""

    def __init__(self) -> None:
        self.made: list[str] = []

    def __call__(self) -> str:
        self.made.append(f"fake-db-password-{len(self.made):04d}")
        return self.made[-1]


@pytest.fixture(scope="module")
def instance() -> Iterator[CloudSqlLike]:
    with cloud_sql_like() as db:
        yield db


@dataclass
class Cell:
    run: CloudRunEmulator
    sm: SecretManagerEmulator
    clock: Clock
    sql: LocalAdminSql
    passwords: Passwords
    agent: agent_databases.CellAppDatabases
    spy: Spy
    runtime: CellAgentDriver
    databases: CellAppDatabases


@pytest.fixture
async def cell(b: Bench, instance: CloudSqlLike) -> AsyncIterator[Cell]:
    run = CloudRunEmulator()
    sm = SecretManagerEmulator(run.accounts)
    clock = Clock(run)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "secretmanager.googleapis.com":
            return sm.handler(request)
        return run.handler(request)

    def mock() -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))

    cloud_run = CloudRunDriver(CELL_RUNTIME, access_token, client=mock(), sleep=clock.driver_sleep)
    custody = CellSecretCustody(
        CELL_RUNTIME,
        access_token,
        cloud_run.ensure_identity,
        client=mock(),
        sleep=clock.driver_sleep,
    )
    writer = CellSecretWriter(PROJECT, access_token, client=mock())
    sql, passwords = LocalAdminSql(instance), Passwords()
    agent = agent_databases.CellAppDatabases(sql, custody, writer, passwords=passwords)
    spy = Spy(
        httpx2.ASGITransport(app=create_agent(cloud_run, None, custody, agent, org_id=b.w.org))
    )
    runtime = CellAgentDriver(
        AGENT, agent_token, org_id=b.w.org, client=httpx2.AsyncClient(transport=spy)
    )
    databases = CellAppDatabases(
        AGENT, agent_token, org_id=b.w.org, client=httpx2.AsyncClient(transport=spy)
    )
    with_api_cell(cast("FastAPI", b.client.app), app_databases=databases)
    yield Cell(run, sm, clock, sql, passwords, agent, spy, runtime, databases)
    await runtime.aclose()
    await databases.aclose()
    await writer.aclose()
    with psycopg.connect(instance.superuser, autocommit=True) as conn:
        for (name,) in conn.execute("select datname from pg_database where datname ~ '^app_'"):
            conn.execute(f"drop database {name} with (force)")
        for (name,) in conn.execute("select rolname from pg_roles where rolname ~ '^app_'"):
            conn.execute(f"drop role {name}")


def database_ready(dsns: Dsns, org: str) -> None:
    """The org's Cloud SQL instance exists (SSC-087 made it)."""
    with psycopg.connect(dsns.superuser) as conn:
        conn.execute(
            "insert into ssc.cell_resource (org_id, resource, state, cause, actor_kind, actor_id, "
            "started_at, ready_at) values (%s, 'database', 'ready', 'admin', 'operator', "
            "'op_test', now(), now())",
            (org,),
        )


async def run_through_the_cell(b: Bench, cell: Cell, op: str) -> str:
    ports = with_cell(b.ports, runtime=cell.runtime, app_databases=cell.databases)
    health = HealthWait(within=5.0, every=HEALTH_POLL_SECONDS, sleep=cell.clock.health_sleep)
    return await run_deployment(ports, org_id=b.w.org, deployment_id=op, health=health)


def database_path(b: Bench, env: str) -> str:
    return f"/v1/apps/{b.w.app}/environments/{env}/database"


def pinned(b: Bench, op: str) -> Any:
    (row,) = rows_of(b.dsn, b.w.org, "select secret_refs from ssc.deployment where id = %s", op)
    return row["secret_refs"]


def latest(cell: Cell, env: str, name: str) -> str:
    return cell.sm.secrets[secret_id(service_name(env), name)]["versions"][-1].decode()


def dsn_of(instance: CloudSqlLike, url: str) -> str:
    parts = urlsplit(url)
    assert parts.username is not None and parts.password is not None
    return instance.dsn(parts.username, unquote(parts.password), parts.path.removeprefix("/"))


def ref(env: str, name: str, version: str) -> dict[str, Any]:
    secret = secret_id(service_name(env), name)
    return {"name": name, "valueSource": {"secretKeyRef": {"secret": secret, "version": version}}}


# ── the whole path ───────────────────────────────────────────────────────────


async def test_a_stateful_deploy_gets_its_own_database_and_no_value_reaches_the_control_plane(
    b: Bench,
    cell: Cell,
    dsns: Dsns,
    instance: CloudSqlLike,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    env, name = b.w.preview, database_name(service_name(b.w.preview))
    database_ready(dsns, b.w.org)
    replies: list[Response] = [get(b, database_path(b, env))]
    assert replies[0].json()["present"] is False

    release = await build_release(b, env, manifest_of(**STATEFUL))
    first = start_deploy(b, env, release).json()["operation_id"]
    assert await run_through_the_cell(b, cell, first) == "healthy"
    assert pinned(b, first) == {DATABASE_URL: "1", PGPASSWORD: "1", DATABASE_CA: "1"}
    template = cell.run.services[service_name(env)].body["template"]
    (container,) = template["containers"]
    plain = {v["name"]: v.get("value") for v in container["env"] if "valueSource" not in v}
    assert {
        k: plain[k] for k in (PGHOST, PGPORT, PGDATABASE, PGUSER, PGSSLMODE, PGSSLROOTCERT)
    } == {
        PGHOST: instance.host,
        PGPORT: str(instance.port),
        PGDATABASE: name,
        PGUSER: name,
        PGSSLMODE: "verify-full",
        PGSSLROOTCERT: DATABASE_CA_PATH,
    }
    assert [v for v in container["env"] if "valueSource" in v] == [
        ref(env, DATABASE_URL, "1"),
        ref(env, PGPASSWORD, "1"),
    ]
    ca_dir, _, ca_file = DATABASE_CA_PATH.rpartition("/")
    assert container["volumeMounts"] == [{"name": "ssc-db-ca", "mountPath": ca_dir}]
    ca_secret = secret_id(service_name(env), DATABASE_CA)
    assert template["volumes"] == [
        {
            "name": "ssc-db-ca",
            "secret": {"secret": ca_secret, "items": [{"path": ca_file, "version": "1"}]},
        }
    ]
    url = latest(cell, env, DATABASE_URL)
    with psycopg.connect(dsn_of(instance, url)) as conn:
        assert conn.execute("select session_user").fetchone() == (name,)
    (row,) = rows_of(b.dsn, b.w.org, "select host, port, connection_limit from ssc.app_database")
    assert row == {"host": instance.host, "port": instance.port, "connection_limit": 2}

    shown = get(b, database_path(b, env))
    assert shown.status_code == 200, shown.text
    out = shown.json()
    assert {k: out[k] for k in ("present", "database", "connection_limit", "pool_size")} == {
        "present": True,
        "database": name,
        "connection_limit": app_database.CONNECTION_LIMIT,
        "pool_size": app_database.POOL_SIZE,
    }
    assert (out["places_used"], out["places_total"], out["connections"]) == (1, 10, 0)
    assert out["size_bytes"] > 0
    replies.append(shown)

    rotated = post(b, f"{database_path(b, env)}/rotate", {}, None)
    assert rotated.status_code == 202, rotated.text
    second = rotated.json()["operation_id"]
    assert rotated.headers["Location"] == f"/v1/operations/{second}"
    assert await run_through_the_cell(b, cell, second) == "healthy"
    assert pinned(b, second) == {DATABASE_URL: "2", PGPASSWORD: "2", DATABASE_CA: "2"}
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        psycopg.connect(dsn_of(instance, url))
    with psycopg.connect(dsn_of(instance, latest(cell, env, DATABASE_URL))) as conn:
        assert conn.execute("select current_database()").fetchone() == (name,)
    outcome = await reconcile_env(
        b.ports.engine, cell.runtime, BundleReleaseSpecs(), org_id=b.w.org, env_id=env
    )
    assert outcome.kind == "converged"
    replies += [rotated, get(b, f"/v1/operations/{second}"), get(b, f"/v1/apps/{b.w.app}")]

    audit = rows_of(
        b.dsn,
        b.w.org,
        "select action, after ->> 'name' as name, after ->> 'version' as version "
        "from ssc.audit_event where target_kind = 'secret_ref' order by seq",
    )
    bound = [{"action": "secret.bound", "name": n, "version": "1"} for n in app_database.SECRETS]
    again = [{"action": "secret.rotated", "name": n, "version": "2"} for n in app_database.SECRETS]
    assert audit == bound + again

    urls = [
        v.decode() for v in cell.sm.secrets[secret_id(service_name(env), DATABASE_URL)]["versions"]
    ]
    assert len(cell.passwords.made) == len(urls) == 2
    svc = cell.run.services[service_name(env)]
    for value in [*cell.passwords.made, *urls]:
        assert find_secret_in(dsns.superuser, value) == []
        assert not [r for r in replies if value in r.text]
        assert value not in caplog.text
        assert not [s for s in cell.spy.seen if value.encode() in s]
        assert value not in json.dumps([svc.body, svc.revisions], default=str)
        assert not [s for s in cell.sql.statements if value in s]


async def test_the_eleventh_stateful_environment_fails_with_db_tier_full(
    b: Bench, cell: Cell, dsns: Dsns, instance: CloudSqlLike
) -> None:
    database_ready(dsns, b.w.org)
    for i in range(10):
        await cell.agent.ensure("ssc-a-" + f"other{i:02d}".ljust(20, "0"))
    release = await build_release(b, b.w.preview, manifest_of(**STATEFUL))
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert await run_through_the_cell(b, cell, op) == "failed"
    out = operation(b, op)
    assert (out["state"], out["failure_code"]) == ("failed", "DB_TIER_FULL")
    assert rows_of(b.dsn, b.w.org, "select * from ssc.app_database") == []
    assert rows_of(b.dsn, b.w.org, "select * from ssc.secret_ref") == []
    assert service_name(b.w.preview) not in cell.run.services
    name = database_name(service_name(b.w.preview))
    with psycopg.connect(instance.superuser) as conn:
        made = conn.execute("select count(*) from pg_roles where rolname like %s", (f"{name}%",))
        assert made.fetchone() == (0,)
    entry = CATALOGUE[ErrorCode.DB_TIER_FULL]
    assert "db-g1-small" in entry.detail
    assert "26" in entry.detail


async def test_a_stateful_deploy_with_no_agent_for_databases_fails_unavailable(
    b: Bench, dsns: Dsns
) -> None:
    database_ready(dsns, b.w.org)
    release = await build_release(b, b.w.preview, manifest_of(**STATEFUL))
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert await run(b, op) == "failed"
    assert operation(b, op)["failure_code"] == "DATABASE_UNAVAILABLE"


async def test_a_fake_database_is_recorded_and_pinned_once(b: Bench, dsns: Dsns) -> None:
    database_ready(dsns, b.w.org)
    fake = FakeAppDatabases()
    ports = with_cell(b.ports, app_databases=fake)
    release = await build_release(b, b.w.preview, manifest_of(**STATEFUL))
    for _ in range(2):
        op = start_deploy(b, b.w.preview, release).json()["operation_id"]
        assert await run(b, op, ports) == "healthy"
        assert pinned(b, op) == dict.fromkeys(app_database.SECRETS, "1")
    assert fake.calls == [("ensure", service_name(b.w.preview))]


# ── refusals ─────────────────────────────────────────────────────────────────


async def test_rotation_needs_a_person_who_may_change_the_environment_and_a_database(
    b: Bench, cell: Cell, dsns: Dsns, signing_key: SigningKey
) -> None:
    path = f"{database_path(b, b.w.preview)}/rotate"
    assert_problem(post(b, path, {}, None), ErrorCode.NOT_FOUND)
    database_ready(dsns, b.w.org)
    release = await build_release(b, b.w.preview, manifest_of(**STATEFUL))
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert await run_through_the_cell(b, cell, op) == "healthy"
    agent = mint(signing_key, org=b.w.org, sub=b.w.admin, jti=f"cred_{new_key()[:16]}", agent=True)
    assert_problem(post(b, path, {}, agent), ErrorCode.AGENT_SESSION_REFUSED)
    assert_problem(post(b, path, {}, b.t.member), ErrorCode.FORBIDDEN)
    assert get(b, database_path(b, b.w.preview), b.t.member).json()["present"] is True
    busy = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert_problem(post(b, path, {}, None), ErrorCode.DEPLOYMENT_IN_FLIGHT)
    assert await run_through_the_cell(b, cell, busy) == "healthy"
    with_api_cell(cast("FastAPI", b.client.app), app_databases=None)
    assert_problem(post(b, path, {}, None), ErrorCode.DATABASE_UNAVAILABLE)
    shown = get(b, database_path(b, b.w.preview)).json()
    assert (shown["present"], shown["size_bytes"], shown["places_total"]) == (True, None, None)
    assert len(cell.passwords.made) == 1


async def test_ssc_secret_set_refuses_every_database_name(b: Bench) -> None:
    for name in (DATABASE_URL, PGPASSWORD, DATABASE_CA, PGHOST, PGUSER):
        assert_problem(grant(b, b.w.preview, name), ErrorCode.VALIDATION_FAILED)


# ── the driver ───────────────────────────────────────────────────────────────


def test_the_driver_gives_a_stateful_app_its_database_and_leaves_it_out_otherwise() -> None:
    env = EnvironmentRow(id=new_id("env"), org_id=new_id("org"), app_id=new_id("app"), name="prod")
    release = ReleaseRow(id=new_id("rel"), image_digest="sha256:" + "a" * 64)
    refs = {"STRIPE_KEY": "3", **dict.fromkeys(app_database.SECRETS, "1")}
    database = DatabaseRow(host="db.test", port=5432)
    name = database_name(service_name(env.id))

    def spec(**tables: Any) -> ServiceSpec:
        desired = desired_for(
            env=env,
            release=release,
            manifest=manifest_of(**tables),
            app_status="active",
            secrets=refs,
            database=database,
        )
        assert isinstance(desired, ServiceSpec)
        return desired

    stateful = spec(**STATEFUL)
    assert dict(stateful.secrets) == refs
    assert {k: v for k, v in stateful.env.items() if k.startswith("PG")} == {
        PGHOST: "db.test",
        PGPORT: "5432",
        PGDATABASE: name,
        PGUSER: name,
        PGSSLMODE: "verify-full",
        PGSSLROOTCERT: DATABASE_CA_PATH,
    }
    assert stateful.max_instances == app_database.MAX_INSTANCES == 1
    stateless = spec()
    assert dict(stateless.secrets) == {"STRIPE_KEY": "3"}
    assert not [k for k in stateless.env if k.startswith("PG")]


def test_0022_downgrades_and_upgrades(dsns: Dsns) -> None:
    rev = importlib.import_module("ssc_control.db.migrations.versions.0022_app_databases")
    name = f"m{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database=name).render_as_string(hide_password=False)

    def present() -> bool:
        with psycopg.connect(dsn) as conn:
            return conn.execute("select to_regclass('ssc.app_database')").fetchone() != (None,)

    upgrade(dsn)
    assert present()
    downgrade(dsn, rev.down_revision)
    assert not present()
    upgrade(dsn)
    assert present()
