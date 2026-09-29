"""Helpers the control-plane tests share: a migrated postgres:18, orgs, signed tokens, problems.

Plain functions live here because test modules cannot import ``conftest.py`` under
``--import-mode=importlib``; the root ``pyproject.toml`` puts this folder on ``pythonpath``.
The fixtures that wrap them (``dsns``, ``signing_key``) are in ``conftest.py``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
import psycopg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import Response
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

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


def wait_for_a_lock_wait(dsn: str, *, seconds: float = 10.0) -> None:
    """Return once another session waits on a lock: the call a test started is blocked."""
    deadline = time.monotonic() + seconds
    with psycopg.connect(dsn, autocommit=True) as conn:
        while time.monotonic() < deadline:
            row = conn.execute(
                "select count(*) from pg_locks where not granted and pid <> pg_backend_pid()"
            ).fetchone()
            if row is not None and row[0] > 0:
                return
            time.sleep(0.01)
    raise AssertionError(f"no session waited on a lock within {seconds} s")


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
