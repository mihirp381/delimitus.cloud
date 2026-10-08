"""What every connector promises (GA-5), as one suite each connector's tests run.

A connector's test module builds a :class:`Subject` against its real source (a database in a
container, a sandbox account, or a contract fake that replays the source's recorded answers)
and runs each of :data:`CHECKS` through :func:`conform`, which also reads every log line the
check wrote and fails if the credential appears in one. The promises, from
``docs/contracts/data-gateway.md`` ("Connectors by kind"):

- a read answers typed columns, every type portable, and at most ``max_rows`` plus one rows;
- a write, or anything that is not one read, is refused before it reaches the source;
- what the source refuses is a query failure, what it cannot reach is unavailable;
- a read past ``timeout_ms`` ends with ``TimeoutError``, a cancelled read stops;
- the credential appears in no repr, no error and no log line;
- a parameter binds;
- a description names the tables, every column type portable, within the caps (GA-5.8).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ssc_datagw.connectors import (
    MAX_COLUMNS,
    MAX_TABLES,
    PORTABLE_TYPES,
    Column,
    Connector,
    Query,
    QueryFailedError,
    QueryRefusedError,
    Scalar,
    UpstreamUnavailableError,
    jsonable,
)

SLOW_SECONDS = 15.0
"""How long a check waits for a timed-out or cancelled read to end."""


@dataclass(frozen=True, kw_only=True)
class Subject:
    """One connector against one source.

    ``read`` returns at least three rows in a fixed order; ``read_columns`` names some of its
    columns and their portable types, ``first_row`` is its first row as JSON. Each of ``writes``
    must be refused before the source. ``bad`` is a read the source itself refuses, ``slow`` a
    read that runs for at least :data:`SLOW_SECONDS` unless stopped. ``unreachable`` is the same
    connector pointed where nothing answers, with the same ``credential``, which must appear in
    no repr of ``secrets``, no error and no log line. ``params`` is a read with one placeholder,
    its parameters, and the first row it answers, as JSON. ``table`` is a table the
    connector's description names; with ``describes`` false the description is empty."""

    connector: Connector
    unreachable: Connector
    credential: str
    secrets: Sequence[object]
    read: str
    read_columns: Mapping[str, str]
    first_row: Sequence[object]
    writes: Sequence[str]
    bad: str
    slow: str
    params: tuple[str, Sequence[Scalar], Sequence[object]] | None = None
    table: str = ""
    describes: bool = True


def ask(
    sql: str, *params: Scalar, max_rows: int = 100, timeout_ms: int = 5_000, tag: str = "ssc:t"
) -> Query:
    return Query(sql=sql, params=params, max_rows=max_rows, timeout_ms=timeout_ms, tag=tag)


async def read(connector: Connector, query: Query) -> tuple[list[Column], list[Sequence[object]]]:
    async with connector.open(query) as cursor:
        return list(cursor.columns), [row async for row in cursor.rows()]


def _hidden(credential: str, *texts: object) -> None:
    for text in texts:
        shown = text if isinstance(text, str) else f"{text!r} {text!s}"
        assert credential not in shown, f"the credential appears in {type(text).__name__}"


async def check_a_read_returns_typed_columns_and_one_row_past_the_cap(s: Subject) -> None:
    columns, rows = await read(s.connector, ask(s.read, max_rows=2))
    assert len(rows) == 3, "a cap of 2 reads 2 rows and the one that shows the cut"
    assert all(c.type in PORTABLE_TYPES for c in columns), [c.type for c in columns]
    assert all(c.db_type for c in columns), "every column names the source's own type"
    types = {c.name: c.type for c in columns}
    for name, portable in s.read_columns.items():
        assert types.get(name) == portable, (name, types.get(name), portable)
    for row in rows:
        assert len(row) == len(columns)
        jsonable(list(row))
    assert jsonable(list(rows[0])) == list(s.first_row)


async def check_a_read_of_fewer_rows_than_the_cap_returns_them_all(s: Subject) -> None:
    _, rows = await read(s.connector, ask(s.read, max_rows=10_000))
    assert len(rows) >= 3
    _, again = await read(s.connector, ask(s.read, max_rows=len(rows)))
    assert len(again) == len(rows), "a cap equal to the row count cuts nothing and adds nothing"


async def check_a_write_is_refused_before_the_source(s: Subject) -> None:
    for sql in s.writes:
        for connector in (s.connector, s.unreachable):
            try:
                await read(connector, ask(sql))
            except QueryRefusedError as exc:
                _hidden(s.credential, exc)
            else:
                raise AssertionError(f"not refused: {sql}")


async def check_what_the_source_refuses_is_a_query_failure(s: Subject) -> None:
    try:
        await read(s.connector, ask(s.bad))
    except QueryFailedError as exc:
        _hidden(s.credential, exc)
    else:
        raise AssertionError("the source accepted the bad read")


async def check_an_unreachable_source_is_unavailable(s: Subject) -> None:
    try:
        async with asyncio.timeout(SLOW_SECONDS):
            await read(s.unreachable, ask(s.read))
    except UpstreamUnavailableError as exc:
        _hidden(s.credential, exc)
    else:
        raise AssertionError("the unreachable source answered")


async def check_a_read_past_its_timeout_times_out(s: Subject) -> None:
    try:
        async with asyncio.timeout(SLOW_SECONDS):
            await read(s.connector, ask(s.slow, timeout_ms=300))
    except TimeoutError:
        return
    raise AssertionError("the slow read did not time out")


async def check_a_cancelled_read_stops(s: Subject) -> None:
    task = asyncio.create_task(read(s.connector, ask(s.slow, timeout_ms=60_000)))
    await asyncio.sleep(1.0)
    task.cancel()
    try:
        async with asyncio.timeout(SLOW_SECONDS):
            await task
    except asyncio.CancelledError:
        return
    raise AssertionError("the cancelled read finished instead of stopping")


async def check_the_credential_is_hidden(s: Subject) -> None:
    _hidden(s.credential, *s.secrets, s.connector, s.unreachable)


async def check_a_parameter_binds(s: Subject) -> None:
    if s.params is None:
        return
    sql, params, first = s.params
    _, rows = await read(s.connector, ask(sql, *params))
    assert rows, "the parameter matched nothing"
    assert jsonable(list(rows[0])) == list(first)


async def check_a_description_names_the_tables(s: Subject) -> None:
    tables = list(await s.connector.describe(schemas=None, timeout_ms=10_000))
    if not s.describes:
        assert tables == [], "this kind names no tables"
        return
    assert len(tables) <= MAX_TABLES
    assert s.table in [t.name for t in tables], (s.table, [t.name for t in tables])
    for table in tables:
        assert len(table.columns) <= MAX_COLUMNS
        assert all(c.type in PORTABLE_TYPES for c in table.columns), table
        assert all(c.db_type for c in table.columns), table
    try:
        async with asyncio.timeout(SLOW_SECONDS):
            await s.unreachable.describe(schemas=None, timeout_ms=10_000)
    except UpstreamUnavailableError as exc:
        _hidden(s.credential, exc)
    else:
        raise AssertionError("the unreachable source answered a description")


CHECKS: Sequence[Callable[[Subject], Awaitable[None]]] = (
    check_a_read_returns_typed_columns_and_one_row_past_the_cap,
    check_a_read_of_fewer_rows_than_the_cap_returns_them_all,
    check_a_write_is_refused_before_the_source,
    check_what_the_source_refuses_is_a_query_failure,
    check_an_unreachable_source_is_unavailable,
    check_a_read_past_its_timeout_times_out,
    check_a_cancelled_read_stops,
    check_the_credential_is_hidden,
    check_a_parameter_binds,
    check_a_description_names_the_tables,
)


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@contextmanager
def _every_log_line() -> Iterator[list[str]]:
    root = logging.getLogger()
    handler = _Lines()
    handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler.lines
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


async def conform(check: Callable[[Subject], Awaitable[Any]], subject: Subject) -> None:
    """Run ``check`` and fail if the credential reached a log line while it ran."""
    with _every_log_line() as lines:
        await check(subject)
    _hidden(subject.credential, *lines)
