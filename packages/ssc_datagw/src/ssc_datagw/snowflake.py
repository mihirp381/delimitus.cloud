"""The Snowflake connector: a key-pair JWT, the SQL API v2, positional bindings (GA-5 B8).

A connection names one account, the database, schema and warehouse a read runs in, and
optionally the role, and holds the private key of a service user ``snowflake_setup.sql`` makes;
the customer registers the public key on the user and grants its role ``SELECT`` only.

Each read runs in this order; the first step that refuses answers:

1. :func:`ssc_datagw.classify.snowflake_refusal` refuses anything that is not one plain read,
   a stage, a session variable, and any table or function in another database or in
   ``SNOWFLAKE``;
2. the ``?`` placeholders sqlglot's Snowflake tokenizer finds must match the parameters, each
   typed by its value (:func:`bindings`);
3. one JWT signed with the user's key (:class:`KeyPair`), then one ``POST
   /api/v2/statements``: the statement, its bindings, the connection's database, schema,
   warehouse and role, a ``timeout`` in seconds, the query's tag as ``query_tag`` and
   ``MULTI_STATEMENT_COUNT`` 1;
4. while Snowflake answers 202 the statement is polled; further partitions follow until
   ``max_rows`` plus one rows.

A description (:meth:`SnowflakeConnector.describe`, GA-5.8) is one fixed statement of ours on
``information_schema.columns`` for the connection's schema, run from step 3 on.

Every request goes through :func:`ssc_datagw.rest.get`: TLS against the system trust store, the
warm-up retry, the 32 MiB cap. The whole read ends at the query's ``timeout_ms``; past it, and
when the gateway cancels the read, the statement is cancelled (5 s at most, from a client of its
own) and Snowflake's own ``timeout`` stops a statement whose handle never came back.

The key, its fingerprint and each JWT are never in a repr, an error or a log line: a key that
does not load is refused with :data:`NOT_A_KEY`, which quotes none of it, and every error names
a status or Snowflake's code, never the body.
"""

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from time import time as wall_clock
from typing import Annotated, Final, Literal, cast

import httpx2
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr
from sqlglot import tokenize
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from ssc_datagw.classify import snowflake_refusal
from ssc_datagw.connectors import (
    DESCRIBE_TAG,
    MAX_COLUMNS,
    MAX_TABLES,
    Column,
    JsonValue,
    Query,
    QueryFailedError,
    QueryRefusedError,
    Table,
    UpstreamUnavailableError,
    grouped,
)
from ssc_datagw.rest import USER_AGENT, get
from ssc_datagw.tls import tls_context
from ssc_datagw.warmup import CONNECT_SECONDS, Warmup

log = logging.getLogger(__name__)

ACCOUNT: Final = r"^[A-Za-z0-9_.-]{1,128}$"
IDENTIFIER: Final = r"^[A-Za-z0-9_.-]{1,128}$"
"""``ssc_contracts.connections.SnowflakeAddress``'s patterns; ``user`` takes the identifier's."""
HOST: Final = "snowflakecomputing.com"
STATEMENTS: Final = "/api/v2/statements"
TOKEN_SECONDS: Final = 59 * 60
"""A key-pair JWT's life: Snowflake refuses one valid for more than an hour."""
MIN_KEY_BITS: Final = 2048
PKCS8: Final = "-----BEGIN PRIVATE KEY-----"
NOT_A_KEY: Final = "the private_key is not an unencrypted PKCS#8 PEM RSA key of at least 2048 bits"
POLL_SECONDS: Final = 0.5
CANCEL_SECONDS: Final = 5.0
FIXED_DIGITS: Final = 38
HANDLE: Final = re.compile(r"[A-Za-z0-9-]{1,64}")
CODE: Final = re.compile(r"[0-9]{6}")
SQLSTATE: Final = re.compile(r"[0-9A-Z]{5}")
NOT_A_RESULT: Final = "the body is not a Snowflake result"
FAILED_CODES: Final = {"002003": "42P01", "001003": "42601", "003001": "42501"}
"""Snowflake's error codes and the SQLSTATE each answers: no such object, a syntax error, not
authorized."""
CREDENTIAL_CODES: Final = frozenset({"390100", "390144", "390142"})
"""The JWT was refused: an expired or invalid token, a key that is not the user's."""
TIMED_OUT: Final = "000630"
CANCELLED: Final = "000604"
PORTABLE: Final = {
    "real": "float",
    "text": "string",
    "boolean": "boolean",
    "date": "date",
    "time": "time",
    "timestamp_ntz": "timestamp",
    "timestamp_ltz": "timestamp",
    "timestamp_tz": "timestamp",
    "binary": "bytes",
    "variant": "json",
    "object": "json",
    "array": "json",
}
"""Portable column types; ``fixed`` is ``integer`` at scale 0 and ``decimal`` otherwise, any
other type ``string``."""
TZ_BASE: Final = 1440
"""A ``TIMESTAMP_TZ`` value's offset is in minutes, plus this."""
_EPOCH: Final = datetime(1970, 1, 1)  # noqa: DTZ001  (TIMESTAMP_NTZ is naive)
_EPOCH_UTC: Final = datetime(1970, 1, 1, tzinfo=UTC)
_EPOCH_DAY: Final = date(1970, 1, 1)
DESCRIBE: Final = f"""
SELECT table_name, column_name, data_type, numeric_scale
FROM (SELECT table_name, column_name, data_type, numeric_scale,
             DENSE_RANK() OVER (ORDER BY table_name) AS t,
             ROW_NUMBER() OVER (PARTITION BY table_name ORDER BY ordinal_position) AS n
      FROM information_schema.columns WHERE table_schema = CURRENT_SCHEMA())
WHERE t <= {MAX_TABLES} AND n <= {MAX_COLUMNS}
ORDER BY t, n
"""  # noqa: S608  (built from constants only)
"""Every column the role may read in the session's database and schema, at most 500 tables of
500 columns."""
ROW_TYPE: Final = {"number": "fixed", "float": "real"}
"""``information_schema``'s ``DATA_TYPE`` (lower case) as ``rowType`` names it, where they
differ."""


def _loaded(pem: str) -> rsa.RSAPrivateKey | None:
    if not pem.lstrip().startswith(PKCS8):
        return None
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except ValueError, TypeError, UnsupportedAlgorithm:
        return None
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < MIN_KEY_BITS:
        return None
    return key


def fingerprint(public: rsa.RSAPublicKey) -> str:
    """The base64 of the SHA-256 of the key's DER ``SubjectPublicKeyInfo``: Snowflake's
    ``RSA_PUBLIC_KEY_FP`` without its ``SHA256:``."""
    der = public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(hashlib.sha256(der).digest()).decode("ascii")


def jwt_account(account: str) -> str:
    """The account as a JWT names it: upper case, without anything from the first ``.`` on
    (the region of a locator such as ``xy12345.us-east-1``); an ``org-account`` is whole."""
    return account.upper().split(".", 1)[0]


def account_url(account: str) -> str:
    """The account's URL: each ``_`` of the identifier a ``-``, as a host name needs."""
    return f"https://{account.replace('_', '-')}.{HOST}"


def claims(account: str, user: str, public: rsa.RSAPublicKey, now: int) -> dict[str, JsonValue]:
    """A key-pair JWT's claims: ``iss`` names the key by its fingerprint, ``sub`` the user."""
    qualified = f"{jwt_account(account)}.{user.upper()}"
    return {
        "iss": f"{qualified}.SHA256:{fingerprint(public)}",
        "sub": qualified,
        "iat": now,
        "exp": now + TOKEN_SECONDS,
    }


@dataclass(frozen=True, slots=True, repr=False)
class KeyPair:
    """The user's RSA key. No repr: it is the credential."""

    key: rsa.RSAPrivateKey

    def token(self, account: str, user: str, now: int) -> str:
        """A JWT for ``user``, valid :data:`TOKEN_SECONDS` from ``now``."""
        return jwt.encode(claims(account, user, self.key.public_key(), now), self.key, "RS256")


def key_pair(private_key: SecretStr) -> KeyPair:
    """The loaded key, or a ``ValueError`` that quotes nothing of it (raised outside the
    ``except`` so it carries no parser error)."""
    key = _loaded(private_key.get_secret_value())
    if key is None:
        raise ValueError(NOT_A_KEY)
    return KeyPair(key)


def _is_a_key(value: SecretStr) -> SecretStr:
    key_pair(value)
    return value


type PrivateKey = Annotated[SecretStr, AfterValidator(_is_a_key)]
"""A target's ``private_key``: an unencrypted PKCS#8 PEM RSA key, refused unless it loads."""


class SnowflakeTarget(BaseModel):
    """One account's database, schema and warehouse, and the service user that reads them.
    ``role`` is the user's default role when left out."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, populate_by_name=True
    )

    kind: Literal["snowflake"] = "snowflake"
    account: str = Field(pattern=ACCOUNT)
    user: str = Field(pattern=IDENTIFIER)
    database: str = Field(pattern=IDENTIFIER)
    schema_name: str = Field(default="PUBLIC", alias="schema", pattern=IDENTIFIER)
    warehouse: str = Field(pattern=IDENTIFIER)
    role: str | None = Field(default=None, pattern=IDENTIFIER)
    private_key: PrivateKey


def placeholders(sql: str) -> int:
    """How many ``?`` placeholders Snowflake reads in ``sql``: one in a string or a comment is
    not one."""
    try:
        tokens = tokenize(sql, read="snowflake")
    except SqlglotError:
        return 0
    return sum(1 for t in tokens if t.token_type == TokenType.PLACEHOLDER)


def _float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return repr(value)


def _binding(value: object) -> dict[str, JsonValue]:  # noqa: PLR0911  (one return per type)
    if value is None:
        return {"type": "TEXT", "value": None}
    if isinstance(value, bool):
        return {"type": "BOOLEAN", "value": "true" if value else "false"}
    if isinstance(value, int):
        if abs(value) >= 10**FIXED_DIGITS:
            raise QueryFailedError(
                "an integer parameter is outside NUMBER(38, 0)", sqlstate="22003"
            )
        return {"type": "FIXED", "value": str(value)}
    if isinstance(value, float):
        return {"type": "REAL", "value": _float(value)}
    if isinstance(value, str):
        return {"type": "TEXT", "value": value}
    if isinstance(value, datetime):
        return {"type": "TIMESTAMP_NTZ", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "DATE", "value": value.isoformat()}
    if isinstance(value, time):
        return {"type": "TIME", "value": value.isoformat()}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "BINARY", "value": bytes(value).hex()}
    raise QueryFailedError("a parameter Snowflake cannot take", sqlstate="22023")


def bindings(params: Sequence[object]) -> dict[str, JsonValue]:
    """``params`` as positional bindings ``"1"``, ``"2"``, ...: a boolean ``BOOLEAN``, an
    integer ``FIXED`` (at most 38 digits), a float ``REAL``, a string ``TEXT``, a datetime
    ``TIMESTAMP_NTZ``, a date ``DATE``, a time ``TIME`` (all three ISO 8601), bytes ``BINARY``
    in hex, ``None`` a ``TEXT`` null."""
    return {str(at): _binding(v) for at, v in enumerate(params, start=1)}


def _field(body: object, name: str, pattern: re.Pattern[str]) -> str | None:
    found = cast("dict[str, object]", body).get(name) if isinstance(body, dict) else None
    return found if isinstance(found, str) and pattern.fullmatch(found) else None


def failure(status: int, body: object, *, cancelled: bool = False) -> Exception:  # noqa: PLR0911  (one return per rule)
    """The connector's error for an answer Snowflake refused: by its ``code`` first, then by
    its status. The message names the code only, never Snowflake's message."""
    code = _field(body, "code", CODE)
    named = code or "unknown"
    if code in FAILED_CODES:
        return QueryFailedError(f"snowflake refused the read: {code}", sqlstate=FAILED_CODES[code])
    if code == TIMED_OUT:
        return TimeoutError("the statement passed its timeout")
    if code == CANCELLED:
        if cancelled:
            return TimeoutError("the statement was cancelled")
        return QueryFailedError(f"snowflake refused the read: {code}", sqlstate="57014")
    if code in CREDENTIAL_CODES or status == 401:
        return QueryFailedError("the source refused the credential", sqlstate="28000")
    if status == 429 or status >= 500:
        return UpstreamUnavailableError(f"snowflake answered {status}: {named}")
    sqlstate = _field(body, "sqlState", SQLSTATE)
    return QueryFailedError(f"snowflake answered {status}: {named}", sqlstate=sqlstate)


def _bad() -> QueryFailedError:
    return QueryFailedError(NOT_A_RESULT, sqlstate="22P02")


@dataclass(frozen=True, slots=True)
class RowType:
    """One column as ``resultSetMetaData.rowType`` names it; ``type`` in lower case."""

    name: str
    type: str
    scale: int = 0

    @property
    def portable(self) -> str:
        if self.type == "fixed":
            return "integer" if self.scale == 0 else "decimal"
        return PORTABLE.get(self.type, "string")


def row_types(raw: object) -> tuple[RowType, ...]:
    """``rowType`` as :class:`RowType`, or 22P02."""
    if not isinstance(raw, list):
        raise _bad()
    found: list[RowType] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            raise _bad()
        column = cast("dict[str, object]", item)
        name, kind, scale = column.get("name"), column.get("type"), column.get("scale")
        scale = 0 if scale is None else scale
        if not (isinstance(name, str) and isinstance(kind, str)):
            raise _bad()
        if not isinstance(scale, int) or isinstance(scale, bool):
            raise _bad()
        found.append(RowType(name, kind.lower(), scale))
    return tuple(found)


def described(name: str, data_type: str, scale: object) -> Column:
    """The portable column for an ``information_schema.columns`` row, as a read types it."""
    kind = data_type.lower()
    found = RowType(name, ROW_TYPE.get(kind, kind), scale if isinstance(scale, int) else 0)
    return Column(name, found.portable, found.type)


def _seconds(text: str) -> int:
    """Seconds with a fraction as whole microseconds, truncated toward the past."""
    seconds = Decimal(text)
    if not seconds.is_finite():
        raise ValueError(text)
    return int((seconds * 10**6).to_integral_value(rounding=ROUND_FLOOR))


def _time(text: str) -> time:
    micros = _seconds(text)
    if not 0 <= micros < 86_400 * 10**6:
        raise ValueError(text)
    whole, micro = divmod(micros, 10**6)
    return time(whole // 3600, whole // 60 % 60, whole % 60, micro)


def _timestamp_tz(text: str) -> datetime:
    """``<seconds since the epoch> <1440 + offset in minutes>``."""
    seconds, offset = text.split(" ")
    zone = timezone(timedelta(minutes=int(offset) - TZ_BASE))
    return (_EPOCH_UTC + timedelta(microseconds=_seconds(seconds))).astimezone(zone)


def _decimal(text: str) -> Decimal:
    value = Decimal(text)
    if not value.is_finite():
        raise ValueError(text)
    return value


def _bool(text: str) -> bool:
    if text not in ("true", "false"):
        raise ValueError(text)
    return text == "true"


def scalar(column: RowType, text: str) -> object:  # noqa: PLR0911  (one return per type)
    """One value's text as its column's type."""
    match column.type:
        case "fixed":
            return int(text) if column.scale == 0 else _decimal(text)
        case "real":
            return float(text)
        case "boolean":
            return _bool(text)
        case "date":
            return _EPOCH_DAY + timedelta(days=int(text))
        case "time":
            return _time(text)
        case "timestamp_ntz":
            return _EPOCH + timedelta(microseconds=_seconds(text))
        case "timestamp_ltz":
            return _EPOCH_UTC + timedelta(microseconds=_seconds(text))
        case "timestamp_tz":
            return _timestamp_tz(text)
        case "binary":
            return bytes.fromhex(text)
        case "variant" | "object" | "array":
            return cast("object", json.loads(text))
        case _:
            return text


def row(columns: Sequence[RowType], raw: object) -> list[object]:
    """One of ``data``, an array of text or nulls, in column order."""
    if not isinstance(raw, list) or len(cast("list[object]", raw)) != len(columns):
        raise _bad()
    cells = cast("list[object]", raw)
    found: list[object] = []
    try:
        for column, cell in zip(columns, cells, strict=True):
            if cell is not None and not isinstance(cell, str):
                raise _bad()
            found.append(None if cell is None else scalar(column, cell))
    except ValueError, ArithmeticError, RecursionError:
        raise _bad() from None
    return found


def handle(body: object) -> str:
    """A 202's or a result's ``statementHandle``, or 22P02."""
    found = _field(body, "statementHandle", HANDLE)
    if found is None:
        raise _bad()
    return found


def partition_data(body: object) -> list[object]:
    """A partition's ``data``, or 22P02."""
    data = cast("dict[str, object]", body).get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise _bad()
    return cast("list[object]", data)


@dataclass(frozen=True, slots=True)
class Result:
    """A 200 answer: the statement's handle, its columns, how many partitions it has and the
    first partition's rows."""

    handle: str
    columns: tuple[RowType, ...]
    partitions: int
    data: list[object]


def result(body: object) -> Result:
    """A 200 answer as a :class:`Result`, or 22P02. Without ``partitionInfo`` there is one
    partition."""
    if not isinstance(body, dict):
        raise _bad()
    answer = cast("dict[str, object]", body)
    meta = answer.get("resultSetMetaData")
    if not isinstance(meta, dict):
        raise _bad()
    found = cast("dict[str, object]", meta)
    columns = row_types(found.get("rowType"))
    partitions = found.get("partitionInfo")
    if partitions is None:
        partitions = []
    if not isinstance(partitions, list):
        raise _bad()
    count = max(1, len(cast("list[object]", partitions)))
    return Result(handle(answer), columns, count, partition_data(answer))


class _Cursor:
    def __init__(self, columns: list[Column], rows: list[list[object]]) -> None:
        self._columns = columns
        self._rows = rows

    @property
    def columns(self) -> Sequence[Column]:
        return self._columns

    async def rows(self) -> AsyncIterator[Sequence[object]]:
        for found in self._rows:
            yield found


def _read_on(_: int) -> None:
    """Every status reads the body: Snowflake's code is in it."""


@dataclass
class _Read:
    """What one read did, for its log line, and the statement to cancel."""

    handle: str | None = None
    cancelled: bool = False
    partitions: int = 0
    polls: int = 0
    bytes: int = 0


class SnowflakeConnector:
    """A :class:`ssc_datagw.connectors.Connector` for one Snowflake database. ``transport``,
    ``base_url``, ``clock`` and ``poll_seconds`` replace the network, Snowflake, the time and
    the poll's pause (tests)."""

    def __init__(  # noqa: PLR0913  (the target and its test seams)
        self,
        target: SnowflakeTarget,
        *,
        connect_seconds: float = CONNECT_SECONDS,
        warmup: Warmup | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        base_url: str | None = None,
        clock: Callable[[], float] = wall_clock,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._target = target
        self._key = key_pair(target.private_key)
        self._base = (base_url or account_url(target.account)).rstrip("/") + STATEMENTS
        self._tls = tls_context(None)
        self._connect_seconds = connect_seconds
        self._warmup = warmup or Warmup()
        self._transport = transport
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._ending: set[asyncio.Task[None]] = set()

    def _client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            verify=self._tls, transport=self._transport, follow_redirects=False, trust_env=False
        )

    def _request(self, query: Query) -> dict[str, JsonValue]:
        t = self._target
        body: dict[str, JsonValue] = {
            "statement": query.sql,
            "timeout": math.ceil(max(1, query.timeout_ms) / 1000),
            "database": t.database,
            "schema": t.schema_name,
            "warehouse": t.warehouse,
            "bindings": bindings(query.params),
            "parameters": {"query_tag": query.tag, "MULTI_STATEMENT_COUNT": "1"},
            "resultSetMetaData": {"format": "jsonv2"},
        }
        if t.role is not None:
            body["role"] = t.role
        return body

    async def _call(  # noqa: PLR0913  (the request and how it is timed)
        self,
        client: httpx2.AsyncClient,
        url: httpx2.URL,
        headers: dict[str, str],
        read: _Read,
        *,
        seconds: float,
        content: bytes | None = None,
    ) -> tuple[int, object]:
        status, body = await get(
            client,
            url,
            headers,
            seconds=seconds,
            connect_seconds=self._connect_seconds,
            warmup=self._warmup,
            failure=_read_on,
            method="GET" if content is None else "POST",
            content=content,
        )
        read.bytes += len(body)
        try:
            parsed = cast("object", json.loads(body))
        except ValueError, RecursionError:
            parsed = None
        if status not in (200, 202):
            raise failure(status, parsed, cancelled=read.cancelled)
        return status, parsed

    async def _read(
        self, client: httpx2.AsyncClient, headers: dict[str, str], query: Query, read: _Read
    ) -> tuple[list[Column], list[list[object]]]:
        want = query.max_rows + 1
        seconds = max(1, query.timeout_ms) / 1000
        content = json.dumps(self._request(query)).encode()
        status, body = await self._call(
            client, httpx2.URL(self._base), headers, read, seconds=seconds, content=content
        )
        while status == 202:  # noqa: PLR2004  (still running)
            read.handle = handle(body)
            await asyncio.sleep(self._poll_seconds)
            read.polls += 1
            url = httpx2.URL(f"{self._base}/{read.handle}")
            status, body = await self._call(client, url, headers, read, seconds=seconds)
        first = result(body)
        read.handle = first.handle
        raw = first.data[:want]
        read.partitions = 1
        for partition in range(1, first.partitions):
            if len(raw) >= want:
                break
            url = httpx2.URL(f"{self._base}/{first.handle}", params={"partition": partition})
            _, body = await self._call(client, url, headers, read, seconds=seconds)
            read.partitions += 1
            raw += partition_data(body)[: want - len(raw)]
        columns = [Column(c.name, c.portable, c.type) for c in first.columns]
        return columns, [row(first.columns, r) for r in raw]

    async def _cancel(self, statement: str, headers: dict[str, str]) -> None:
        """Cancel ``statement``, best effort, from a client of its own."""
        url = httpx2.URL(f"{self._base}/{statement}/cancel")
        try:
            async with asyncio.timeout(CANCEL_SECONDS):
                client = self._client()
                try:
                    await get(
                        client,
                        url,
                        headers,
                        seconds=CANCEL_SECONDS,
                        connect_seconds=min(self._connect_seconds, CANCEL_SECONDS),
                        warmup=self._warmup,
                        method="POST",
                        content=b"",
                    )
                finally:
                    await client.aclose()
        except (QueryFailedError, UpstreamUnavailableError, TimeoutError) as exc:
            log.warning("could not cancel the statement: %s", type(exc).__name__)

    async def _end(self, read: _Read, headers: dict[str, str]) -> None:
        """Run :meth:`_cancel` to the end even if the caller is cancelled again."""
        if read.handle is None:
            return
        read.cancelled = True
        task = asyncio.create_task(self._cancel(read.handle, headers))
        self._ending.add(task)
        task.add_done_callback(self._ending.discard)
        await asyncio.shield(task)

    @asynccontextmanager
    async def open(self, query: Query) -> AsyncGenerator[_Cursor]:
        t = self._target
        reason = snowflake_refusal(query.sql, t.database)
        if reason is not None:
            raise QueryRefusedError(reason)
        count = placeholders(query.sql)
        if count != len(query.params):
            raise QueryFailedError(
                f"{len(query.params)} parameters for {count} placeholders", sqlstate="07001"
            )
        bindings(query.params)  # a parameter Snowflake cannot take fails before a request
        columns, rows = await self._run(query)
        yield _Cursor(columns, rows)

    async def describe(self, *, schemas: Sequence[str] | None, timeout_ms: int) -> list[Table]:
        """``information_schema.columns`` of the session's database and schema through
        :meth:`_run`; ``schemas`` does not apply (the schema is the connection's)."""
        del schemas
        query = Query(
            sql=DESCRIBE,
            params=(),
            max_rows=MAX_TABLES * MAX_COLUMNS,
            timeout_ms=timeout_ms,
            tag=DESCRIBE_TAG,
        )
        _, rows = await self._run(query)
        return grouped(
            (str(table), described(str(name), str(kind), scale))
            for table, name, kind, scale in rows
        )

    async def _run(self, query: Query) -> tuple[list[Column], list[list[object]]]:
        """Steps 3 and 4 of the module's order, under ``timeout_ms``, the statement cancelled
        past it or when the caller is."""
        t = self._target
        token = self._key.token(t.account, t.user, int(self._clock()))
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {token}",
            "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT",
        }
        read = _Read()
        client = self._client()
        try:
            async with asyncio.timeout(max(1, query.timeout_ms) / 1000):
                columns, rows = await self._read(client, headers, query, read)
        except TimeoutError:
            await self._end(read, headers)
            raise TimeoutError("the read passed timeout_ms") from None
        except asyncio.CancelledError:
            await self._end(read, headers)
            raise
        finally:
            await client.aclose()
        log.info(
            "snowflake read: partitions=%s polls=%s bytes=%s",
            read.partitions,
            read.polls,
            read.bytes,
        )
        return columns, rows
