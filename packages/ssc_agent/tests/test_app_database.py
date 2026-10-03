"""SSC-040: app databases made by the cell agent, on a postgres:18 set up the way Cloud SQL is
(25 connections, a ``cloudsqlsuperuser`` that is no superuser), and the Cloud SQL Admin API
client against a mock.

Ticket "done when" checks that run here; the live run against Cloud SQL is SSC-086 T5:
  * the cross-connect test fails as it should    -> test_ten_environments_fit_and_none_reaches_...
  * the eleventh stateful environment is refused -> test_the_eleventh_environment_is_refused_...
  * ten apps at their limit leave a connection
    for administration                           -> test_ten_apps_at_their_limit_leave_room_...
Plus the review of the connection arithmetic: a new revision still connects while the live
instance holds its pool                          -> test_a_new_revision_connects_while_the_live_...
Plus: the recipe, re-running it after a failure, rotation, what an app role cannot do, the
connection limit, usage, the URL form, the password in no statement and no reply, the agent's
routes and the Admin API client.
"""

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx2
import psycopg
import pytest
from ssc_testkit import (
    CLOUD_SQL_ADMIN,
    FAKE_SERVER_CA,
    CloudSqlLike,
    LocalAdminSql,
    MemoryVault,
    cloud_sql_like,
)

from ssc_agent.app import create_app
from ssc_agent.app_database import (
    AdminSqlError,
    CellAppDatabases,
    DatabaseMissingError,
    TierFullError,
    database_url,
    scram_verifier,
)
from ssc_agent.cloud_sql import SQL_API, CloudSqlAdmin
from ssc_contracts import app_database
from ssc_contracts.app_env import DATABASE_CA, DATABASE_CA_PATH, DATABASE_URL, PGPASSWORD
from ssc_shared.runtime import database_name, secret_id

PROJECT = "cell-project-test"


def service(i: int) -> str:
    return "ssc-a-" + f"t{i:02d}".ljust(20, "0")


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


@pytest.fixture
def sql(instance: CloudSqlLike) -> Iterator[LocalAdminSql]:
    yield LocalAdminSql(instance)
    with psycopg.connect(instance.superuser, autocommit=True) as conn:
        for (name,) in conn.execute("select datname from pg_database where datname ~ '^app_'"):
            conn.execute(f"drop database {name} with (force)")
        for (name,) in conn.execute("select rolname from pg_roles where rolname ~ '^app_'"):
            conn.execute(f"drop role {name}")


@pytest.fixture
def vault() -> MemoryVault:
    return MemoryVault()


@pytest.fixture
def passwords() -> Passwords:
    return Passwords()


@pytest.fixture
def dbs(sql: LocalAdminSql, vault: MemoryVault, passwords: Passwords) -> CellAppDatabases:
    return CellAppDatabases(sql, vault, vault, passwords=passwords)


def url_of(vault: MemoryVault, svc: str) -> str:
    return vault.latest(secret_id(svc, DATABASE_URL))


def dsn_of(instance: CloudSqlLike, url: str, database: str | None = None) -> str:
    """The URL's user and password, against ``database`` (its own by default)."""
    parts = urlsplit(url)
    assert parts.username is not None and parts.password is not None
    own = parts.path.removeprefix("/")
    return instance.dsn(parts.username, unquote(parts.password), database or own)


def rows(instance: CloudSqlLike, sql: str, *args: object) -> list[tuple[Any, ...]]:
    with psycopg.connect(instance.superuser) as conn:
        return conn.execute(sql, args).fetchall()


# ── the recipe ───────────────────────────────────────────────────────────────


async def test_an_app_database_is_its_own_and_the_url_reaches_it(
    dbs: CellAppDatabases,
    sql: LocalAdminSql,
    vault: MemoryVault,
    instance: CloudSqlLike,
    passwords: Passwords,
) -> None:
    svc = service(0)
    name = database_name(svc)
    made = await dbs.ensure(svc)
    assert (made.database, made.user, made.host, made.port) == (
        name,
        name,
        instance.host,
        instance.port,
    )
    assert made.connection_limit == app_database.CONNECTION_LIMIT
    assert made.versions == {DATABASE_URL: "1", PGPASSWORD: "1", DATABASE_CA: "1"}

    (password,) = passwords.made
    url = url_of(vault, svc)
    assert url == (
        f"postgresql://{name}:{password}@{instance.host}:{instance.port}/{name}"
        f"?sslmode=verify-full&sslrootcert={DATABASE_CA_PATH}"
    )
    assert vault.latest(secret_id(svc, PGPASSWORD)) == password
    assert vault.latest(secret_id(svc, DATABASE_CA)) == FAKE_SERVER_CA
    assert not [s for s in sql.statements if password in s]
    assert [s for s in sql.statements if "PASSWORD 'SCRAM-SHA-256$4096:" in s]

    with psycopg.connect(dsn_of(instance, url)) as conn:
        who = conn.execute("select session_user, current_user, current_database()").fetchone()
        assert who == (name, f"{name}_owner", name)
        conn.execute("create table notes (id int primary key)")
        conn.execute("insert into notes values (1)")
        (owner,) = conn.execute(
            "select tableowner from pg_tables where tablename = 'notes'"
        ).fetchone() or (None,)
        assert owner == f"{name}_owner"

    (role,) = rows(
        instance,
        "select r.rolcanlogin, r.rolconnlimit, o.rolcanlogin, r.rolsuper, r.rolcreatedb, "
        "r.rolcreaterole from pg_roles r, pg_roles o where r.rolname = %s and o.rolname = %s",
        name,
        f"{name}_owner",
    )
    assert role == (True, app_database.CONNECTION_LIMIT, False, False, False, False)
    public_on_db = rows(
        instance,
        "select a.privilege_type from pg_database d, aclexplode(d.datacl) a "
        "where d.datname = %s and a.grantee = 0",
        name,
    )
    assert public_on_db == []
    (dba,) = rows(
        instance, "select datdba::regrole::text from pg_database where datname = %s", name
    )
    assert dba == (f"{name}_owner",)
    superuser_in_app_db = psycopg.conninfo.make_conninfo(instance.superuser, dbname=name)
    with psycopg.connect(superuser_in_app_db) as conn:
        public_on_schema = conn.execute(
            "select a.privilege_type from pg_namespace n, aclexplode(n.nspacl) a "
            "where n.nspname = 'public' and a.grantee = 0"
        ).fetchall()
    assert public_on_schema == []


async def test_a_half_made_database_is_finished_by_running_again(
    dbs: CellAppDatabases, sql: LocalAdminSql, vault: MemoryVault, instance: CloudSqlLike
) -> None:
    svc = service(1)
    name = database_name(svc)
    sql.fail_creates = 1
    with pytest.raises(AdminSqlError, match="injected"):
        await dbs.ensure(svc)
    assert rows(instance, "select count(*) from pg_roles where rolname like %s", f"{name}%") == [
        (2,)
    ]
    assert rows(instance, "select count(*) from pg_database where datname = %s", name) == [(0,)]
    assert vault.secrets == {}

    assert (await dbs.ensure(svc)).versions[DATABASE_URL] == "1"
    assert (await dbs.ensure(svc)).versions[DATABASE_URL] == "2"
    assert rows(instance, "select count(*) from pg_roles where rolname like %s", f"{name}%") == [
        (2,)
    ]
    with psycopg.connect(dsn_of(instance, url_of(vault, svc))) as conn:
        assert conn.execute("select current_database()").fetchone() == (name,)


async def test_rotation_sets_a_new_password_and_the_old_one_stops_working(
    dbs: CellAppDatabases, vault: MemoryVault, instance: CloudSqlLike
) -> None:
    svc = service(2)
    await dbs.ensure(svc)
    old = url_of(vault, svc)
    rotated = await dbs.rotate(svc)
    assert rotated.versions == {DATABASE_URL: "2", PGPASSWORD: "2", DATABASE_CA: "2"}
    new = url_of(vault, svc)
    assert new != old
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        psycopg.connect(dsn_of(instance, old))
    with psycopg.connect(dsn_of(instance, new)) as conn:
        assert conn.execute("select 1").fetchone() == (1,)
    with pytest.raises(DatabaseMissingError):
        await dbs.rotate(service(3))


async def test_an_app_role_cannot_reach_beyond_its_database(
    dbs: CellAppDatabases, vault: MemoryVault, instance: CloudSqlLike
) -> None:
    svc = service(4)
    await dbs.ensure(svc)
    url = url_of(vault, svc)
    refused = (
        "create database other",
        "create role other",
        "create extension dblink",
        "create extension postgres_fdw",
        "create function f() returns int language c as 'f'",
        f"set role {CLOUD_SQL_ADMIN}",
    )
    with psycopg.connect(dsn_of(instance, url), autocommit=True) as conn:
        for statement in refused:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement.encode())
    with psycopg.connect(dsn_of(instance, url, "postgres"), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(b"create table elsewhere (id int)")


async def test_usage_tells_presence_size_limit_and_places(
    dbs: CellAppDatabases, instance: CloudSqlLike
) -> None:
    svc = service(5)
    missing = await dbs.usage(svc)
    assert (missing.present, missing.size_bytes, missing.connection_limit) == (False, None, None)
    assert (missing.environments, missing.ceiling) == (0, 10)
    await dbs.ensure(svc)
    seen = await dbs.usage(svc)
    assert seen.present
    assert seen.size_bytes is not None and seen.size_bytes > 0
    assert (seen.connection_limit, seen.connections) == (app_database.CONNECTION_LIMIT, 0)
    assert (seen.environments, seen.ceiling) == (1, 10)


# ── ten places, and not one more ─────────────────────────────────────────────


async def test_ten_environments_fit_and_none_reaches_another(
    dbs: CellAppDatabases, vault: MemoryVault, instance: CloudSqlLike
) -> None:
    svcs = [service(i) for i in range(10)]
    for svc in svcs:
        await dbs.ensure(svc)
    for a in svcs:
        for b in svcs:
            dsn = dsn_of(instance, url_of(vault, a), database_name(b))
            if a == b:
                psycopg.connect(dsn).close()
                continue
            with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
                psycopg.connect(dsn)


async def test_the_eleventh_environment_is_refused_before_anything_is_made(
    dbs: CellAppDatabases, sql: LocalAdminSql, vault: MemoryVault, instance: CloudSqlLike
) -> None:
    for i in range(10):
        await dbs.ensure(service(i))
    eleventh = service(10)
    with pytest.raises(TierFullError, match="10 app databases"):
        await dbs.ensure(eleventh)
    name = database_name(eleventh)
    assert rows(instance, "select count(*) from pg_roles where rolname like %s", f"{name}%") == [
        (0,)
    ]
    assert rows(instance, "select count(*) from pg_database where datname = %s", name) == [(0,)]
    assert not [s for s in vault.secrets if s.startswith(eleventh)]
    assert (await dbs.ensure(service(0))).versions[DATABASE_URL] == "2"
    assert (await dbs.usage(service(0))).environments == 10


async def test_ten_apps_at_their_limit_leave_room_for_administration(
    dbs: CellAppDatabases, vault: MemoryVault, instance: CloudSqlLike
) -> None:
    svcs = [service(i) for i in range(10)]
    for svc in svcs:
        await dbs.ensure(svc)
    async with AsyncExitStack() as held:
        for svc in svcs:
            for _ in range(app_database.CONNECTION_LIMIT):
                await held.enter_async_context(
                    await psycopg.AsyncConnection.connect(dsn_of(instance, url_of(vault, svc)))
                )
        with pytest.raises(psycopg.OperationalError, match="too many connections for role"):
            psycopg.connect(dsn_of(instance, url_of(vault, svcs[0])))
        seen = await dbs.usage(svcs[0])
        assert seen.connections == app_database.CONNECTION_LIMIT
        assert (await dbs.rotate(svcs[1])).versions[DATABASE_URL] == "2"


async def test_a_new_revision_connects_while_the_live_instance_holds_its_pool(
    dbs: CellAppDatabases, vault: MemoryVault, instance: CloudSqlLike
) -> None:
    svc = service(0)
    await dbs.ensure(svc)
    live = app_database.MAX_INSTANCES * app_database.POOL_SIZE
    async with AsyncExitStack() as held:
        for _ in range(live):
            await held.enter_async_context(
                await psycopg.AsyncConnection.connect(dsn_of(instance, url_of(vault, svc)))
            )
        assert (await dbs.rotate(svc)).versions[DATABASE_URL] == "2"
        incoming = await held.enter_async_context(
            await psycopg.AsyncConnection.connect(dsn_of(instance, url_of(vault, svc)))
        )
        assert await (await incoming.execute("select 1")).fetchone() == (1,)
        assert (await dbs.usage(svc)).connections == app_database.CONNECTION_LIMIT
        with pytest.raises(psycopg.OperationalError, match="too many connections for role"):
            psycopg.connect(dsn_of(instance, url_of(vault, svc)))


def test_the_ceiling_is_ten_on_the_base_tier_and_twenty_two_on_the_next() -> None:
    assert app_database.ceiling(25, 3) == 10
    assert app_database.ceiling(50, 3) == 22
    assert app_database.MAX_INSTANCES == 1
    assert app_database.MAX_INSTANCES * app_database.POOL_SIZE < app_database.CONNECTION_LIMIT
    for name in ("node-pg", "Prisma", "Django", f"max: {app_database.POOL_SIZE}"):
        assert name in app_database.POOL_FIX_IT


def test_the_url_quotes_the_password_and_the_verifier_is_postgres_form() -> None:
    url = database_url(user="app_x", password="a/b@c", host="h", port=5432, database="app_x")
    assert url.startswith("postgresql://app_x:a%2Fb%40c@h:5432/app_x?sslmode=verify-full&")
    verifier = scram_verifier("fake-db-password", b"0123456789abcdef")
    assert verifier.startswith("SCRAM-SHA-256$4096:MDEyMzQ1Njc4OWFiY2RlZg==$")
    assert "fake-db-password" not in verifier


# ── the agent's routes ───────────────────────────────────────────────────────


@pytest.fixture
async def agent(dbs: CellAppDatabases) -> AsyncIterator[httpx2.AsyncClient]:
    app = create_app(driver=None, databases=dbs)  # type: ignore[arg-type]
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://agent"
    ) as client:
        yield client


async def test_the_agent_answers_references_never_a_value(
    agent: httpx2.AsyncClient, vault: MemoryVault, passwords: Passwords
) -> None:
    svc = service(6)
    made = await agent.post("/v1/databases/ensure", json={"service": svc})
    assert made.status_code == 200, made.text
    assert set(made.json()) == {"database", "user", "host", "port", "connection_limit", "versions"}
    rotated = await agent.post("/v1/databases/rotate", json={"service": svc})
    assert rotated.json()["versions"] == {DATABASE_URL: "2", PGPASSWORD: "2", DATABASE_CA: "2"}
    usage = await agent.post("/v1/databases/usage", json={"service": svc})
    assert usage.json()["present"] is True
    for value in [*passwords.made, url_of(vault, svc)]:
        assert value not in made.text + rotated.text + usage.text

    missing = await agent.post("/v1/databases/rotate", json={"service": service(7)})
    assert (missing.status_code, missing.json()["code"]) == (404, "DATABASE_NOT_FOUND")
    bad = await agent.post("/v1/databases/ensure", json={"service": "postgres"})
    assert (bad.status_code, bad.json()["code"]) == (400, "INVALID_REQUEST")
    unknown = await agent.post("/v1/databases/drop", json={"service": svc})
    assert unknown.status_code == 404


async def test_a_full_instance_is_db_tier_full_at_the_agent(agent: httpx2.AsyncClient) -> None:
    for i in range(10):
        assert (await agent.post("/v1/databases/ensure", json={"service": service(i)})).is_success
    full = await agent.post("/v1/databases/ensure", json={"service": service(10)})
    assert (full.status_code, full.json()["code"]) == (409, "DB_TIER_FULL")


async def test_an_agent_without_an_instance_makes_no_database() -> None:
    app = create_app(driver=None)  # type: ignore[arg-type]
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://agent"
    ) as client:
        r = await client.post("/v1/databases/ensure", json={"service": service(0)})
    assert (r.status_code, r.json()["code"]) == (503, "DATABASES_NOT_CONFIGURED")


# ── the Cloud SQL Admin API client ───────────────────────────────────────────


INSTANCE = f"{SQL_API}/projects/{PROJECT}/instances/ssc-cell"


class AdminApi:
    """The few Admin API v1 calls the agent makes, scripted."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.sql: dict[str, Any] = {"results": []}
        self.insert = httpx2.Response(200, json={"name": "op-1"})
        self.polls = [{"status": "RUNNING"}, {"status": "DONE"}]
        self.instance: dict[str, Any] = {}

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == f"{INSTANCE}/executeSql":
            return httpx2.Response(200, json=self.sql)
        if url == f"{INSTANCE}/databases":
            return self.insert
        if url == f"{SQL_API}/projects/{PROJECT}/operations/op-1":
            return httpx2.Response(200, json=self.polls.pop(0))
        if url == INSTANCE:
            return httpx2.Response(200, json=self.instance)
        if url == f"{INSTANCE}/listServerCas":
            return httpx2.Response(200, json={"certs": [{"cert": "PEM-1"}, {"cert": "PEM-2\n"}]})
        return httpx2.Response(404, json={"error": {"status": "NOT_FOUND", "message": url}})


@pytest.fixture
def api() -> AdminApi:
    return AdminApi()


async def no_sleep(_: float) -> None:
    return None


async def token() -> str:
    return "fake-access-token"


@pytest.fixture
def admin(api: AdminApi) -> CloudSqlAdmin:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(api.handler))
    return CloudSqlAdmin(PROJECT, "ssc-cell", token, client=client, sleep=no_sleep)


async def test_execute_sql_sends_one_batch_and_reads_the_last_result(
    admin: CloudSqlAdmin, api: AdminApi
) -> None:
    api.sql = {
        "results": [
            {"columns": [{"name": "x"}], "rows": [{"values": [{"value": "0"}]}]},
            {
                "columns": [{"name": "a"}, {"name": "b"}],
                "rows": [{"values": [{"value": "1"}, {"nullValue": True}]}],
            },
        ]
    }
    assert await admin.run("postgres", ["SELECT 0", "SELECT 1"]) == [{"a": "1", "b": None}]
    (sent,) = api.requests
    assert sent.headers["Authorization"] == "Bearer fake-access-token"
    assert json.loads(sent.content) == {
        "sqlStatement": "SELECT 0;\nSELECT 1",
        "database": "postgres",
        "autoIamAuthn": True,
        "partialResultMode": "FAIL_PARTIAL_RESULT",
        "application": "ssc-cell-agent",
    }


async def test_a_failed_statement_is_an_error_though_http_says_200(
    admin: CloudSqlAdmin, api: AdminApi
) -> None:
    api.sql = {"status": {"code": 3, "message": "ERROR: invalid input syntax"}, "results": []}
    with pytest.raises(AdminSqlError, match="invalid input syntax"):
        await admin.run("postgres", ["SELECT 1"])


async def test_create_database_waits_for_its_operation_and_tolerates_one_that_exists(
    admin: CloudSqlAdmin, api: AdminApi
) -> None:
    await admin.create_database("app_x")
    assert json.loads(api.requests[0].content) == {"name": "app_x"}
    assert len(api.requests) == 3
    api.insert = httpx2.Response(
        409, json={"error": {"status": "ALREADY_EXISTS", "message": "app_x already exists"}}
    )
    await admin.create_database("app_x")
    api.insert = httpx2.Response(200, json={"name": "op-1"})
    api.polls = [{"status": "DONE", "error": {"errors": [{"message": "quota"}]}}]
    with pytest.raises(AdminSqlError, match="quota"):
        await admin.create_database("app_y")
    api.insert = httpx2.Response(403, json={"error": {"status": "FORBIDDEN", "message": "no"}})
    with pytest.raises(AdminSqlError) as refused:
        await admin.create_database("app_y")
    assert refused.value.status == 403


async def test_the_endpoint_is_the_dns_name_else_the_private_address(
    admin: CloudSqlAdmin, api: AdminApi
) -> None:
    api.instance = {
        "dnsName": "abc.us-central1.sql.goog.",
        "ipAddresses": [{"type": "PRIVATE", "ipAddress": "10.20.0.3"}],
    }
    assert await admin.endpoint() == ("abc.us-central1.sql.goog", 5432)
    api.instance = {"ipAddresses": [{"type": "PRIVATE", "ipAddress": "10.20.0.3"}]}
    assert await admin.endpoint() == ("10.20.0.3", 5432)
    api.instance = {}
    with pytest.raises(AdminSqlError, match="no private address"):
        await admin.endpoint()
    assert await admin.server_ca() == "PEM-1\nPEM-2\n"
