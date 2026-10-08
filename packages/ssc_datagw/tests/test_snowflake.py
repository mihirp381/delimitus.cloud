"""The Snowflake connector against a fake SQL API v2 over TLS in-process (GA-5 B8).

Each test that reads starts a FastAPI app under ``uvicorn`` on 127.0.0.1 with a server
certificate for ``localhost`` from the test PKI, so every request crosses a real TLS socket. The
app answers ``POST /api/v2/statements``, the status and partition ``GET`` and the cancel as
Snowflake does: it verifies the bearer JWT against the test user's public key (RS256, the
``KEYPAIR_JWT`` token type, ``iss`` naming the key's SHA-256 fingerprint, ``sub`` the user, at
most an hour) and answers Snowflake-shaped errors otherwise. Its answers are keyed by the
statement: ``ORDERS`` (every type the API sends), a statement in three partitions, one still
running for two answers, one that runs until it is cancelled, one answered 429 once, and
``002003``, ``001003`` and ``003001``. It records every request body, poll and cancel. The
user's key is made here, never read from disk. The connector suite runs against it; the
conversions, the bindings, the target model, the JWT and the error map are unit-tested without
it."""

import asyncio
import base64
import hashlib
import json
import logging
import socket
import time
import traceback
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from datetime import time as clock
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import jwt
import pytest
import uvicorn
from connector_suite import CHECKS, Subject, ask, conform, read
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pki import Pki, make_pki
from pydantic import ValidationError

from ssc_datagw.connectors import (
    QueryFailedError,
    QueryRefusedError,
    UpstreamUnavailableError,
    jsonable,
)
from ssc_datagw.snowflake import (
    DESCRIBE,
    NOT_A_KEY,
    POLL_SECONDS,
    TOKEN_SECONDS,
    RowType,
    SnowflakeConnector,
    SnowflakeTarget,
    account_url,
    bindings,
    claims,
    failure,
    fingerprint,
    jwt_account,
    placeholders,
    row,
    row_types,
    scalar,
)
from ssc_datagw.tls import tls_context

ACCOUNT = "myorg-acct_1"
USER = "ssc_datagw"
SUB = "MYORG-ACCT_1.SSC_DATAGW"
DATABASE = "REPORTING"
SCHEMA = "SALES"
WAREHOUSE = "READ_WH"
ROLE = "SSC_DATAGW_READ"
BODY_MARK = "do-not-echo-the-body"
"""In every error body the fake sends; no error may carry it."""
PLACED = 1_704_164_645
"""2024-01-02T03:04:05Z in seconds."""
DAY = (date(2024, 1, 2) - date(1970, 1, 1)).days
AT = 3 * 3600 + 4 * 60 + 5

READ = "SELECT * FROM orders"
PARAMS = "SELECT * FROM orders WHERE id = ?"
MANY = "SELECT * FROM many"
POLLED = "SELECT * FROM polled"
SLOW = "SELECT * FROM slow"
LIMITED = "SELECT * FROM limited"
MISSING = "SELECT * FROM missing"
SYNTAX = "SELECT 'syntax' AS x"
SECRET = "SELECT * FROM secret"

ORDERS_TYPE: list[dict[str, Any]] = [
    {"name": "ID", "type": "fixed", "precision": 38, "scale": 0, "nullable": False},
    {"name": "AMOUNT", "type": "fixed", "precision": 12, "scale": 2, "nullable": True},
    {"name": "RATIO", "type": "real", "precision": None, "scale": None, "nullable": True},
    {"name": "NOTE", "type": "text", "length": 100, "nullable": True},
    {"name": "OK", "type": "boolean", "nullable": True},
    {"name": "PLACED_ON", "type": "date", "nullable": True},
    {"name": "AT", "type": "time", "scale": 9, "nullable": True},
    {"name": "PLACED", "type": "timestamp_ntz", "scale": 9, "nullable": True},
    {"name": "PLACED_TZ", "type": "timestamp_tz", "scale": 9, "nullable": True},
    {"name": "RAW", "type": "binary", "nullable": True},
    {"name": "META", "type": "variant", "nullable": True},
]


def _order(i: int) -> list[str | None]:
    """Row ``i``: ``NOTE`` is null in the third."""
    return [
        str(i),
        f"{i}.25",
        str(i / 2),
        f"note {i}" if i < 3 else None,
        "true" if i % 2 == 0 else "false",
        str(DAY + i - 1),
        f"{AT + i - 1}.123456789",
        f"{PLACED + i - 1}.123456789",
        f"{PLACED + i - 1}.123456789 1500",
        f"00ff{i:02x}",
        json.dumps({"a": [i, None]}),
    ]


ORDERS = [_order(i) for i in range(1, 4)]
FIRST_ROW = [
    1,
    "1.25",
    0.5,
    "note 1",
    False,
    "2024-01-02",
    "03:04:05.123456",
    "2024-01-02T03:04:05.123456",
    "2024-01-02T04:04:05.123456+01:00",
    "AP8B",
    {"a": [1, None]},
]
X_TYPE: list[dict[str, Any]] = [{"name": "X", "type": "text", "nullable": True}]


def _xs(n: int) -> list[list[str | None]]:
    return [[f"x{i}"] for i in range(1, n + 1)]


@dataclass(frozen=True)
class Answer:
    """What the fake answers a statement: its rows in partitions of ``partition`` rows after
    ``running`` answers of 202, or 202 until cancelled (``slow``), or an ``error``."""

    row_type: list[dict[str, Any]] = field(default_factory=lambda: X_TYPE)
    rows: list[list[str | None]] = field(default_factory=list[list[str | None]])
    partition: int | None = None
    running: int = 0
    slow: bool = False
    error: tuple[int, str, str] | None = None


DESCRIBE_TYPE: list[dict[str, Any]] = [
    {"name": "TABLE_NAME", "type": "text", "nullable": False},
    {"name": "COLUMN_NAME", "type": "text", "nullable": False},
    {"name": "DATA_TYPE", "type": "text", "nullable": False},
    {"name": "NUMERIC_SCALE", "type": "fixed", "scale": 0, "nullable": True},
]
DESCRIPTION: list[list[str | None]] = [
    ["ORDERS", name, kind, scale]
    for name, kind, scale in (
        ("ID", "NUMBER", "0"),
        ("AMOUNT", "NUMBER", "2"),
        ("RATIO", "FLOAT", None),
        ("NOTE", "TEXT", None),
        ("OK", "BOOLEAN", None),
        ("PLACED_ON", "DATE", None),
        ("AT", "TIME", "9"),
        ("PLACED", "TIMESTAMP_NTZ", "9"),
        ("PLACED_TZ", "TIMESTAMP_TZ", "9"),
        ("RAW", "BINARY", None),
        ("META", "VARIANT", None),
    )
]
ANSWERS: dict[str, Answer] = {
    DESCRIBE: Answer(DESCRIBE_TYPE, DESCRIPTION),
    READ: Answer(ORDERS_TYPE, ORDERS),
    MANY: Answer(rows=_xs(6), partition=2),
    POLLED: Answer(rows=_xs(2), running=2),
    SLOW: Answer(slow=True),
    LIMITED: Answer(rows=_xs(1)),
    MISSING: Answer(error=(422, "002003", "42S02")),
    SYNTAX: Answer(error=(422, "001003", "42000")),
    SECRET: Answer(error=(422, "003001", "42501")),
}


@dataclass(frozen=True)
class Key:
    """The service user's RSA key: the PEM the customer gives, and its fingerprint computed
    here apart from the connector's."""

    key: rsa.RSAPrivateKey

    @property
    def pem(self) -> str:
        return self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    @property
    def body(self) -> list[str]:
        """The key's base64 lines, without the PEM armour."""
        return self.pem.splitlines()[1:-1]

    @property
    def fingerprint(self) -> str:
        der = self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return base64.b64encode(hashlib.sha256(der).digest()).decode()


def make_key(bits: int = 2048) -> Key:
    return Key(rsa.generate_private_key(public_exponent=65537, key_size=bits))


@dataclass
class Statement:
    answer: Answer
    rows: list[list[str | None]]
    running: int
    cancelled: bool = False


@dataclass
class Seen:
    """What the app saw: every bearer token, the claims of each it accepted, each statement
    body, each status or partition GET, each cancel and each statement made."""

    bearers: list[str] = field(default_factory=list[str])
    claims: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    bodies: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    gets: list[tuple[str, dict[str, str]]] = field(default_factory=list[tuple[str, dict[str, str]]])
    cancels: list[str] = field(default_factory=list[str])
    statements: dict[str, Statement] = field(default_factory=dict[str, Statement])
    limited: bool = False


def _error(status: int, code: str | None, sqlstate: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"message": f"SQL compilation error: {BODY_MARK}"}
    if code is not None:
        body["code"] = code
    if sqlstate is not None:
        body["sqlState"] = sqlstate
    body["statementHandle"] = str(uuid.uuid4())
    return JSONResponse(body, status_code=status)


def _running(handle: str) -> JSONResponse:
    return JSONResponse(
        {
            "code": "333334",
            "message": "Asynchronous execution in progress.",
            "statementHandle": handle,
            "statementStatusUrl": f"/api/v2/statements/{handle}",
        },
        status_code=202,
    )


def _chunks(statement: Statement) -> list[list[list[str | None]]]:
    size = statement.answer.partition or max(1, len(statement.rows))
    return [statement.rows[at : at + size] for at in range(0, len(statement.rows), size)] or [[]]


def _result(handle: str, statement: Statement, partition: int) -> dict[str, Any]:
    chunks = _chunks(statement)
    if partition:
        return {"data": chunks[partition]}
    return {
        "resultSetMetaData": {
            "numRows": len(statement.rows),
            "format": "jsonv2",
            "partitionInfo": [{"rowCount": len(c), "uncompressedSize": 64} for c in chunks],
            "rowType": statement.answer.row_type,
        },
        "data": chunks[0],
        "code": "090001",
        "statementStatusUrl": f"/api/v2/statements/{handle}",
        "requestId": str(uuid.uuid4()),
        "sqlState": "00000",
        "statementHandle": handle,
        "message": "Statement executed successfully.",
        "createdOn": 1,
    }


def app(key: Key, seen: Seen) -> FastAPI:  # noqa: C901  (one fake API)
    api = FastAPI()
    public = key.key.public_key()
    issuer = f"{SUB}.SHA256:{key.fingerprint}"

    def verified(request: Request) -> bool:
        if request.headers.get("x-snowflake-authorization-token-type") != "KEYPAIR_JWT":
            return False
        bearer = request.headers.get("authorization", "")
        if not bearer.startswith("Bearer "):
            return False
        token = bearer.removeprefix("Bearer ")
        seen.bearers.append(token)
        try:
            found = jwt.decode(
                token,
                public,
                algorithms=["RS256"],
                issuer=issuer,
                options={"require": ["iss", "sub", "iat", "exp"]},
            )
        except jwt.PyJWTError:
            return False
        if found["sub"] != SUB or found["exp"] - found["iat"] > 3600:
            return False
        seen.claims.append(found)
        return True

    @api.post("/api/v2/statements")
    async def submit(request: Request) -> Any:
        if not verified(request):
            return _error(401, "390144")
        body = await request.json()
        seen.bodies.append(body)
        text = body["statement"]
        answer = ANSWERS[READ] if text == PARAMS else ANSWERS.get(text)
        if answer is None:
            return _error(422, "001003", "42000")
        if answer.error is not None:
            return _error(*answer.error)
        if text == LIMITED and not seen.limited:
            seen.limited = True
            return _error(429, None)
        rows = answer.rows
        if text == PARAMS:
            rows = [r for r in rows if r[0] == body["bindings"]["1"]["value"]]
        handle = str(uuid.uuid4())
        statement = Statement(answer, rows, answer.running)
        seen.statements[handle] = statement
        if answer.slow or statement.running:
            statement.running = max(0, statement.running - 1)
            return _running(handle)
        return _result(handle, statement, 0)

    @api.get("/api/v2/statements/{handle}")
    async def status(handle: str, request: Request) -> Any:
        if not verified(request):
            return _error(401, "390144")
        statement = seen.statements.get(handle)
        if statement is None:
            return _error(404, "000709", "02000")
        params = dict(request.query_params)
        seen.gets.append((handle, params))
        if statement.cancelled:
            return _error(422, "000604", "57014")
        if statement.answer.slow or statement.running:
            statement.running = max(0, statement.running - 1)
            return _running(handle)
        return _result(handle, statement, int(params.get("partition", "0")))

    @api.post("/api/v2/statements/{handle}/cancel")
    async def cancel(handle: str, request: Request) -> Any:
        if not verified(request):
            return _error(401, "390144")
        statement = seen.statements.get(handle)
        if statement is None:
            return _error(404, "000709", "02000")
        statement.cancelled = True
        seen.cancels.append(handle)
        return {"code": "000604", "sqlState": "57014", "statementHandle": handle}

    return api


ADDRESS: dict[str, Any] = {
    "account": ACCOUNT,
    "user": USER,
    "database": DATABASE,
    "schema": SCHEMA,
    "warehouse": WAREHOUSE,
    "role": ROLE,
}


@dataclass(frozen=True)
class Source:
    """The running app, its PKI, the user's key and ways in."""

    pki: Pki
    base_url: str
    key: Key
    seen: Seen

    def target(self, **update: Any) -> SnowflakeTarget:
        return SnowflakeTarget.model_validate(ADDRESS | {"private_key": self.key.pem} | update)

    def connector(self, base_url: str | None = None, **update: Any) -> SnowflakeConnector:
        return SnowflakeConnector(
            self.target(**update),
            base_url=base_url or self.base_url,
            transport=httpx2.AsyncHTTPTransport(verify=tls_context(self.pki.ca), trust_env=False),
            connect_seconds=2,
            poll_seconds=0.05,
        )

    def secrets(self) -> list[str]:
        """Everything of the credential that must never show: the PEM, each line of it, its
        fingerprint and every JWT sent."""
        return [self.key.pem, *self.key.body, self.key.fingerprint, *self.seen.bearers]


@pytest.fixture(scope="module")
def pki() -> Pki:
    return make_pki()


@pytest.fixture(scope="module")
def key() -> Key:
    return make_key()


@pytest.fixture
async def source(pki: Pki, key: Key, tmp_path: Path) -> AsyncIterator[Source]:
    cert, server_key = tmp_path / "server.crt", tmp_path / "server.key"
    cert.write_text(pki.cert)
    server_key.write_text(pki.key)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    seen = Seen()
    config = uvicorn.Config(
        app(key, seen),
        ssl_certfile=str(cert),
        ssl_keyfile=str(server_key),
        log_config=None,
        lifespan="off",
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        yield Source(pki, f"https://localhost:{port}", key, seen)
    finally:
        server.should_exit = True
        await task
        sock.close()


def subject(s: Source) -> Subject:
    """The Snowflake connector as the connector suite sees it (GA-5)."""
    return Subject(
        connector=s.connector(),
        unreachable=s.connector(base_url="https://localhost:9"),
        credential=s.key.pem,
        secrets=(s.target(),),
        read=READ,
        read_columns={
            "ID": "integer",
            "AMOUNT": "decimal",
            "RATIO": "float",
            "NOTE": "string",
            "OK": "boolean",
            "PLACED_ON": "date",
            "AT": "time",
            "PLACED": "timestamp",
            "PLACED_TZ": "timestamp",
            "RAW": "bytes",
            "META": "json",
        },
        first_row=FIRST_ROW,
        writes=(
            "INSERT INTO orders VALUES (1)",
            "DELETE FROM orders",
            "CREATE TABLE t (a int)",
            "SELECT * FROM TABLE(RESULT_SCAN('x'))",
            "SELECT * FROM other_db.public.t",
            "SELECT * FROM snowflake.account_usage.query_history",
        ),
        bad=MISSING,
        slow=SLOW,
        params=(PARAMS, (1,), FIRST_ROW),
        table="ORDERS",
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_the_snowflake_connector_conforms(
    source: Source, check: Callable[[Subject], Awaitable[None]]
) -> None:
    await conform(check, subject(source))


async def test_a_description_types_the_schemas_columns_as_a_read_types_them(
    source: Source,
) -> None:
    (table,) = await source.connector().describe(schemas=["OTHER"], timeout_ms=4_000)
    columns, _ = await read(source.connector(), ask(READ))
    assert table.name == "ORDERS"
    assert [(c.name, c.type) for c in table.columns] == [(c.name, c.type) for c in columns]
    assert [c.db_type for c in table.columns][:3] == ["fixed", "fixed", "real"]
    body = source.seen.bodies[0]
    assert "information_schema.columns WHERE table_schema = CURRENT_SCHEMA()" in body["statement"]
    assert (body["database"], body["schema"], body["parameters"]["query_tag"]) == (
        DATABASE,
        SCHEMA,
        "ssc:describe",
    )
    assert source.seen.claims, "the fake verified the JWT"


async def test_a_description_stops_at_500_tables_of_500_columns(
    source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    wide: list[list[str | None]] = [["T000", f"C{i}", "NUMBER", "0"] for i in range(1, 502)]
    narrow: list[list[str | None]] = [[f"T{i:03}", "X", "TEXT", None] for i in range(1, 503)]
    rows = [*wide, *narrow]
    monkeypatch.setitem(ANSWERS, DESCRIBE, Answer(DESCRIBE_TYPE, rows, partition=400))
    tables = await source.connector().describe(schemas=None, timeout_ms=10_000)
    assert len(tables) == 500
    assert (tables[0].name, len(tables[0].columns)) == ("T000", 500)
    assert tables[-1].name == "T499"


async def test_a_description_past_its_timeout_cancels_its_statement(
    source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(ANSWERS, DESCRIBE, Answer(slow=True))
    with pytest.raises(TimeoutError):
        await source.connector().describe(schemas=None, timeout_ms=1_000)
    assert source.seen.cancels == list(source.seen.statements)


async def test_a_read_types_every_column(source: Source) -> None:
    columns, rows = await read(source.connector(), ask(READ))
    assert [(c.name, c.type, c.db_type) for c in columns] == [
        ("ID", "integer", "fixed"),
        ("AMOUNT", "decimal", "fixed"),
        ("RATIO", "float", "real"),
        ("NOTE", "string", "text"),
        ("OK", "boolean", "boolean"),
        ("PLACED_ON", "date", "date"),
        ("AT", "time", "time"),
        ("PLACED", "timestamp", "timestamp_ntz"),
        ("PLACED_TZ", "timestamp", "timestamp_tz"),
        ("RAW", "bytes", "binary"),
        ("META", "json", "variant"),
    ]
    assert list(rows[0]) == [
        1,
        Decimal("1.25"),
        0.5,
        "note 1",
        False,
        date(2024, 1, 2),
        clock(3, 4, 5, 123_456),
        datetime(2024, 1, 2, 3, 4, 5, 123_456),  # noqa: DTZ001  (TIMESTAMP_NTZ is naive)
        datetime(2024, 1, 2, 4, 4, 5, 123_456, tzinfo=timezone(timedelta(hours=1))),
        b"\x00\xff\x01",
        {"a": [1, None]},
    ]
    assert rows[1][4] is True
    assert rows[2][3] is None, "a null stays None"
    assert jsonable(list(rows[0])) == FIRST_ROW


async def test_the_request_carries_the_address_the_bindings_the_timeout_and_the_tag(
    source: Source,
) -> None:
    await read(source.connector(), ask(PARAMS, 2, max_rows=7, timeout_ms=4_001, tag="ssc:a:p:R-9"))
    (body,) = source.seen.bodies
    assert body == {
        "statement": PARAMS,
        "timeout": 5,
        "database": DATABASE,
        "schema": SCHEMA,
        "warehouse": WAREHOUSE,
        "role": ROLE,
        "bindings": {"1": {"type": "FIXED", "value": "2"}},
        "parameters": {"query_tag": "ssc:a:p:R-9", "MULTI_STATEMENT_COUNT": "1"},
        "resultSetMetaData": {"format": "jsonv2"},
    }
    await read(source.connector(role=None), ask(READ, timeout_ms=999))
    assert "role" not in source.seen.bodies[1], "the user's default role"
    assert source.seen.bodies[1]["timeout"] == 1
    assert source.seen.bodies[1]["bindings"] == {}


async def test_the_jwt_names_the_user_and_the_keys_fingerprint(source: Source) -> None:
    before = int(time.time())
    await read(source.connector(), ask(POLLED))
    assert len(source.seen.claims) == 3, "the statement and both polls sent a JWT it verified"
    assert len(set(source.seen.bearers)) == 1, "one JWT a read"
    found = source.seen.claims[-1]
    assert found["iss"] == f"MYORG-ACCT_1.SSC_DATAGW.SHA256:{source.key.fingerprint}"
    assert found["sub"] == "MYORG-ACCT_1.SSC_DATAGW"
    assert found["exp"] - found["iat"] == 59 * 60 == TOKEN_SECONDS
    assert before - 5 <= found["iat"] <= int(time.time()) + 5
    assert set(found) == {"iss", "sub", "iat", "exp"}
    assert jwt.get_unverified_header(source.seen.bearers[0])["alg"] == "RS256"


PUBLIC_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuLwCgs5hnsJ/j0hWQgA1
9c6y9t4lBvs6ZZlc8iO9jd19UETVYl2x2vSn8HqwC64NxjyPvEgM0mWJOvF55OB4
wmMqpwmSPA789c+0yG3TyeprDYZZ18+Z+LtB6wPjjQP8KD+h+EBAUrAW39C0L2aq
mmVyN67GfTPRIuNPeABITHmIJocuUknSK1fsR6bb346u26MzCDyoLGncrkSXKyQL
iHSLcLJAspKNv9RzcaGA0bXJv8va6UeTXdltIAqD39ce3Q9FW7VSF+oUxg1+I4np
Bf56NWSGvEn9hCOlnZR9aJtHgFWFZNcJ+hAwgA74zT/8vtiKFTuhDOtOP8v1VNHH
gwIDAQAB
-----END PUBLIC KEY-----
"""
"""A public key made once for this vector; its private half was never kept."""
PUBLIC_FP = "sxRSM6Sdn7ZYIpiv3SkR85G5xjIB6wie5v7NhWHdWgk="
"""Computed once with ``cryptography`` (the DER SubjectPublicKeyInfo, SHA-256, base64): what
Snowflake's documented ``openssl rsa -pubin -in rsa_key.pub -outform DER | openssl dgst -sha256
-binary | openssl enc -base64`` gives, and ``DESC USER`` shows as ``RSA_PUBLIC_KEY_FP`` after
``SHA256:``."""


def test_the_fingerprint_and_claims_match_a_fixed_vector() -> None:
    public = serialization.load_pem_public_key(PUBLIC_PEM)
    assert isinstance(public, rsa.RSAPublicKey)
    assert fingerprint(public) == PUBLIC_FP
    assert claims("xy12345.us-east-1.aws", "Reader_1", public, 1_700_000_000) == {
        "iss": f"XY12345.READER_1.SHA256:{PUBLIC_FP}",
        "sub": "XY12345.READER_1",
        "iat": 1_700_000_000,
        "exp": 1_700_003_540,
    }
    assert claims("myorg-acct", "r", public, 0)["sub"] == "MYORG-ACCT.R"


def test_the_account_names_the_jwt_and_the_host_by_the_documented_rules() -> None:
    assert jwt_account("xy12345") == "XY12345"
    assert jwt_account("xy12345.us-east-1") == "XY12345"
    assert jwt_account("xy12345.eu-west-2.aws") == "XY12345"
    assert jwt_account("myorg-my_acct") == "MYORG-MY_ACCT"
    assert account_url("myorg-my_acct") == "https://myorg-my-acct.snowflakecomputing.com"
    assert account_url("xy12345.us-east-1") == "https://xy12345.us-east-1.snowflakecomputing.com"


async def test_each_read_signs_one_jwt_by_the_clock(key: Key) -> None:
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json=_ok(_xs(1)))

    connector = _mock(key, answer, clock=lambda: 1_700_000_000.9)
    await read(connector, ask("SELECT 1"))
    token = sent[0].headers["authorization"].removeprefix("Bearer ")
    found = jwt.decode(
        token, key.key.public_key(), algorithms=["RS256"], options={"verify_exp": False}
    )
    assert (found["iat"], found["exp"]) == (1_700_000_000, 1_700_003_540)
    assert found["iss"] == f"MYORG-ACCT_1.SSC_DATAGW.SHA256:{key.fingerprint}"


async def test_a_running_statement_is_polled_until_it_answers(
    source: Source, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _, rows = await read(source.connector(), ask(POLLED, max_rows=10))
    assert rows == [["x1"], ["x2"]]
    (handle,) = source.seen.statements
    assert source.seen.gets == [(handle, {})] * 2
    assert "snowflake read: partitions=1 polls=2 bytes=" in caplog.text


async def test_the_poll_waits_half_a_second_by_default(source: Source) -> None:
    connector = SnowflakeConnector(
        source.target(),
        base_url=source.base_url,
        transport=httpx2.AsyncHTTPTransport(verify=tls_context(source.pki.ca), trust_env=False),
    )
    started = time.monotonic()
    await read(connector, ask(POLLED))
    assert POLL_SECONDS == 0.5
    assert time.monotonic() - started >= 2 * POLL_SECONDS


async def test_more_partitions_follow_until_one_row_past_the_cap(source: Source) -> None:
    _, rows = await read(source.connector(), ask(MANY, max_rows=1000))
    assert rows == _xs(6)
    assert [g[1] for g in source.seen.gets] == [{"partition": "1"}, {"partition": "2"}]
    source.seen.gets.clear()
    _, rows = await read(source.connector(), ask(MANY, max_rows=2))
    assert rows == _xs(3), "max_rows plus one, no more"
    assert [g[1] for g in source.seen.gets] == [{"partition": "1"}]
    source.seen.gets.clear()
    _, rows = await read(source.connector(), ask(MANY, max_rows=1))
    assert rows == _xs(2)
    assert source.seen.gets == [], "the first partition held enough"


@pytest.mark.parametrize(
    ("sql", "sqlstate", "message"),
    [
        (MISSING, "42P01", "snowflake refused the read: 002003"),
        (SYNTAX, "42601", "snowflake refused the read: 001003"),
        (SECRET, "42501", "snowflake refused the read: 003001"),
    ],
)
async def test_what_snowflake_refuses_is_a_query_failure_by_its_code(
    source: Source, sql: str, sqlstate: str, message: str
) -> None:
    with pytest.raises(QueryFailedError) as failed:
        await read(source.connector(), ask(sql))
    assert failed.value.sqlstate == sqlstate
    assert str(failed.value) == message


async def test_a_429_is_unavailable_and_the_next_read_answers(source: Source) -> None:
    with pytest.raises(UpstreamUnavailableError, match="^snowflake answered 429: unknown$"):
        await read(source.connector(), ask(LIMITED))
    _, rows = await read(source.connector(), ask(LIMITED))
    assert rows == [["x1"]]


async def test_another_key_or_user_is_28000(source: Source) -> None:
    for update in ({"private_key": make_key().pem}, {"user": "someone_else"}):
        with pytest.raises(QueryFailedError) as refused:
            await read(source.connector(**update), ask(READ))
        assert refused.value.sqlstate == "28000"
        assert str(refused.value) == "the source refused the credential"
    assert source.seen.bodies == []


async def test_a_read_past_its_timeout_cancels_its_statement(
    source: Source, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="^the read passed timeout_ms$"):
        await read(source.connector(), ask(SLOW, timeout_ms=1_500))
    assert time.monotonic() - started < 8
    assert source.seen.cancels == list(source.seen.statements)
    assert len(source.seen.cancels) == 1
    assert "could not cancel" not in caplog.text


async def test_a_cancelled_read_cancels_its_statement(source: Source) -> None:
    task = asyncio.create_task(read(source.connector(), ask(SLOW, timeout_ms=60_000)))
    await asyncio.sleep(1.0)
    assert source.seen.gets, "the read is polling"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(source.seen.cancels) == 1
    assert source.seen.cancels == list(source.seen.statements)


async def test_a_read_that_never_got_a_handle_cancels_nothing(key: Key) -> None:
    sent: list[str] = []

    async def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.url.path)
        await asyncio.sleep(20)
        return httpx2.Response(500)

    with pytest.raises(TimeoutError):
        await read(_mock(key, answer), ask(READ, timeout_ms=300))
    assert sent == ["/api/v2/statements"]


async def test_a_cancel_that_fails_is_logged_by_class_only(
    key: Key, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    cancels: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/cancel"):
            cancels.append(request.url.path)
            return httpx2.Response(503, json={"message": BODY_MARK})
        return httpx2.Response(202, json={"statementHandle": "01b2-c3", "message": BODY_MARK})

    with pytest.raises(TimeoutError):
        await read(_mock(key, answer), ask(READ, timeout_ms=300))
    assert cancels == ["/api/v2/statements/01b2-c3/cancel"]
    assert "could not cancel the statement: UpstreamUnavailableError" in caplog.text
    assert BODY_MARK not in caplog.text


async def test_another_ca_is_refused(source: Source) -> None:
    connector = SnowflakeConnector(
        source.target(),
        base_url=source.base_url,
        transport=httpx2.AsyncHTTPTransport(verify=tls_context(source.pki.other_ca)),
    )
    with pytest.raises(UpstreamUnavailableError, match="^cannot connect: "):
        await read(connector, ask(READ))
    assert source.seen.bearers == [], "no request crossed a refused handshake"


async def test_the_credential_is_absent_from_errors_and_logs(
    source: Source, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _, rows = await read(source.connector(), ask(READ))
    assert rows, "the JWT was sent and accepted"
    failing = [
        (source.connector(), ask(sql))
        for sql in (MISSING, SYNTAX, SECRET, LIMITED, "DELETE FROM orders", "SELECT * FROM @s")
    ]
    failing += [
        (source.connector(private_key=make_key().pem), ask(READ)),
        (source.connector(base_url="https://localhost:9"), ask(READ)),
        (source.connector(), ask(SLOW, timeout_ms=200)),
        (source.connector(), ask(READ, 1)),
        (source.connector(), ask(PARAMS, 10**38)),
    ]
    for connector, query in failing:
        with pytest.raises(
            (QueryFailedError, QueryRefusedError, UpstreamUnavailableError, TimeoutError)
        ) as failed:
            await read(connector, query)
        shown = "".join(traceback.format_exception(failed.value)) + repr(failed.value)
        assert BODY_MARK not in shown, query.sql
        for secret in source.secrets():
            assert secret not in shown, query.sql
    assert len(source.seen.bearers) >= 6
    for secret in [*source.secrets(), BODY_MARK]:
        assert secret not in caplog.text
    assert "snowflake read: partitions=1 polls=0 bytes=" in caplog.text
    assert READ not in caplog.text, "no statement text in a log line"


def test_a_target_hides_its_key(key: Key) -> None:
    target = SnowflakeTarget.model_validate(ADDRESS | {"private_key": key.pem})
    connector = SnowflakeConnector(target)
    for shown in (repr(target), str(target), repr(connector), str(connector)):
        for secret in (key.pem, key.fingerprint, *key.body):
            assert secret not in shown


def _other_keys(key: Key) -> list[str]:
    """A PKCS#1 PEM of the same key, an encrypted PKCS#8, a 1024-bit RSA key, an EC key and
    text that is no key."""
    pkcs1 = key.key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    encrypted = key.key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"a passphrase"),
    ).decode()
    small = make_key(1024).pem
    elliptic = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    broken = key.pem.replace(key.body[3], key.body[3][::-1])
    return [pkcs1, encrypted, small, elliptic, broken, "not a key " + key.body[0]]


def test_a_private_key_that_is_not_one_is_refused_without_quoting_it(key: Key) -> None:
    for private_key in _other_keys(key):
        with pytest.raises(ValidationError) as refused:
            SnowflakeTarget.model_validate(ADDRESS | {"private_key": private_key})
        shown = str(refused.value) + "".join(traceback.format_exception(refused.value))
        assert NOT_A_KEY in shown
        assert "private_key" in shown, "the error names the field"
        for line in private_key.splitlines()[1:-1] or [private_key]:
            assert line not in shown
        for secret in key.body:
            assert secret not in shown


def test_the_target_model_takes_the_control_planes_address(key: Key) -> None:
    target = SnowflakeTarget.model_validate(
        {
            "account": "xy12345.us-east-1",
            "user": "R",
            "database": "D",
            "warehouse": "W",
            "private_key": key.pem,
        }
    )
    assert (target.kind, target.schema_name, target.role) == ("snowflake", "PUBLIC", None)
    by_alias = SnowflakeTarget.model_validate(ADDRESS | {"private_key": key.pem})
    assert by_alias.schema_name == SCHEMA
    assert by_alias.model_dump(by_alias=True)["schema"] == SCHEMA
    named = {k: v for k, v in ADDRESS.items() if k != "schema"}
    assert (
        SnowflakeTarget.model_validate(named | {"schema_name": "S1", "private_key": key.pem})
    ).schema_name == "S1"


@pytest.mark.parametrize(
    "update",
    [
        {"account": ""},
        {"account": "a b"},
        {"account": "a/b"},
        {"account": "a" * 129},
        {"user": "a@b"},
        {"user": ""},
        {"database": 'a"b'},
        {"schema": "a b"},
        {"warehouse": "a;b"},
        {"warehouse": None},
        {"role": "a b"},
        {"kind": "bigquery"},
        {"extra": 1},
    ],
)
def test_the_target_model_refuses_a_bad_address(key: Key, update: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SnowflakeTarget.model_validate(ADDRESS | {"private_key": key.pem} | update)


def _mock(
    key: Key,
    answer: Callable[[httpx2.Request], httpx2.Response | Awaitable[httpx2.Response]],
    **seams: Any,
) -> SnowflakeConnector:
    target = SnowflakeTarget.model_validate(ADDRESS | {"private_key": key.pem})
    return SnowflakeConnector(
        target,
        transport=httpx2.MockTransport(answer),  # pyright: ignore[reportArgumentType]
        poll_seconds=0.01,
        **seams,
    )


def _ok(data: list[Any] | None = None, *, partitions: int = 1, **extra: Any) -> dict[str, Any]:
    meta = {"rowType": X_TYPE, "partitionInfo": [{"rowCount": 1}] * partitions}
    body = {"resultSetMetaData": meta, "statementHandle": "01b2-c3", "data": data or []}
    return body | extra


async def test_the_request_is_one_post_to_the_accounts_host(key: Key) -> None:
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json=_ok(_xs(1)))

    _, rows = await read(_mock(key, answer), ask("SELECT 'a' AS x"))
    assert rows == [["x1"]]
    (request,) = sent
    assert request.method == "POST"
    assert str(request.url) == "https://myorg-acct-1.snowflakecomputing.com/api/v2/statements"
    assert request.headers["authorization"].startswith("Bearer ey")
    assert request.headers["x-snowflake-authorization-token-type"] == "KEYPAIR_JWT"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == "ssc-datagw"


async def test_a_parameter_count_that_differs_is_07001_before_the_request(key: Key) -> None:
    sent: list[httpx2.Request] = []
    connector = _mock(key, sent.append)  # pyright: ignore[reportArgumentType]
    for sql, params in ((PARAMS, ()), (PARAMS, (1, 2)), ("SELECT '?' AS x -- ?", (1,))):
        with pytest.raises(QueryFailedError) as failed:
            await read(connector, ask(sql, *params))
        assert failed.value.sqlstate == "07001"
    with pytest.raises(QueryFailedError) as wide:
        await read(connector, ask(PARAMS, -(10**38)))
    assert wide.value.sqlstate == "22003"
    with pytest.raises(QueryRefusedError):
        await read(connector, ask("SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))"))
    assert sent == []


@pytest.mark.parametrize(
    ("status", "code", "sqlstate_in", "error", "sqlstate"),
    [
        (422, "002003", "42S02", QueryFailedError, "42P01"),
        (422, "001003", "42000", QueryFailedError, "42601"),
        (422, "003001", "42501", QueryFailedError, "42501"),
        (408, "000630", "57014", TimeoutError, None),
        (422, "000604", "57014", QueryFailedError, "57014"),
        (401, "390100", "08004", QueryFailedError, "28000"),
        (401, "390144", None, QueryFailedError, "28000"),
        (403, "390142", None, QueryFailedError, "28000"),
        (401, None, None, QueryFailedError, "28000"),
        (422, "100038", "22018", QueryFailedError, "22018"),
        (400, "000000", "0A000", QueryFailedError, "0A000"),
        (422, "100038", None, QueryFailedError, None),
        (422, "100038", "bad!!", QueryFailedError, None),
        (422, "100038", "220180", QueryFailedError, None),
        (404, None, None, QueryFailedError, None),
        (302, None, None, QueryFailedError, None),
        (429, None, None, UpstreamUnavailableError, None),
        (500, "000000", "XX000", UpstreamUnavailableError, None),
        (503, None, None, UpstreamUnavailableError, None),
    ],
)
async def test_each_status_and_code_maps_to_its_error(  # noqa: PLR0913  (one row of the table)
    key: Key,
    status: int,
    code: str | None,
    sqlstate_in: str | None,
    error: type[Exception],
    sqlstate: str | None,
) -> None:
    body: dict[str, Any] = {"message": BODY_MARK, "statementHandle": "01b2-c3"}
    body |= {"code": code} if code else {}
    body |= {"sqlState": sqlstate_in} if sqlstate_in else {}
    connector = _mock(key, lambda _: httpx2.Response(status, json=body))
    with pytest.raises(error) as failed:
        await read(connector, ask("SELECT 1"))
    assert getattr(failed.value, "sqlstate", None) == sqlstate
    assert BODY_MARK not in str(failed.value)
    if isinstance(failed.value, QueryFailedError) and sqlstate != "28000":
        assert str(failed.value).endswith(code or "unknown")


def test_a_cancelled_statement_is_a_timeout_only_when_the_connector_cancelled_it() -> None:
    body = {"code": "000604", "sqlState": "57014"}
    assert isinstance(failure(422, body, cancelled=True), TimeoutError)
    ours = failure(422, body)
    assert isinstance(ours, QueryFailedError)
    assert ours.sqlstate == "57014"


async def test_a_code_that_is_not_six_digits_is_not_quoted(key: Key) -> None:
    for code in ("x y <script>", 7, "1234567", BODY_MARK):
        body = {"code": code, "message": BODY_MARK}
        connector = _mock(key, lambda _, b=body: httpx2.Response(422, json=b))
        with pytest.raises(QueryFailedError, match="^snowflake answered 422: unknown$"):
            await read(connector, ask("SELECT 1"))
    connector = _mock(key, lambda _: httpx2.Response(502, content=b"<html>"))
    with pytest.raises(UpstreamUnavailableError, match="^snowflake answered 502: unknown$"):
        await read(connector, ask("SELECT 1"))


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b"{}",
        b'{"data": [], "statementHandle": "h"}',
        b'{"resultSetMetaData": [], "data": [], "statementHandle": "h"}',
        b'{"resultSetMetaData": {}, "data": [], "statementHandle": "h"}',
        b'{"resultSetMetaData": {"rowType": {}}, "data": [], "statementHandle": "h"}',
        b'{"resultSetMetaData": {"rowType": [{"name": "x"}]}, "data": [], "statementHandle": "h"}',
        b'{"resultSetMetaData": {"rowType": [{"name": "x", "type": "fixed", "scale": "2"}]},'
        b' "data": [], "statementHandle": "h"}',
        b'{"resultSetMetaData": {"rowType": [{"name": "x", "type": "text"}]},'
        b' "data": [], "statementHandle": "../x"}',
        b'{"resultSetMetaData": {"rowType": [{"name": "x", "type": "text"}]}, "data": []}',
        b'{"resultSetMetaData": {"rowType": [{"name": "x", "type": "text"}]},'
        b' "statementHandle": "h"}',
        b'{"resultSetMetaData": {"rowType": [{"name": "x", "type": "text"}], "partitionInfo": {}},'
        b' "data": [], "statementHandle": "h"}',
        b'{"resultSetMetaData": {"rowType": [{"name": "x", "type": "text"}]},'
        b' "data": {}, "statementHandle": "h"}',
        b'{"resultSetMetaData": {"rowType": [{"name": "x", "type": "text"}]},'
        b' "data": [[]], "statementHandle": "h"}',
        b'{"resultSetMetaData": {"rowType": [{"name": "x", "type": "text"}]},'
        b' "data": [["a", "b"]], "statementHandle": "h"}',
        b'{"resultSetMetaData": {"rowType": [{"name": "x", "type": "text"}]},'
        b' "data": [[1]], "statementHandle": "h"}',
        b'{"resultSetMetaData": {"rowType": [{"name": "x", "type": "fixed", "scale": 0}]},'
        b' "data": [["1.5"]], "statementHandle": "h"}',
    ],
)
async def test_a_body_that_is_not_a_snowflake_result_is_22p02(key: Key, body: bytes) -> None:
    connector = _mock(key, lambda _: httpx2.Response(200, content=body))
    with pytest.raises(QueryFailedError) as odd:
        await read(connector, ask("SELECT 1"))
    assert odd.value.sqlstate == "22P02"
    assert str(odd.value) == "the body is not a Snowflake result"


@pytest.mark.parametrize(
    "running",
    [{}, {"statementHandle": "../x"}, {"statementHandle": 7}, []],
)
async def test_a_202_without_a_handle_is_22p02(key: Key, running: object) -> None:
    connector = _mock(key, lambda _: httpx2.Response(202, json=running))
    with pytest.raises(QueryFailedError) as odd:
        await read(connector, ask("SELECT 1"))
    assert odd.value.sqlstate == "22P02"


async def test_a_partition_without_data_is_22p02(key: Key) -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(200, json=_ok(_xs(1), partitions=2))
        return httpx2.Response(200, json={"rows": []})

    with pytest.raises(QueryFailedError) as odd:
        await read(_mock(key, answer), ask("SELECT 1"))
    assert odd.value.sqlstate == "22P02"


def test_bindings_are_typed_by_value() -> None:
    values: list[object] = [
        True,
        False,
        7,
        10**38 - 1,
        -(10**38) + 1,
        1.5,
        "s",
        None,
        datetime(2024, 1, 2, 3, 4, 5, 6),  # noqa: DTZ001  (TIMESTAMP_NTZ is naive)
        date(2024, 2, 29),
        clock(23, 59, 58, 500_000),
        b"\x00\xff",
        bytearray(b"\x10"),
    ]
    assert bindings(values) == {
        "1": {"type": "BOOLEAN", "value": "true"},
        "2": {"type": "BOOLEAN", "value": "false"},
        "3": {"type": "FIXED", "value": "7"},
        "4": {"type": "FIXED", "value": "9" * 38},
        "5": {"type": "FIXED", "value": "-" + "9" * 38},
        "6": {"type": "REAL", "value": "1.5"},
        "7": {"type": "TEXT", "value": "s"},
        "8": {"type": "TEXT", "value": None},
        "9": {"type": "TIMESTAMP_NTZ", "value": "2024-01-02T03:04:05.000006"},
        "10": {"type": "DATE", "value": "2024-02-29"},
        "11": {"type": "TIME", "value": "23:59:58.500000"},
        "12": {"type": "BINARY", "value": "00ff"},
        "13": {"type": "BINARY", "value": "10"},
    }
    assert bindings([float("nan"), float("inf"), float("-inf")]) == {
        "1": {"type": "REAL", "value": "NaN"},
        "2": {"type": "REAL", "value": "inf"},
        "3": {"type": "REAL", "value": "-inf"},
    }
    assert bindings([]) == {}
    for wide in (10**38, -(10**38)):
        with pytest.raises(QueryFailedError) as failed:
            bindings([wide])
        assert failed.value.sqlstate == "22003"
    with pytest.raises(QueryFailedError) as odd:
        bindings([object()])
    assert odd.value.sqlstate == "22023"


def test_placeholders_are_counted_as_snowflake_reads_them() -> None:
    assert placeholders("SELECT ?, ? FROM t WHERE a = ?") == 3
    assert placeholders("SELECT '?', \"?\", $$?$$ -- ?\n/* ? */") == 0


def _type(kind: str, scale: int = 0) -> RowType:
    return RowType("x", kind, scale)


@pytest.mark.parametrize(
    ("column", "text", "expected"),
    [
        (_type("fixed"), "-99999999999999999999999999999999999999", -(10**38) + 1),
        (_type("fixed", 3), "-0.001", Decimal("-0.001")),
        (_type("fixed", 2), "12", Decimal("12")),
        (_type("real"), "1e-3", 0.001),
        (_type("real"), "inf", float("inf")),
        (_type("real"), "-inf", float("-inf")),
        (_type("text"), "", ""),
        (_type("boolean"), "true", True),
        (_type("date"), "0", date(1970, 1, 1)),
        (_type("date"), "-1", date(1969, 12, 31)),
        (_type("time"), "0", clock(0)),
        (_type("time"), "86399.999999999", clock(23, 59, 59, 999_999)),
        (_type("timestamp_ntz"), "0", datetime(1970, 1, 1)),  # noqa: DTZ001
        (_type("timestamp_ntz"), "-0.5", datetime(1969, 12, 31, 23, 59, 59, 500_000)),  # noqa: DTZ001
        (_type("timestamp_ntz"), "1.0000009", datetime(1970, 1, 1, 0, 0, 1)),  # noqa: DTZ001
        (_type("timestamp_ltz"), f"{PLACED}.5", datetime(2024, 1, 2, 3, 4, 5, 500_000, tzinfo=UTC)),
        (_type("timestamp_tz"), "0 1440", datetime(1970, 1, 1, tzinfo=UTC)),
        (
            _type("timestamp_tz"),
            "0 1110",
            datetime(1969, 12, 31, 18, 30, tzinfo=timezone(timedelta(hours=-5, minutes=-30))),
        ),
        (
            _type("timestamp_tz"),
            "0 2879",
            datetime(1970, 1, 1, 23, 59, tzinfo=timezone(timedelta(minutes=1439))),
        ),
        (_type("binary"), "", b""),
        (_type("binary"), "DEADbeef", b"\xde\xad\xbe\xef"),
        (_type("variant"), "null", None),
        (_type("variant"), '"s"', "s"),
        (_type("object"), '{"k": {"n": 1.5}}', {"k": {"n": 1.5}}),
        (_type("array"), "[1, null]", [1, None]),
        (_type("geography"), "POINT(1 2)", "POINT(1 2)"),
    ],
)
def test_each_value_converts_by_its_columns_type(
    column: RowType, text: str, expected: object
) -> None:
    assert scalar(column, text) == expected


def test_a_timestamp_tz_keeps_its_offset() -> None:
    found = scalar(_type("timestamp_tz"), f"{PLACED}.123456789 1500")
    assert isinstance(found, datetime)
    assert found.utcoffset() == timedelta(hours=1)
    assert jsonable(found) == "2024-01-02T04:04:05.123456+01:00"
    ltz = scalar(_type("timestamp_ltz"), str(PLACED))
    assert jsonable(ltz) == "2024-01-02T03:04:05+00:00"


@pytest.mark.parametrize(
    ("column", "text"),
    [
        (_type("fixed"), "1.0"),
        (_type("fixed"), ""),
        (_type("fixed", 2), "NaN"),
        (_type("fixed", 2), "x"),
        (_type("real"), "one"),
        (_type("boolean"), "TRUE"),
        (_type("boolean"), "1"),
        (_type("date"), "1.5"),
        (_type("date"), "99999999"),
        (_type("time"), "86400"),
        (_type("time"), "-1"),
        (_type("timestamp_ntz"), "NaN"),
        (_type("timestamp_ntz"), "1" * 30),
        (_type("timestamp_ltz"), "soon"),
        (_type("timestamp_tz"), "0"),
        (_type("timestamp_tz"), "0 x"),
        (_type("timestamp_tz"), "0 0"),
        (_type("timestamp_tz"), "0 2880"),
        (_type("binary"), "zz"),
        (_type("variant"), "{"),
    ],
)
def test_a_value_that_does_not_convert_is_22p02(column: RowType, text: str) -> None:
    with pytest.raises(QueryFailedError) as odd:
        row([column], [text])
    assert odd.value.sqlstate == "22P02"


def test_db_type_is_snowflakes_name_in_lower_case() -> None:
    found = row_types(
        [
            {"name": "a", "type": "FIXED", "scale": 0},
            {"name": "b", "type": "fixed", "scale": 4},
            {"name": "c", "type": "fixed", "scale": None},
            {"name": "d", "type": "TIMESTAMP_LTZ"},
            {"name": "e", "type": "geography"},
            {"name": "f", "type": "vector"},
        ]
    )
    assert [(c.type, c.portable) for c in found] == [
        ("fixed", "integer"),
        ("fixed", "decimal"),
        ("fixed", "integer"),
        ("timestamp_ltz", "timestamp"),
        ("geography", "string"),
        ("vector", "string"),
    ]
    assert row(found, [None] * 6) == [None] * 6
