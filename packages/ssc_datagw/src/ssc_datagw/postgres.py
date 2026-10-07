"""The Postgres connector: read-only even against a hostile query (SSC-051).

Each query gets its own connection, over TLS, as the least-privilege role ``postgres_setup.sql``
creates, and runs in this order:

1. :func:`ssc_datagw.classify.refusal` refuses anything that is not one plain read;
2. ``BEGIN READ ONLY``, then one statement that reads back the session (the backend
   pid, read-only, ``standard_conforming_strings``, the role's posture) and sets the
   transaction's ``statement_timeout`` and the query's tag as ``application_name``;
3. the statement is prepared, so Postgres itself refuses a second statement, and read through a
   cursor, at most ``max_rows`` plus one rows, which is how the gateway sees the result was cut;
4. ``ROLLBACK``, and the connection is closed; on every other path closing it aborts the
   transaction, so nothing a read did is ever committed.

The connection is refused before any read when the session is not what it was asked to be: a
backend pid that differs from the one the server announced, or startup settings that did not
land, mean a pooler sits in between and would break the session guarantees; a role that is a
superuser or may create objects or temporary tables is not the role the setup script makes.

When the gateway cancels a read (its kill watch or its deadline), the connector ends the backend
with ``pg_terminate_backend`` from a second connection as the same role, which may end only its
own sessions, so a statement that ignores the cancel does not run on.

A new instance's outbound calls through Cloud NAT may not connect for its first 20 to 37 s
(``spikes/proofrun`` T6). For :data:`WARMUP_SECONDS` after the process starts, a connect that
times out is tried again, each attempt cut to :data:`WARMUP_CONNECT_SECONDS`, until it connects
or the gateway's deadline cancels the read. Any other failure, and a time-out after warm-up,
is ``UpstreamUnavailableError`` at once.
"""

import asyncio
import json
import logging
import ssl
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any, Final, cast
from uuid import UUID

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ssc_datagw.classify import refusal
from ssc_datagw.connectors import (
    Column,
    Query,
    QueryFailedError,
    QueryRefusedError,
    Scalar,
    UpstreamUnavailableError,
)

log = logging.getLogger(__name__)

CONNECT_SECONDS: Final = 10.0
WARMUP_SECONDS: Final = 60.0
WARMUP_CONNECT_SECONDS: Final = 5.0
WARMUP_PAUSE_SECONDS: Final = 1.0
PROCESS_STARTED: Final = monotonic()
KILL_SECONDS: Final = 5.0
BATCH: Final = 500
IDLE_IN_TRANSACTION_MS: Final = 60_000
APPLICATION_NAME: Final = "ssc-datagw"
SESSION: Final = {
    "application_name": APPLICATION_NAME,
    "default_transaction_read_only": "on",
    "standard_conforming_strings": "on",
    "idle_in_transaction_session_timeout": str(IDLE_IN_TRANSACTION_MS),
}
"""Sent in the startup packet and read back, since a pooler can drop them."""
NAME_BYTES: Final = 63
READBACK: Final = """
SELECT pg_catalog.pg_backend_pid() AS pid,
       pg_catalog.current_setting('transaction_read_only') AS read_only,
       pg_catalog.current_setting('standard_conforming_strings') AS conforming,
       pg_catalog.current_setting('default_transaction_read_only') AS default_read_only,
       r.rolsuper AS superuser,
       pg_catalog.has_database_privilege(pg_catalog.current_database(), 'CREATE') AS db_create,
       pg_catalog.has_database_privilege(pg_catalog.current_database(), 'TEMP') AS db_temp,
       EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n
               WHERE pg_catalog.has_schema_privilege(n.oid, 'CREATE')) AS schema_create,
       pg_catalog.set_config('statement_timeout', $1, true) AS timeout,
       pg_catalog.set_config('application_name', $2, true) AS tag
FROM pg_catalog.pg_roles r WHERE r.rolname = current_user
"""
TERMINATE: Final = "SELECT pg_catalog.pg_terminate_backend($1)"
UNAVAILABLE_CLASSES: Final = ("08", "53", "57P", "58")
"""SQLSTATE classes that mean the database, not the statement: connection, resources, operator
intervention (a terminated backend), system errors."""
TIMEOUT_SQLSTATE: Final = "57014"
PORTABLE: Final = {
    "int2": "integer",
    "int4": "integer",
    "int8": "integer",
    "oid": "integer",
    "float4": "float",
    "float8": "float",
    "numeric": "decimal",
    "bool": "boolean",
    "date": "date",
    "timestamp": "timestamp",
    "timestamptz": "timestamp",
    "time": "time",
    "timetz": "time",
    "interval": "interval",
    "bytea": "bytes",
    "uuid": "uuid",
    "json": "json",
    "jsonb": "json",
}
"""Portable column types; any other type is ``string`` (an array ``array``)."""
_JSON: Final = frozenset({"json", "jsonb"})
_CONNECT: Final = cast("Callable[..., Awaitable[Any]]", asyncpg.connect)  # pyright: ignore[reportUnknownMemberType]


class PostgresTarget(BaseModel):
    """Where one connection points and the role it logs in as. ``ca`` is the server's CA
    certificate in PEM, pasted by the customer: with it the chain must lead to that CA and the
    name is not checked (``verify-ca``, as for a Cloud SQL certificate that names its instance,
    not its address); without it the system trust store and the host name decide
    (``verify-full``). There is no way to turn TLS or its check off."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=NAME_BYTES)
    user: str = Field(min_length=1, max_length=NAME_BYTES)
    password: SecretStr
    ca: str | None = Field(default=None, min_length=1)


def tls_context(ca: str | None) -> ssl.SSLContext:
    """``verify-ca`` against a pasted CA, else ``verify-full`` against the system store. Python
    3.13 and later refuse a CA without an Authority Key Identifier under ``VERIFY_X509_STRICT``,
    which Cloud SQL's per-instance CA lacks, so that one flag is cleared for a pasted CA."""
    if ca is None:
        return ssl.create_default_context()
    try:
        context = ssl.create_default_context(cadata=ca)
    except (ssl.SSLError, ValueError) as exc:
        raise ValueError("the CA certificate is not PEM") from exc
    context.check_hostname = False
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


@dataclass(frozen=True, slots=True)
class Readback:
    """The first row of every transaction: what the session really is."""

    pid: int
    read_only: str
    conforming: str
    default_read_only: str
    superuser: bool
    db_create: bool
    db_temp: bool
    schema_create: bool


def session_problem(server_pid: int, row: Readback) -> str | None:
    """Why this session must not run the read, or ``None``. ``server_pid`` is the pid the server
    announced at startup; behind a pooler the statement runs on another backend."""
    checks = (
        (row.pid != server_pid, "the backend pid is not the announced one: a pooler is between"),
        (row.default_read_only != "on", "the startup settings did not land: a pooler is between"),
        (row.read_only != "on", "the transaction is not read-only"),
        (row.conforming != "on", "standard_conforming_strings is off"),
        (row.superuser, "the role is a superuser"),
        (row.db_create or row.schema_create, "the role may create objects"),
        (row.db_temp, "the role may create temporary tables"),
    )
    return next((why for failed, why in checks if failed), None)


def portable(type_name: str, kind: str) -> str:
    if kind == "array":
        return "array"
    return PORTABLE.get(type_name, "string")


def _coerce(value: Scalar, type_name: str) -> object:  # noqa: PLR0911  (one return per type)
    """JSON has no date, time, UUID or exact decimal, so a string (or a number, for
    ``numeric``) bound to one of those is converted; asyncpg refuses the rest by type."""
    if isinstance(value, bool) or value is None:
        return value
    if type_name == "numeric":
        return Decimal(str(value))
    if not isinstance(value, str):
        return value
    match type_name:
        case "date":
            return date.fromisoformat(value)
        case "timestamp" | "timestamptz":
            return datetime.fromisoformat(value)
        case "time" | "timetz":
            return time.fromisoformat(value)
        case "uuid":
            return UUID(value)
        case _:
            return value


def bind(params: Sequence[Scalar], types: Sequence[str]) -> list[object]:
    """``params`` for a statement whose parameters have ``types``."""
    if len(params) != len(types):
        raise QueryFailedError(
            f"{len(params)} parameters for {len(types)} placeholders", sqlstate="08P01"
        )
    try:
        return [_coerce(v, t) for v, t in zip(params, types, strict=True)]
    except (ValueError, InvalidOperation) as exc:
        raise QueryFailedError("a parameter does not fit its type", sqlstate="22P02") from exc


def _value(value: object, is_json: bool) -> object:
    if is_json and isinstance(value, str):
        return json.loads(value)
    if isinstance(value, asyncpg.Record):
        return dict(cast("Any", value).items())
    return value


class _Cursor:
    def __init__(self, columns: list[Column], cursor: Any, limit: int) -> None:
        self._columns = columns
        self._cursor = cursor
        self._limit = limit
        self._json = [c.db_type in _JSON for c in columns]

    @property
    def columns(self) -> Sequence[Column]:
        return self._columns

    async def rows(self) -> AsyncIterator[Sequence[object]]:
        left = self._limit
        while left > 0:
            batch = cast("list[Any]", await self._cursor.fetch(min(left, BATCH)))
            if not batch:
                return
            left -= len(batch)
            for record in batch:
                yield [_value(v, j) for v, j in zip(record.values(), self._json, strict=True)]


def _failure(exc: Exception) -> Exception:
    """The connector's error for a database error. The message names the error class only: the
    database's own text can quote parameter values, and the gateway logs this message."""
    name = type(exc).__name__
    if isinstance(exc, asyncpg.PostgresError):
        sqlstate = cast("str | None", getattr(exc, "sqlstate", None))
        if sqlstate == TIMEOUT_SQLSTATE:
            return TimeoutError("statement_timeout")
        if sqlstate is None or sqlstate.startswith(UNAVAILABLE_CLASSES):
            return UpstreamUnavailableError(f"the database ended the read: {name}")
        return QueryFailedError(name, sqlstate=sqlstate)
    if isinstance(exc, asyncpg.InterfaceError) and isinstance(exc, ValueError):
        return QueryFailedError("a parameter does not fit its type", sqlstate="22P02")
    return UpstreamUnavailableError(f"the connection failed: {name}")


@dataclass(frozen=True, slots=True)
class Warmup:
    """When the process started, and the clock and pause the warm-up retry uses."""

    started: float = PROCESS_STARTED
    clock: Callable[[], float] = monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


class PostgresConnector:
    """A :class:`ssc_datagw.connectors.Connector` for one Postgres connection. ``classify``
    replaces the classifier (tests that prove what the database refuses by itself)."""

    def __init__(
        self,
        target: PostgresTarget,
        *,
        classify: Callable[[str], str | None] = refusal,
        connect_seconds: float = CONNECT_SECONDS,
        warmup: Warmup | None = None,
    ) -> None:
        self._target = target
        self._tls = tls_context(target.ca)
        self._classify = classify
        self._connect_seconds = connect_seconds
        self._warmup = warmup or Warmup()
        self._ending: set[asyncio.Task[None]] = set()

    def _warming(self) -> bool:
        return self._warmup.clock() - self._warmup.started < WARMUP_SECONDS

    async def _connect(self) -> Any:
        while True:
            warming = self._warming()
            seconds = min(self._connect_seconds, WARMUP_CONNECT_SECONDS) if warming else None
            try:
                return await self._connect_once(seconds or self._connect_seconds)
            except TimeoutError:
                if not warming:
                    raise UpstreamUnavailableError("cannot connect: TimeoutError") from None
                log.info("connect timed out while the instance is new; trying again")
            await self._warmup.sleep(WARMUP_PAUSE_SECONDS)

    async def _connect_once(self, seconds: float) -> Any:
        t = self._target
        try:
            return await _CONNECT(
                host=t.host,
                port=t.port,
                user=t.user,
                password=t.password.get_secret_value(),
                database=t.database,
                ssl=self._tls,
                direct_tls=False,
                timeout=seconds,
                statement_cache_size=0,
                server_settings=SESSION,
            )
        except TimeoutError:
            raise
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            raise UpstreamUnavailableError(f"cannot connect: {type(exc).__name__}") from None

    async def _terminate(self, pid: int) -> None:
        """End backend ``pid`` from a second session of the same role."""
        try:
            async with asyncio.timeout(KILL_SECONDS):
                conn = await self._connect()
                try:
                    await conn.fetchval(TERMINATE, pid)
                finally:
                    conn.terminate()
        except (UpstreamUnavailableError, TimeoutError, asyncpg.PostgresError) as exc:
            log.warning("could not end backend %s: %s", pid, type(exc).__name__)

    async def _end(self, pid: int) -> None:
        """Run :meth:`_terminate` to the end even if the caller is cancelled again."""
        task = asyncio.create_task(self._terminate(pid))
        self._ending.add(task)
        task.add_done_callback(self._ending.discard)
        await asyncio.shield(task)

    @asynccontextmanager
    async def open(self, query: Query) -> AsyncGenerator[_Cursor]:
        reason = self._classify(query.sql)
        if reason is not None:
            raise QueryRefusedError(reason)
        conn = await self._connect()
        pid: int | None = None
        try:
            transaction = conn.transaction(readonly=True)
            await transaction.start()
            tag = query.tag.encode()[:NAME_BYTES].decode(errors="ignore")
            record = await conn.fetchrow(READBACK, str(max(1, query.timeout_ms)), tag)
            if record is None:
                raise UpstreamUnavailableError("the session could not be read back")
            row = Readback(*(record[f.name] for f in fields(Readback)))
            problem = session_problem(conn.get_server_pid(), row)
            if problem is not None:
                raise UpstreamUnavailableError(problem)
            pid = row.pid
            statement = await conn.prepare(query.sql)
            args = bind(query.params, [p.name for p in statement.get_parameters()])
            columns = [
                Column(a.name, portable(a.type.name, a.type.kind), a.type.name)
                for a in statement.get_attributes()
            ]
            cursor = await statement.cursor(*args)
            yield _Cursor(columns, cursor, query.max_rows + 1)
            await transaction.rollback()
        except asyncio.CancelledError:
            if pid is not None:
                await self._end(pid)
            raise
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
            raise _failure(exc) from None
        finally:
            conn.terminate()
