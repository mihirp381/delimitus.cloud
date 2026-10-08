"""The SQL Server connector (GA-5): one read on SQL Server 2022, over TLS, as the read-only
login ``sqlserver_setup.sql`` makes.

The shape follows :mod:`ssc_datagw.postgres`. Each read opens its own connection, settles the
session (``READ COMMITTED``, ``LOCK_TIMEOUT`` at ``timeout_ms``, ``ARITHABORT``, ``DATEFORMAT
ymd``), reads it back and refuses to run when the login can do more than read: SQL Server has no
read-only session, so the login's grants and the classifier are the guard. The statement is
sent through ``sp_executesql`` with its ``?`` placeholders bound server-side, and its rows are
fetched 500 at a time.

The driver, pytds, is synchronous: every call runs in a worker thread. The connector owns the
TCP socket under it, so a read the gateway cancels, or one that passes ``timeout_ms``, is stopped
by shutting that socket down: SQL Server ends the request of a client that went away (within
seconds, ``tests/test_sqlserver.py``). ``KILL`` would need ``ALTER ANY CONNECTION``, a server-wide
right to end anyone's session, which the login does not get.

TLS is ``verify-ca`` with the pasted CA, which is required: the chain must lead to it and the
name is not checked, the rule of :mod:`ssc_datagw.tls`. pytds speaks TLS 1.2 only, and refuses a
server that offers no encryption.
"""

import asyncio
import functools
import logging
import os
import socket
import tempfile
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Generator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, fields
from typing import Any, Final, Literal, cast

import pytds  # pyright: ignore[reportMissingTypeStubs]
import sqlglot
from OpenSSL import SSL
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pytds import tds_base  # pyright: ignore[reportMissingTypeStubs]
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from ssc_datagw.classify import tsql_refusal
from ssc_datagw.connectors import (
    Column,
    Query,
    QueryFailedError,
    QueryRefusedError,
    Scalar,
    UpstreamUnavailableError,
)
from ssc_datagw.tls import tls_context
from ssc_datagw.warmup import CONNECT_SECONDS, Warmup, connect_with_warmup

log = logging.getLogger(__name__)
logging.getLogger("pytds").setLevel(logging.WARNING)
"""pytds logs the statement's text at INFO; the gateway never logs it."""

STOP_SECONDS: Final = 5.0
"""How long a stopped read waits for its worker thread to end."""
BATCH: Final = 500
APPLICATION_NAME: Final = "ssc-datagw"
NAME_CHARS: Final = 128
TAG_CHARS: Final = 128
SESSION: Final = (
    "SET TRANSACTION ISOLATION LEVEL READ COMMITTED; SET LOCK_TIMEOUT {timeout_ms}; "
    "SET ARITHABORT ON; SET DATEFORMAT ymd;"
)
_WIDER: Final = "('INSERT', 'UPDATE', 'DELETE', 'ALTER', 'CONTROL', 'EXECUTE')"
READBACK: Final = f"""
SELECT @@SPID AS spid,
       DB_NAME() AS database_name,
       IS_SRVROLEMEMBER('sysadmin') AS sysadmin,
       IS_MEMBER('db_owner') AS db_owner,
       IS_MEMBER('db_datawriter') AS datawriter,
       IS_MEMBER('db_ddladmin') AS ddladmin,
       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT') AS can_insert,
       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE') AS can_update,
       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE') AS can_delete,
       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER') AS can_alter,
       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CONTROL') AS can_control,
       HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'EXECUTE') AS can_execute,
       (SELECT COUNT(*) FROM sys.database_permissions
        WHERE grantee_principal_id = DATABASE_PRINCIPAL_ID() AND state IN ('G', 'W')
          AND permission_name IN {_WIDER}) AS direct_grants
"""  # noqa: S608  (built from constants only)
"""What the session is and what the login may do. ``HAS_PERMS_BY_NAME`` on the database does not
see a grant on one schema or object, so those made to the login's user are counted too."""
LOCK_TIMEOUT: Final = 1222
SQLSTATE: Final = {
    208: "42P01",  # no such object
    4104: "42P01",  # a multi-part name that does not bind
    207: "42703",  # no such column
    209: "42702",  # an ambiguous column
    102: "42601",  # syntax
    156: "42601",  # syntax near a keyword
    195: "42883",  # not a recognised function
    4121: "42883",  # no such function
    229: "42501",  # permission denied on an object
    230: "42501",  # permission denied on a column
    297: "42501",  # the user may not perform this action
    916: "42501",  # the login may not use the database
    262: "42501",  # permission denied in the database
    8134: "22012",  # division by zero
    245: "22P02",  # a conversion failed
    8114: "22P02",  # a conversion to a type failed
    241: "22P02",  # a date or time conversion failed
    242: "22P02",  # a date or time out of range
    8115: "22003",  # arithmetic overflow
    220: "22003",  # arithmetic overflow of an integer
    512: "21000",  # a subquery returned more than one value
    1205: "40P01",  # a deadlock victim
}
"""The SQLSTATE each SQL Server error number stands for; another number has none."""
UNAVAILABLE: Final = frozenset({18456, 4060, 701, 1204, 17809})
"""Error numbers that mean the database, not the statement: login failed, cannot open the
database, out of memory, out of locks, out of connections."""
PORTABLE: Final = {
    50: ("boolean", "bit"),
    48: ("integer", "tinyint"),
    52: ("integer", "smallint"),
    56: ("integer", "int"),
    127: ("integer", "bigint"),
    59: ("float", "real"),
    62: ("float", "float"),
    106: ("decimal", "decimal"),
    108: ("decimal", "numeric"),
    60: ("decimal", "money"),
    122: ("decimal", "smallmoney"),
    167: ("string", "varchar"),
    231: ("string", "nvarchar"),
    35: ("string", "text"),
    99: ("string", "ntext"),
    241: ("string", "xml"),
    36: ("uuid", "uniqueidentifier"),
    40: ("date", "date"),
    41: ("time", "time"),
    61: ("timestamp", "datetime"),
    42: ("timestamp", "datetime2"),
    58: ("timestamp", "smalldatetime"),
    43: ("timestamp", "datetimeoffset"),
    165: ("bytes", "varbinary"),
    34: ("bytes", "image"),
    98: ("string", "sql_variant"),
}
"""Portable column types by the type id pytds reports. pytds reports ``char`` as ``varchar``,
``nchar`` as ``nvarchar``, ``binary`` as ``varbinary`` and ``numeric`` as ``decimal``, and
``nvarchar(max)`` with the id of ``ntext`` (told apart by its serializer)."""
_UDT: Final = 0
_CONNECT: Final = cast("Callable[..., Any]", pytds.connect)  # pyright: ignore[reportUnknownMemberType]
_DIAL: Final = cast("Callable[..., socket.socket]", socket.create_connection)
_DRIVER_ERRORS: Final = (tds_base.Error, SSL.Error, OSError, ValueError)


class SqlServerTarget(BaseModel):
    """Where one connection points and the login it uses. ``ca`` is the server's CA
    certificate in PEM, pasted by the customer, and required: the chain must lead to it and the
    name is not checked (``verify-ca``). There is no way to turn TLS or its check off."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: Literal["sqlserver"] = "sqlserver"
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=1433, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=NAME_CHARS)
    user: str = Field(min_length=1, max_length=NAME_CHARS)
    password: SecretStr
    ca: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class Readback:
    """The row :data:`READBACK` answers: what the session really is. A permission check that
    answers ``NULL`` counts as granted."""

    spid: int
    database_name: str | None
    sysadmin: int | None
    db_owner: int | None
    datawriter: int | None
    ddladmin: int | None
    can_insert: int | None
    can_update: int | None
    can_delete: int | None
    can_alter: int | None
    can_control: int | None
    can_execute: int | None
    direct_grants: int | None


def _on(value: int | None) -> bool:
    return value is None or value != 0


def session_problem(database: str, row: Readback) -> str | None:
    """Why this session must not run the read, or ``None``. ``database`` is the connection's."""
    checks = (
        (
            (row.database_name or "").casefold() != database.casefold(),
            "the session is not in the connection's database",
        ),
        (_on(row.sysadmin), "the login is a sysadmin"),
        (_on(row.db_owner) or _on(row.can_control), "the login owns or controls the database"),
        (
            _on(row.datawriter)
            or _on(row.can_insert)
            or _on(row.can_update)
            or _on(row.can_delete),
            "the login may write",
        ),
        (_on(row.ddladmin) or _on(row.can_alter), "the login may create or alter objects"),
        (_on(row.can_execute), "the login may execute procedures"),
        (_on(row.direct_grants), "the login holds a grant beyond SELECT"),
    )
    return next((why for failed, why in checks if failed), None)


def column(name: str, type_id: int, serializer: object) -> Column:
    """The portable column for one result column (``docs/contracts/data-gateway.md``,
    Response). A CLR type (``hierarchyid``, ``geography``, ``geometry``) arrives as bytes."""
    if type_id == _UDT:
        return Column(name, "bytes", str(getattr(serializer, "type_name", "udt")).lower())
    if type_id == 99 and type(serializer).__name__.startswith("NVarCharMax"):  # noqa: PLR2004
        return Column(name, "string", "nvarchar")
    portable, db_type = PORTABLE.get(type_id, ("string", f"type {type_id}"))
    return Column(name, portable, db_type)


def placeholders(sql: str) -> list[tuple[int, int]]:
    """The ``(start, end)`` of every ``?`` placeholder, in order. A ``?`` in a string, a
    bracketed name or a comment is not one."""
    try:
        tokens = sqlglot.tokenize(sql, read="tsql")
    except SqlglotError as exc:
        raise QueryRefusedError("the statement does not parse") from exc
    return [(t.start, t.end) for t in tokens if t.token_type == TokenType.PLACEHOLDER]


def bind(sql: str, params: Sequence[Scalar]) -> tuple[str, tuple[Scalar, ...]]:
    """``sql`` as pytds takes it, and its parameters: each ``?`` becomes ``%s``, which pytds
    turns into ``@P1``, ``@P2``, ... for ``sp_executesql``, and every other ``%`` is doubled,
    since pytds formats the text with ``%``."""
    found = placeholders(sql)
    if len(params) != len(found):
        raise QueryFailedError(
            f"{len(params)} parameters for {len(found)} placeholders", sqlstate="07001"
        )
    parts: list[str] = []
    at = 0
    for start, end in found:
        parts += [sql[at:start].replace("%", "%%"), "%s"]
        at = end + 1
    parts.append(sql[at:].replace("%", "%%"))
    return "".join(parts), tuple(params)


def tagged(sql: str, tag: str) -> str:
    """``sql`` (already bound) with the query's tag in a leading comment, which the customer's
    DBA sees in ``sys.dm_exec_sql_text``. T-SQL comments nest, so both ``/*`` and ``*/`` are
    dropped until none is left."""
    clean = tag
    while (shorter := clean.replace("*/", "").replace("/*", "")) != clean:
        clean = shorter
    return f"/* {clean[:TAG_CHARS].replace('%', '%%')} */ {sql}"


class _Line:
    """The TCP socket under one connection, which the event loop may shut down while a worker
    thread is blocked reading it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._dropped = False

    def attach(self, sock: socket.socket) -> None:
        with self._lock:
            if self._dropped:
                sock.close()
                raise ConnectionAbortedError("the read was stopped")
            self._sock = sock

    def drop(self) -> None:
        """Shut the socket down: a blocked read returns, and the server ends the request."""
        with self._lock:
            self._dropped = True
            if self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def close(self) -> None:
        self.drop()
        with self._lock:
            if self._sock is not None:
                self._sock.close()


type _Fetch = Callable[[int], Awaitable[Sequence[Sequence[object]]]]


class _Cursor:
    def __init__(self, columns: list[Column], fetch: _Fetch, limit: int, deadline: float) -> None:
        self._columns = columns
        self._fetch = fetch
        self._limit = limit
        self._deadline = deadline

    @property
    def columns(self) -> Sequence[Column]:
        return self._columns

    async def rows(self) -> AsyncIterator[Sequence[object]]:
        left = self._limit
        while left > 0:
            async with asyncio.timeout_at(self._deadline):
                batch = await self._fetch(min(left, BATCH))
            if not batch:
                return
            left -= len(batch)
            for record in batch:
                yield list(record)


def _failure(exc: BaseException) -> Exception:
    """The connector's error for a driver error. The message names the error class and number
    only: the server's own text quotes names and values, and the gateway logs this message."""
    name = type(exc).__name__
    if isinstance(exc, TimeoutError):
        return TimeoutError("timeout_ms")
    if isinstance(exc, tds_base.DatabaseError):
        number = cast("int", getattr(exc, "number", 0))
        if number == LOCK_TIMEOUT:
            return TimeoutError("LOCK_TIMEOUT")
        if number in UNAVAILABLE:
            return UpstreamUnavailableError(f"the database ended the read: {name} {number}")
        return QueryFailedError(f"{name} {number}", sqlstate=SQLSTATE.get(number))
    return UpstreamUnavailableError(f"the connection failed: {name}")


def _close(sock: socket.socket | None) -> None:
    if sock is not None:
        sock.close()


@contextmanager
def _ca_file(ca: str) -> Generator[str]:
    """The CA as a file only this process may read, as pytds wants it, removed afterwards."""
    handle, path = tempfile.mkstemp(prefix="ssc-ca-", suffix=".pem")
    try:
        with os.fdopen(handle, "w") as out:
            out.write(ca)
        yield path
    finally:
        os.unlink(path)


class SqlServerConnector:
    """A :class:`ssc_datagw.connectors.Connector` for one SQL Server connection. ``classify``
    replaces the classifier (tests that prove what the database refuses by itself)."""

    def __init__(
        self,
        target: SqlServerTarget,
        *,
        classify: Callable[[str], str | None] = tsql_refusal,
        connect_seconds: float = CONNECT_SECONDS,
        warmup: Warmup | None = None,
    ) -> None:
        tls_context(target.ca)
        self._target = target
        self._classify = classify
        self._connect_seconds = connect_seconds
        self._warmup = warmup or Warmup()
        self._ending: set[asyncio.Future[Any]] = set()

    async def _call[T](self, line: _Line, work: Callable[[], T]) -> T:
        """``work()`` in a worker thread. When the caller is cancelled (the gateway, or a
        deadline), the line is dropped and the thread waited for before the cancel goes on."""
        task = asyncio.ensure_future(asyncio.to_thread(work))
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            line.drop()
            await self._settle(task)
            raise
        return task.result()

    async def _settle(self, task: asyncio.Future[Any]) -> None:
        """Wait for a stopped worker thread, at most :data:`STOP_SECONDS`, even if the caller
        is cancelled again."""
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        waiter = asyncio.ensure_future(asyncio.wait({task}, timeout=STOP_SECONDS))
        self._ending.add(waiter)
        waiter.add_done_callback(self._ending.discard)
        await asyncio.shield(waiter)
        if not task.done():
            log.warning("a stopped read's thread did not end in %s s", STOP_SECONDS)

    async def _connect(self, line: _Line, timeout_ms: int) -> Any:
        async def once(seconds: float) -> Any:
            return await self._call(line, lambda: self._connect_once(line, seconds, timeout_ms))

        return await connect_with_warmup(
            once, connect_seconds=self._connect_seconds, warmup=self._warmup
        )

    def _connect_once(self, line: _Line, seconds: float, timeout_ms: int) -> Any:
        """One connect and login, in a worker thread. A time-out stays a ``TimeoutError`` for
        the warm-up retry; pytds turns a login error it would retry (such as 4060) into a
        ``TimeoutError`` whose cause is that error, which is unavailable at once."""
        t = self._target
        sock: socket.socket | None = None
        try:
            sock = _DIAL((t.host, t.port), seconds)
            line.attach(sock)
            with _ca_file(t.ca) as cafile:
                conn = _CONNECT(
                    dsn=t.host,
                    port=t.port,
                    database=t.database,
                    user=t.user,
                    password=t.password.get_secret_value(),
                    cafile=cafile,
                    validate_host=False,
                    login_timeout=seconds,
                    timeout=timeout_ms / 1000 + 1,
                    appname=APPLICATION_NAME,
                    autocommit=True,
                    readonly=True,
                    disable_connect_retry=True,
                    sock=sock,
                )
        except TimeoutError as exc:
            _close(sock)
            cause = exc.__cause__
            if isinstance(cause, tds_base.DatabaseError):
                number = cast("int", getattr(cause, "number", 0))
                raise UpstreamUnavailableError(
                    f"cannot connect: {type(cause).__name__} {number}"
                ) from None
            raise TimeoutError("connect") from None
        except _DRIVER_ERRORS as exc:
            _close(sock)
            raise UpstreamUnavailableError(f"cannot connect: {type(exc).__name__}") from None
        if sock.fileno() == -1:
            conn.close()
            raise UpstreamUnavailableError(
                "the server redirected the connection: point the connection at the server's "
                "own address"
            )
        return conn

    def _settle_session(self, conn: Any, timeout_ms: int) -> tuple[Any, Readback]:
        cursor = conn.cursor()
        cursor.execute(SESSION.format(timeout_ms=int(timeout_ms)))
        cursor.execute(READBACK)
        record = cast("Sequence[Any] | None", cursor.fetchone())
        if record is None:
            raise UpstreamUnavailableError("the session could not be read back")
        return cursor, Readback(*record[: len(fields(Readback))])

    @staticmethod
    def _execute(cursor: Any, sql: str, params: tuple[Scalar, ...]) -> list[Column]:
        cursor.execute(sql, params)
        found = cursor._session.res_info  # noqa: SLF001
        if found is None:
            return []
        return [
            column(c.column_name, c.serializer.get_typeid(), c.serializer) for c in found.columns
        ]

    @asynccontextmanager
    async def open(self, query: Query) -> AsyncGenerator[_Cursor]:
        reason = self._classify(query.sql)
        if reason is not None:
            raise QueryRefusedError(reason)
        timeout_ms = max(1, query.timeout_ms)
        sql, params = bind(query.sql, query.params)
        sql = tagged(sql, query.tag)
        line = _Line()
        try:
            conn = await self._connect(line, timeout_ms)
            cursor, row = await self._call(line, lambda: self._settle_session(conn, timeout_ms))
            problem = session_problem(self._target.database, row)
            if problem is not None:
                raise UpstreamUnavailableError(problem)
            deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
            async with asyncio.timeout_at(deadline):
                columns = await self._call(line, lambda: self._execute(cursor, sql, params))

            async def fetch(size: int) -> Sequence[Sequence[object]]:
                return cast(
                    "Sequence[Sequence[object]]",
                    await self._call(line, functools.partial(cursor.fetchmany, size)),
                )

            yield _Cursor(columns, fetch, query.max_rows + 1, deadline)
        except TimeoutError, UpstreamUnavailableError, QueryFailedError:
            raise
        except _DRIVER_ERRORS as exc:
            raise _failure(exc) from None
        finally:
            line.close()


__all__ = [
    "SqlServerConnector",
    "SqlServerTarget",
    "Readback",
    "bind",
    "column",
    "placeholders",
    "session_problem",
    "tagged",
]
