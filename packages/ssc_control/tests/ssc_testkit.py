"""Helpers the control-plane tests share: a migrated postgres:18, orgs, signed tokens, problems.

Plain functions live here because test modules cannot import ``conftest.py`` under
``--import-mode=importlib``; the root ``pyproject.toml`` puts this folder on ``pythonpath``.
The fixtures that wrap them (``dsns``, ``signing_key``) are in ``conftest.py``.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import jwt
import psycopg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import Response
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from testcontainers.postgres import PostgresContainer

from ssc_agent.app_database import AdminSqlError
from ssc_contracts.errors import CATALOGUE, PROBLEM_MEDIA_TYPE, ErrorCode, problem_type
from ssc_contracts.ids import new_id
from ssc_control.api.auth import API_TOKEN_TYP
from ssc_control.api.problems import REQUEST_ID_HEADER
from ssc_control.api.settings import USER_AUDIENCE
from ssc_control.db import (
    APP_ROLE,
    MIGRATE_ROLE,
    CreatedOrg,
    NewOrg,
    create_org,
    ensure_roles,
    make_engine,
    upgrade,
)

ISSUER = "https://auth.test"
KID = "test-1"


# ── database ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Dsns:
    superuser: str
    migrate: str
    app: str


def with_role(dsn: str, user: str, password: str) -> str:
    return make_url(dsn).set(username=user, password=password).render_as_string(hide_password=False)


@contextmanager
def control_db() -> Iterator[Dsns]:
    """A postgres:18 container with both roles created and the schema migrated to head."""
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


SCANNED_SCHEMAS = ("ssc", "procrastinate")


def secret_forms(value: str) -> set[str]:
    """``value`` as it could be stored: as is, JSON-escaped, URL-encoded, base64 and hex."""
    raw = value.encode()
    return {
        value,
        value.replace("\\", "\\\\").replace('"', '\\"'),
        quote(value, safe=""),
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
        raw.hex(),
    }


def find_secret_in(superuser_dsn: str, value: str) -> list[str]:
    """Every place ``value`` appears in the control database, as ``schema.table`` (``.args`` for
    job arguments, ``.before``/``.after`` for audit rows): every table of ``ssc`` and
    ``procrastinate``, whole rows as text, past RLS. Empty when the secret is nowhere."""
    forms = secret_forms(value)
    found: list[str] = []
    with psycopg.connect(superuser_dsn) as conn:
        tables = conn.execute(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema = any(%s) and table_type = 'BASE TABLE' order by 1, 2",
            (list(SCANNED_SCHEMAS),),
        ).fetchall()
        for schema, table in tables:
            rows = conn.execute(f'select t::text from "{schema}"."{table}" t').fetchall()
            if any(form in row for (row,) in rows for form in forms):
                found.append(f"{schema}.{table}")
        named = (
            ("procrastinate.procrastinate_jobs.args", "procrastinate.procrastinate_jobs", "args"),
            ("ssc.audit_event.before", "ssc.audit_event", "before"),
            ("ssc.audit_event.after", "ssc.audit_event", "after"),
        )
        for where, table, column in named:
            rows = conn.execute(f"select {column}::text from {table}").fetchall()
            if any(form in (row or "") for (row,) in rows for form in forms):
                found.append(where)
    return found


def wait_for_a_lock_wait(dsn: str, *, seconds: float = 10.0, blocker: int | None = None) -> None:
    """Return once another session waits on a lock (held by backend ``blocker``, when given):
    the call a test started is blocked."""
    deadline = time.monotonic() + seconds
    with psycopg.connect(dsn, autocommit=True) as conn:
        while time.monotonic() < deadline:
            row = conn.execute(
                "select count(*) from pg_locks where not granted and pid <> pg_backend_pid()"
                if blocker is None
                else "select count(*) from pg_stat_activity where %s = any(pg_blocking_pids(pid))",
                () if blocker is None else (blocker,),
            ).fetchone()
            if row is not None and row[0] > 0:
                return
            time.sleep(0.01)
    raise AssertionError(f"no session waited on a lock within {seconds} s")


# ── an instance set up the way Cloud SQL is (SSC-040) ────────────────────────


CLOUD_SQL_ADMIN = "cloudsqlsuperuser"
CLOUD_SQL_AGENT = "ssc_cell_agent"
CLOUD_SQL_AGENT_PASSWORD = "fake-agent-password-for-tests"
FAKE_SERVER_CA = "-----BEGIN CERTIFICATE-----\nZmFrZSBDQQ==\n-----END CERTIFICATE-----\n"


@dataclass(frozen=True)
class CloudSqlLike:
    host: str
    port: int
    superuser: str

    def dsn(self, user: str, password: str, database: str) -> str:
        """Plain text, as the test container has no certificate: verify-full is SSC-086 T5."""
        return psycopg.conninfo.make_conninfo(
            host=self.host,
            port=self.port,
            user=user,
            password=password,
            dbname=database,
            sslmode="disable",
        )


@contextmanager
def cloud_sql_like(max_connections: int = 25) -> Iterator[CloudSqlLike]:
    """A postgres:18 with ``db-f1-micro``'s 25 connections, a ``cloudsqlsuperuser`` that is no
    superuser but may create roles and databases and read activity, and the cell agent's own
    login in it, as Cloud SQL sets them up."""
    container = PostgresContainer("postgres:18", driver=None).with_command(
        f"postgres -c max_connections={max_connections}"
    )
    with container as pg:
        su = pg.get_connection_url()
        with psycopg.connect(su, autocommit=True) as conn:
            conn.execute(f"create role {CLOUD_SQL_ADMIN} nologin createrole createdb")
            conn.execute(f"grant pg_read_all_stats to {CLOUD_SQL_ADMIN}")
            conn.execute(
                f"create role {CLOUD_SQL_AGENT} login password '{CLOUD_SQL_AGENT_PASSWORD}' "
                f"in role {CLOUD_SQL_ADMIN}"
            )
        yield CloudSqlLike(pg.get_container_host_ip(), int(pg.get_exposed_port(5432)), su)


class LocalAdminSql:
    """The cell agent's ``AdminSql`` on a :func:`cloud_sql_like` instance, as ``executeSql``
    runs it: as the agent's own login, one transaction per batch, values back as text.
    ``statements`` keeps every statement run; ``fail_creates`` fails that many database
    creations first."""

    def __init__(self, db: CloudSqlLike) -> None:
        self.db = db
        self.statements: list[str] = []
        self.fail_creates = 0

    def _agent(self, database: str) -> str:
        return self.db.dsn(CLOUD_SQL_AGENT, CLOUD_SQL_AGENT_PASSWORD, database)

    async def run(self, database: str, statements: Sequence[str]) -> list[dict[str, Any]]:
        self.statements += statements
        rows: list[dict[str, Any]] = []
        try:
            async with await psycopg.AsyncConnection.connect(
                self._agent(database), autocommit=True
            ) as conn:
                async with conn.transaction():
                    for statement in statements:
                        cur = await conn.execute(statement.encode())
                        names = [c.name for c in cur.description or []]
                        rows = [
                            {
                                n: None if v is None else str(v)
                                for n, v in zip(names, r, strict=True)
                            }
                            for r in (await cur.fetchall() if names else [])
                        ]
        except psycopg.Error as exc:
            raise AdminSqlError(str(exc)) from None
        return rows

    async def create_database(self, name: str) -> None:
        if self.fail_creates:
            self.fail_creates -= 1
            raise AdminSqlError("databases.insert: injected failure")
        async with await psycopg.AsyncConnection.connect(
            self._agent("postgres"), autocommit=True
        ) as conn:
            await conn.execute(f"SET ROLE {CLOUD_SQL_ADMIN}".encode())
            try:
                await conn.execute(f"CREATE DATABASE {name}".encode())
            except psycopg.errors.DuplicateDatabase:
                pass

    async def endpoint(self) -> tuple[str, int]:
        return self.db.host, self.db.port

    async def server_ca(self) -> str:
        return FAKE_SERVER_CA


class MemoryVault:
    """``SecretCustody`` and ``SecretWriter`` in memory, for tests that must read a value back
    to prove where it works; nothing in SSC can."""

    def __init__(self) -> None:
        self.secrets: dict[str, list[bytes]] = {}

    async def ensure(self, secret: str) -> None:
        self.secrets.setdefault(secret, [])

    async def add_version(self, secret: str, value: bytes) -> str:
        self.secrets[secret].append(value)
        return str(len(self.secrets[secret]))

    def latest(self, secret: str) -> str:
        return self.secrets[secret][-1].decode()


async def backend_pid(conn: AsyncConnection) -> int:
    return int((await conn.execute(text("select pg_backend_pid()"))).scalar_one())


async def audit_head_is_free(conn: AsyncConnection, org_id: str) -> bool:
    """Whether ``conn`` can lock the org's audit head at once (``FOR UPDATE NOWAIT``)."""
    head = text("select 1 from ssc.audit_head where org_id = :org for update nowait")
    try:
        async with conn.begin_nested():
            await conn.execute(head, {"org": org_id})
    except DBAPIError as e:
        assert getattr(e.orig, "sqlstate", None) == "55P03", e  # lock_not_available
        return False
    return True


def make_org(dsn: str, name: str = "Acme") -> CreatedOrg:
    """``create_org`` through ``asyncio.run``: call it from sync code only."""

    async def go() -> CreatedOrg:
        engine = make_engine(dsn)
        try:
            spec = NewOrg(name, "Ada Admin", "ada@example.com", ISSUER, new_id("usr"))
            return await create_org(engine, spec)
        finally:
            await engine.dispose()

    return asyncio.run(go())


# ── credentials ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SigningKey:
    private_pem: bytes
    jwk: dict[str, Any]


def new_signing_key() -> SigningKey:
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk.update({"kid": KID, "alg": "ES256", "use": "sig"})
    return SigningKey(pem, jwk)


def mint(
    key: SigningKey,
    *,
    org: str,
    sub: str,
    kind: str = "user",
    audience: str = USER_AUDIENCE,
    jti: str | None = None,
    typ: str = API_TOKEN_TYP,
    kid: str = KID,
    issuer: str = ISSUER,
    expires_in: int = 300,
    **extra: Any,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": sub,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
        "jti": jti or f"cred_{uuid4().hex[:12]}",
        "org": org,
        "kind": kind,
        **extra,
    }
    return jwt.encode(claims, key.private_pem, algorithm="ES256", headers={"kid": kid, "typ": typ})


def auth(token: str, **headers: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **headers}


def new_key() -> str:
    return uuid4().hex


# ── responses ────────────────────────────────────────────────────────────────


def assert_problem(r: Response, code: ErrorCode) -> dict[str, Any]:
    entry = CATALOGUE[code]
    assert r.status_code == entry.status, r.text
    assert r.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = r.json()
    assert body["code"] == code.value
    assert body["type"] == problem_type(code)
    assert body["title"] == entry.title
    assert body["detail"] == entry.detail
    assert body["status"] == entry.status
    assert body["instance"] == r.request.url.path
    assert body["request_id"] == r.headers[REQUEST_ID_HEADER]
    assert set(body) == {"type", "title", "status", "detail", "instance", "code", "request_id"}
    return body
