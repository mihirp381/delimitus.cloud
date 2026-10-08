"""The MySQL connector (GA-5): one read on a MySQL 8.0 or later source, over TLS, as the
read-only user ``mysql_setup.sql`` makes.

The shape follows :mod:`ssc_datagw.postgres`. Each read opens its own session (no pool: the
password is a per-connection secret and a session carries state), settles the session in one
``SET`` (the statement timeout, a read-only transaction, UTC, a fixed ``sql_mode``), starts a
read-only transaction, reads the session back and refuses to run when it is not what was asked
for, then streams the rows with the text protocol. The read-back checks the same things the
Postgres one does: that the session is the one the server announced (a pooler between would
answer another), that the transaction is read-only, that the user holds nothing beyond
``SELECT`` and has no active role.

MySQL has no server-side prepared statement that streams rows (asyncmy buffers the result of a
prepared statement), so a ``?`` placeholder is bound by the connector: each one the classifier's
tokenizer found is replaced by the parameter as a quoted literal the driver escapes. This is
safe because ``sql_mode`` is set here and read back, so ``NO_BACKSLASH_ESCAPES`` is never on, and
because the text the classifier approved had the placeholder where a value may stand. ``%`` in
the statement is never a format directive.

A description (:meth:`MySqlConnector.describe`, GA-5.8) is one fixed statement of ours on
``information_schema.columns`` for the session's database, run in the same checked session as a
read, without the classifier, which is for the app's text.

What the database refuses on its own, with the classifier off (``tests/test_mysql.py``): a
write (``1142``, the user has ``SELECT`` only), a temporary table (``1792``, the transaction is
read-only), a change of the transaction (``1568``), a locking read (``1142``), a file
(``1227``). The driver sends every statement with ``MULTI_STATEMENTS`` on, so only the
classifier holds a text to one statement; the second statement of a pair meets the same
privileges as the first.
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast

import asyncmy
import sqlglot
from asyncmy import errors
from asyncmy.cursors import SSCursor
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from ssc_datagw.classify import mysql_refusal
from ssc_datagw.connectors import (
    DESCRIBE_TAG,
    MAX_COLUMNS,
    MAX_TABLES,
    Column,
    Query,
    QueryFailedError,
    QueryRefusedError,
    Scalar,
    Table,
    UpstreamUnavailableError,
    grouped,
)
from ssc_datagw.tls import tls_context
from ssc_datagw.warmup import CONNECT_SECONDS, Warmup, connect_with_warmup

log = logging.getLogger(__name__)

KILL_SECONDS: Final = 5.0
BATCH: Final = 500
PROGRAM_NAME: Final = "ssc-datagw"
NAME_BYTES: Final = 64
USER_BYTES: Final = 32
SQL_MODE: Final = (
    "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION"
)
"""MySQL 8.0's default ``sql_mode``, set for the session and read back: never ``ANSI_QUOTES``
(a double-quoted string stays a string) and never ``NO_BACKSLASH_ESCAPES`` (the driver's
escaping stays right)."""
SESSION: Final = (
    "SET SESSION max_execution_time = {timeout_ms}, transaction_read_only = 1, "
    "time_zone = '+00:00', sql_mode = '" + SQL_MODE + "'"
)
BEGIN: Final = "START TRANSACTION READ ONLY"
_GRANTEE: Final = "CONCAT('''', REPLACE(CURRENT_USER(), '@', '''@'''), '''')"
"""``'user'@'host'`` as ``information_schema`` spells the grantee."""
READBACK: Final = f"""
SELECT CONNECTION_ID() AS id,
       @@session.transaction_read_only AS read_only,
       @@session.max_execution_time AS timeout_ms,
       @@session.sql_mode AS sql_mode,
       @@session.time_zone AS time_zone,
       CURRENT_ROLE() AS roles,
       (SELECT COUNT(*) FROM information_schema.user_privileges
        WHERE grantee = {_GRANTEE} AND privilege_type <> 'USAGE') AS global_grants,
       (SELECT COUNT(*) FROM information_schema.schema_privileges
        WHERE grantee = {_GRANTEE} AND privilege_type <> 'SELECT') AS schema_grants,
       (SELECT COUNT(*) FROM information_schema.table_privileges
        WHERE grantee = {_GRANTEE} AND privilege_type <> 'SELECT') AS table_grants,
       (SELECT COUNT(*) FROM information_schema.column_privileges
        WHERE grantee = {_GRANTEE} AND privilege_type <> 'SELECT') AS column_grants
"""  # noqa: S608  (built from constants only)
KILL: Final = "KILL QUERY {id}"
TIMEOUT_ERRNO: Final = 3024
"""``ER_QUERY_TIMEOUT``: ``max_execution_time`` ran out."""
SQLSTATE: Final = {
    1044: "42000",  # access denied to the database
    1142: "42000",  # command denied for a table
    1143: "42000",  # command denied for a column
    1227: "42000",  # a privilege is needed (FILE, SUPER, ...)
    1370: "42000",  # execute denied for a routine
    1064: "42000",  # syntax
    1305: "42000",  # no such function or procedure
    1146: "42S02",  # no such table
    1054: "42S22",  # no such column
    1792: "25006",  # a write in a read-only transaction
    1568: "25001",  # the transaction's characteristics cannot change now
    1690: "22003",  # out of range
    3140: "22032",  # invalid JSON text
    1317: "70100",  # interrupted: a KILL QUERY from outside the gateway
}
"""The SQLSTATE a MySQL error number stands for (the driver surfaces the number only)."""
CONNECTION_ERRNOS: Final = frozenset(
    {2002, 2003, 2006, 2013, 1040, 1053, 1077, 1152, 1159, 1160, 1161}
)
"""Driver and server numbers that mean the connection, not the statement."""
TAG_BYTES: Final = 64
_PLAIN_TAG: Final = re.compile(r"[^ -~]|\*/")
_STRING: Final = frozenset({15, 249, 250, 251, 252, 253, 254})
_BINARY_CHARSET: Final = 63
_FLAG_BINARY: Final = 128
_FLAG_ENUM: Final = 256
_FLAG_SET: Final = 2048
SIMPLE: Final = {
    2: ("integer", "smallint"),
    3: ("integer", "int"),
    8: ("integer", "bigint"),
    9: ("integer", "mediumint"),
    13: ("integer", "year"),
    16: ("integer", "bit"),
    246: ("decimal", "decimal"),
    4: ("float", "float"),
    5: ("float", "double"),
    10: ("date", "date"),
    7: ("timestamp", "timestamp"),
    12: ("timestamp", "datetime"),
    11: ("interval", "time"),
    245: ("json", "json"),
    255: ("bytes", "geometry"),
}
"""Portable type and MySQL name by field type code, for the codes that need no flag."""
_TEXT: Final = {
    15: "varchar",
    249: "tinytext",
    250: "mediumtext",
    251: "longtext",
    252: "text",
    253: "varchar",
    254: "char",
}
_BINARY: Final = {
    15: "varbinary",
    249: "tinyblob",
    250: "mediumblob",
    251: "longblob",
    252: "blob",
    253: "varbinary",
    254: "binary",
}
BY_NAME: Final = {
    **{db_type: portable for portable, db_type in SIMPLE.values()},
    **dict.fromkeys(_TEXT.values(), "string"),
    **dict.fromkeys(_BINARY.values(), "bytes"),
    "tinyint": "integer",
    "enum": "string",
    "set": "string",
    **dict.fromkeys(
        (
            "point",
            "linestring",
            "polygon",
            "multipoint",
            "multilinestring",
            "multipolygon",
            "geomcollection",
            "geometrycollection",
        ),
        "bytes",
    ),
}
"""Portable type by ``information_schema``'s ``DATA_TYPE``: the names :func:`column` gives a
read's columns, and the spatial types a read answers as ``geometry`` bytes."""
DESCRIBE: Final = f"""
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE
FROM (SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE,
             DENSE_RANK() OVER (ORDER BY TABLE_NAME) AS t,
             ROW_NUMBER() OVER (PARTITION BY TABLE_NAME ORDER BY ORDINAL_POSITION) AS n
      FROM information_schema.columns WHERE TABLE_SCHEMA = DATABASE()) d
WHERE t <= {MAX_TABLES} AND n <= {MAX_COLUMNS}
ORDER BY t, n
"""  # noqa: S608  (built from constants only)
"""Every column the user may read in the session's database, at most 500 tables of 500."""
_CONNECT: Final = cast("Callable[..., Any]", asyncmy.connect)  # pyright: ignore[reportUnknownMemberType]


class MySqlTarget(BaseModel):
    """Where one connection points and the user it logs in as. ``database`` is the schema the
    session starts in, which the user must be able to read. ``ca`` is as for Postgres: with it
    the chain must lead to that CA, without it the system trust store and the host name decide.
    There is no way to turn TLS or its check off."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["mysql"] = "mysql"
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=3306, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=NAME_BYTES)
    user: str = Field(min_length=1, max_length=USER_BYTES)
    password: SecretStr
    ca: str | None = Field(default=None, min_length=1)


@dataclass(frozen=True, slots=True)
class Readback:
    """The first row of every transaction: what the session really is."""

    id: int
    read_only: int
    timeout_ms: int
    sql_mode: str
    time_zone: str
    roles: str
    global_grants: int
    schema_grants: int
    table_grants: int
    column_grants: int


def session_problem(thread_id: int, timeout_ms: int, row: Readback) -> str | None:
    """Why this session must not run the read, or ``None``. ``thread_id`` is the id the server
    announced at the handshake; behind a pooler the statement runs on another session."""
    checks = (
        (row.id != thread_id, "the session is not the announced one: a pooler is between"),
        (row.read_only != 1, "the transaction is not read-only"),
        (row.timeout_ms != timeout_ms, "the statement timeout did not land"),
        (row.sql_mode != SQL_MODE, "sql_mode is not the one set"),
        (row.time_zone != "+00:00", "the session is not in UTC"),
        (row.roles != "NONE", "the user has an active role"),
        (row.global_grants > 0, "the user holds a global privilege"),
        (
            row.schema_grants > 0 or row.table_grants > 0 or row.column_grants > 0,
            "the user holds a privilege beyond SELECT",
        ),
    )
    return next((why for failed, why in checks if failed), None)


@dataclass(frozen=True, slots=True)
class FieldInfo:
    """What the result's column descriptor says: the driver's ``FieldDescriptorPacket``."""

    name: str
    type_code: int
    charsetnr: int
    flags: int
    length: int


def _string_column(f: FieldInfo) -> Column:
    if f.flags & _FLAG_ENUM:
        return Column(f.name, "string", "enum")
    if f.flags & _FLAG_SET:
        return Column(f.name, "string", "set")
    if f.charsetnr == _BINARY_CHARSET or f.flags & _FLAG_BINARY:
        return Column(
            f.name, "bytes", _BINARY.get(f.type_code, {253: "varbinary"}.get(f.type_code, "binary"))
        )
    return Column(
        f.name, "string", _TEXT.get(f.type_code, {253: "varchar"}.get(f.type_code, "char"))
    )


def column(f: FieldInfo) -> Column:
    """The portable column for a field (``docs/contracts/data-gateway.md``, Response).
    ``TINYINT(1)`` is how MySQL spells ``BOOL``; any other ``TINYINT`` is an integer."""
    if f.type_code == 1:
        if f.length == 1:
            return Column(f.name, "boolean", "tinyint(1)")
        return Column(f.name, "integer", "tinyint")
    if f.type_code in SIMPLE:
        portable, db_type = SIMPLE[f.type_code]
        return Column(f.name, portable, db_type)
    if f.type_code in _STRING:
        return _string_column(f)
    return Column(f.name, "string", f"type {f.type_code}")


def described(name: str, data_type: str, column_type: str) -> Column:
    """The portable column for an ``information_schema.columns`` row, as :func:`column` types
    the same column in a read. ``TINYINT(1)`` is a boolean there too."""
    kind, full = data_type.lower(), column_type.lower()
    if kind == "tinyint" and full.startswith("tinyint(1)"):
        return Column(name, "boolean", "tinyint(1)")
    return Column(name, BY_NAME.get(kind, "string"), kind)


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _value(value: object, col: Column) -> object:
    """A driver value as the response spells it. ``TIMESTAMP`` is in UTC (the session is);
    ``DATETIME`` has no zone and stays naive. ``BIT`` arrives as bytes; ``JSON`` as text."""
    if value is None:
        return None
    if col.type == "boolean":
        return bool(cast("int", value))
    if col.db_type == "bit" and isinstance(value, bytes):
        return int.from_bytes(value, "big")
    if col.db_type == "timestamp" and isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    if col.type == "json" and isinstance(value, (str, bytes)):
        return json.loads(value)
    return value


def placeholders(sql: str) -> list[tuple[int, int]]:
    """The ``(start, end)`` of every ``?`` placeholder, in order. The classifier already parsed
    the text; a text it refused never gets here."""
    try:
        tokens = sqlglot.tokenize(sql, read="mysql")
    except SqlglotError as exc:
        raise QueryRefusedError("the statement does not parse") from exc
    return [(t.start, t.end) for t in tokens if t.token_type == TokenType.PLACEHOLDER]


def bind(sql: str, params: Sequence[Scalar], escape: Callable[[object], str]) -> str:
    """``sql`` with each ``?`` replaced by its parameter as a literal ``escape`` makes."""
    found = placeholders(sql)
    if len(params) != len(found):
        raise QueryFailedError(
            f"{len(params)} parameters for {len(found)} placeholders", sqlstate="07001"
        )
    out = sql
    for (start, end), value in reversed(list(zip(found, params, strict=True))):
        if isinstance(value, bool):
            literal = "TRUE" if value else "FALSE"
        else:
            literal = escape(value)
        out = out[:start] + literal + out[end + 1 :]
    return out


def tagged(sql: str, tag: str) -> str:
    """``sql`` with the query's tag in a leading comment, which the server's process list and
    slow log show the customer's DBA. Anything that could end the comment is dropped."""
    clean = tag
    while (shorter := _PLAIN_TAG.sub("", clean)) != clean:
        clean = shorter
    return f"/* {clean[:TAG_BYTES]} */ {sql}"


class _Cursor:
    def __init__(self, columns: list[Column], cursor: Any, limit: int) -> None:
        self._columns = columns
        self._cursor = cursor
        self._limit = limit

    @property
    def columns(self) -> Sequence[Column]:
        return self._columns

    async def rows(self) -> AsyncIterator[Sequence[object]]:
        left = self._limit
        while left > 0:
            batch = cast(
                "Sequence[Sequence[object]]", await self._cursor.fetchmany(min(left, BATCH))
            )
            if not batch:
                return
            left -= len(batch)
            for record in batch:
                yield [_value(v, c) for v, c in zip(record, self._columns, strict=True)]


def _errno(exc: BaseException) -> int | None:
    args = cast("tuple[object, ...]", getattr(exc, "args", ()))
    return args[0] if args and isinstance(args[0], int) else None


def _failure(exc: Exception) -> Exception:
    """The connector's error for a driver error. The message names the error class and number
    only: the server's own text quotes names and values, and the gateway logs this message."""
    name = type(exc).__name__
    if isinstance(exc, errors.Error):
        errno = _errno(exc)
        if errno == TIMEOUT_ERRNO:
            return TimeoutError("max_execution_time")
        if errno is None or errno in CONNECTION_ERRNOS or isinstance(exc, errors.InterfaceError):
            return UpstreamUnavailableError(f"the database ended the read: {name} {errno}")
        return QueryFailedError(f"{name} {errno}", sqlstate=SQLSTATE.get(errno))
    return UpstreamUnavailableError(f"the connection failed: {name}")


class MySqlConnector:
    """A :class:`ssc_datagw.connectors.Connector` for one MySQL connection. ``classify``
    replaces the classifier (tests that prove what the database refuses by itself)."""

    def __init__(
        self,
        target: MySqlTarget,
        *,
        classify: Callable[[str], str | None] = mysql_refusal,
        connect_seconds: float = CONNECT_SECONDS,
        warmup: Warmup | None = None,
    ) -> None:
        self._target = target
        self._tls = tls_context(target.ca)
        self._classify = classify
        self._connect_seconds = connect_seconds
        self._warmup = warmup or Warmup()
        self._ending: set[asyncio.Task[None]] = set()

    async def _connect(self) -> Any:
        return await connect_with_warmup(
            self._connect_once, connect_seconds=self._connect_seconds, warmup=self._warmup
        )

    async def _connect_once(self, seconds: float) -> Any:
        """The driver folds its own connect timeout into error 2003, so the deadline is held
        here, where a time-out stays a ``TimeoutError`` for the warm-up retry."""
        t = self._target
        try:
            async with asyncio.timeout(seconds):
                return await _CONNECT(
                    host=t.host,
                    port=t.port,
                    user=t.user,
                    password=t.password.get_secret_value(),
                    db=t.database,
                    ssl=self._tls,
                    connect_timeout=max(seconds, 1.0) + 1.0,
                    program_name=PROGRAM_NAME,
                    cursor_cls=SSCursor,
                    autocommit=False,
                    charset="utf8mb4",
                )
        except TimeoutError:
            raise
        except (OSError, errors.Error) as exc:
            raise UpstreamUnavailableError(
                f"cannot connect: {type(exc).__name__} {_errno(exc) or ''}".rstrip()
            ) from None

    async def _kill(self, thread_id: int) -> None:
        """Stop the statement running on session ``thread_id`` from a second session."""
        try:
            async with asyncio.timeout(KILL_SECONDS):
                conn = await self._connect()
                try:
                    async with conn.cursor() as cursor:
                        await cursor.execute(KILL.format(id=int(thread_id)))
                finally:
                    conn.close()
        except (UpstreamUnavailableError, TimeoutError, errors.Error) as exc:
            log.warning("could not stop session %s: %s", thread_id, type(exc).__name__)

    async def _end(self, thread_id: int) -> None:
        """Run :meth:`_kill` to the end even if the caller is cancelled again."""
        task = asyncio.create_task(self._kill(thread_id))
        self._ending.add(task)
        task.add_done_callback(self._ending.discard)
        await asyncio.shield(task)

    @asynccontextmanager
    async def open(self, query: Query) -> AsyncGenerator[_Cursor]:
        reason = self._classify(query.sql)
        if reason is not None:
            raise QueryRefusedError(reason)
        async with self._read(query) as cursor:
            yield cursor

    async def describe(self, *, schemas: Sequence[str] | None, timeout_ms: int) -> list[Table]:
        """``information_schema.columns`` of the connection's database through :meth:`_read`;
        ``schemas`` does not apply (the database is the connection's)."""
        del schemas
        query = Query(
            sql=DESCRIBE,
            params=(),
            max_rows=MAX_TABLES * MAX_COLUMNS,
            timeout_ms=timeout_ms,
            tag=DESCRIBE_TAG,
        )
        async with asyncio.timeout(max(1, timeout_ms) / 1000):
            async with self._read(query) as cursor:
                rows = [row async for row in cursor.rows()]
        return grouped(
            (_text(table), described(_text(name), _text(kind), _text(full)))
            for table, name, kind, full in rows
        )

    @asynccontextmanager
    async def _read(self, query: Query) -> AsyncGenerator[_Cursor]:
        """One statement in a checked read-only session, bound and tagged."""
        conn = await self._connect()
        thread_id: int | None = None
        try:
            timeout_ms = max(1, query.timeout_ms)
            cursor = conn.cursor()
            await cursor.execute(SESSION.format(timeout_ms=timeout_ms))
            await cursor.execute(BEGIN)
            await cursor.execute(READBACK)
            record = cast("Sequence[object] | None", await cursor.fetchone())
            await cursor.fetchall()
            if record is None:
                raise UpstreamUnavailableError("the session could not be read back")
            row = Readback(*(cast("Any", v) for v in record))
            problem = session_problem(conn.thread_id(), timeout_ms, row)
            if problem is not None:
                raise UpstreamUnavailableError(problem)
            thread_id = row.id
            sql = tagged(bind(query.sql, query.params, conn.escape), query.tag)
            await cursor.execute(sql)
            columns = [
                column(FieldInfo(f.name, f.type_code, f.charsetnr, f.flags, f.length))
                for f in cursor._result.fields or ()  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
            ]
            yield _Cursor(columns, cursor, query.max_rows + 1)
        except asyncio.CancelledError:
            if thread_id is not None:
                await self._end(thread_id)
            raise
        except (errors.Error, OSError) as exc:
            raise _failure(exc) from None
        finally:
            conn.close()


__all__ = [
    "MySqlConnector",
    "MySqlTarget",
    "Readback",
    "bind",
    "column",
    "described",
    "placeholders",
    "session_problem",
    "tagged",
]
