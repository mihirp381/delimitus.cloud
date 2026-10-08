"""What the data gateway asks of a database driver (SSC-050). The Postgres connector is
:mod:`ssc_datagw.postgres` (SSC-051).

A connector also describes its source (GA-5.8): :meth:`Connector.describe` answers the tables
the credential may read and their columns, at most :data:`MAX_TABLES` tables of at most
:data:`MAX_COLUMNS` columns, typed as a read types them.

A connector opens one read for a :class:`Query` and yields its rows; the gateway counts rows and
bytes, stops reading at a cap, and leaves the context, which ends the read on the database. The
gateway runs the read under its own deadline and cancels it when the kill switch fires, so a
connector must let ``CancelledError`` through and stop the statement when it does.
"""

import base64
import json
import math
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Final, Protocol, cast
from uuid import UUID

type Scalar = str | int | float | bool | None
type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


PORTABLE_TYPES: Final = frozenset(
    {
        "string",
        "integer",
        "float",
        "decimal",
        "boolean",
        "date",
        "time",
        "timestamp",
        "interval",
        "bytes",
        "uuid",
        "json",
        "array",
    }
)
"""Every ``Column.type`` a connector may answer (``docs/contracts/data-gateway.md``, Response).
A source type that fits none is ``string``."""
MAX_TABLES: Final = 500
MAX_COLUMNS: Final = 500
"""What :meth:`Connector.describe` answers at most: tables, and columns of each table."""
DESCRIBE_TAG: Final = "ssc:describe"
"""The tag a description's statement carries, as a read carries its query's."""


@dataclass(frozen=True, slots=True)
class Column:
    """``type`` is one of :data:`PORTABLE_TYPES` (``string``, ``integer``, ``decimal``,
    ``timestamp``, ...); ``db_type`` is the database's own name for it."""

    name: str
    type: str
    db_type: str


@dataclass(frozen=True, slots=True)
class Table:
    """One table (or view, sheet, object) a connector describes. ``name`` is how a read names
    it; ``columns`` are typed as a read types them, none when only a read can tell."""

    name: str
    columns: Sequence[Column]


@dataclass(frozen=True, slots=True, kw_only=True)
class Query:
    """One read. ``max_rows`` is the cap; a connector fetches at most one row more, which is how
    the gateway sees the result was cut. ``tag`` (``ssc:<app>:<env>:<request id>``) names the
    query to the customer's DBA."""

    sql: str
    params: tuple[Scalar, ...]
    max_rows: int
    timeout_ms: int
    tag: str


class Cursor(Protocol):
    @property
    def columns(self) -> Sequence[Column]: ...

    def rows(self) -> AsyncIterator[Sequence[object]]: ...


class Connector(Protocol):
    def open(self, query: Query) -> AbstractAsyncContextManager[Cursor]: ...

    async def describe(self, *, schemas: Sequence[str] | None, timeout_ms: int) -> Sequence[Table]:
        """The tables the credential may read, in ``schemas`` when the kind has schemas and
        they are given (``None`` or empty: every one). The whole call ends at ``timeout_ms``
        with ``TimeoutError``; a cancel goes through; the source's errors are the read's."""
        ...


def grouped(rows: Iterable[tuple[str, Column]]) -> list[Table]:
    """``(table, column)`` pairs, a table's columns together and in order, as tables: at most
    :data:`MAX_TABLES` of at most :data:`MAX_COLUMNS` columns each."""
    tables: dict[str, list[Column]] = {}
    for name, column in rows:
        columns = tables.get(name)
        if columns is None:
            if len(tables) >= MAX_TABLES:
                break
            columns = tables[name] = []
        if len(columns) < MAX_COLUMNS:
            columns.append(column)
    return [Table(name, columns) for name, columns in tables.items()]


class QueryRefusedError(Exception):
    """The statement is not a plain read (the connector's classifier). The message is fixed."""


class QueryFailedError(Exception):
    """The database refused the statement. ``sqlstate`` goes back to the app; the message only
    to the log."""

    def __init__(self, message: str, *, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class UpstreamUnavailableError(Exception):
    """The database could not be reached or ran out of connections."""


def _duration(value: timedelta) -> str:
    seconds = Decimal(value.days * 86_400 + value.seconds) + Decimal(value.microseconds) / 10**6
    return f"PT{seconds.normalize():f}S"


def jsonable(value: object) -> JsonValue:  # noqa: PLR0911  (one return per type)
    """``value`` as JSON without losing precision: a ``Decimal`` stays a string, never a float;
    times are ISO 8601, bytes base64, a non-finite float its name."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return _duration(value)
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        items = cast("Mapping[object, object]", value).items()
        return {str(k): jsonable(v) for k, v in items}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in cast("Sequence[object]", value)]
    return str(value)


def encoded_size(row: list[JsonValue]) -> int:
    """The row's bytes in the response, with the comma that follows it."""
    return len(json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode()) + 1
