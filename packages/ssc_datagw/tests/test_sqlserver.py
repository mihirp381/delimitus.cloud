"""The SQL Server connector on SQL Server 2022 over TLS (GA-5).

SQL Server runs in a container that forces TLS 1.2 with a server certificate from a CA made here,
seeded with a ``reporting`` schema the login may read, a ``secret`` schema it may not, and a
login in ``db_datawriter``; ``sqlserver_setup.sql`` then makes the gateway's login with the
container's own sqlcmd. With the classifier switched off, the database alone refuses what the
login was not granted."""

import asyncio
import logging
import os
import socket
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock_time
from decimal import Decimal
from importlib.resources import files
from typing import Any, cast
from uuid import UUID

import pytds  # pyright: ignore[reportMissingTypeStubs]
import pytest
from connector_suite import CHECKS, Subject, conform
from pki import Pki, make_pki
from pytds import tds_base  # pyright: ignore[reportMissingTypeStubs]
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from ssc_datagw import sqlserver
from ssc_datagw.connectors import (
    Query,
    QueryFailedError,
    QueryRefusedError,
    UpstreamUnavailableError,
)
from ssc_datagw.sqlserver import (
    APPLICATION_NAME,
    Readback,
    SqlServerConnector,
    SqlServerTarget,
    bind,
    column,
    session_problem,
    tagged,
)
from ssc_datagw.warmup import WARMUP_CONNECT_SECONDS, Warmup

IMAGE = "mcr.microsoft.com/mssql/server:2022-latest"
LOGIN = "ssc_datagw"
PASSWORD = "Fake-" + "gw-" + "Pw-1234"
WRITER_PASSWORD = "Fake-" + "wr-" + "Pw-1234"
SA_PASSWORD = "Fake-" + "sa-" + "Pw-1234"
SQLCMD = "/opt/mssql-tools18/bin/sqlcmd"
SETUP = files("ssc_datagw").joinpath("sqlserver_setup.sql").read_text()
SEED = f"""
CREATE DATABASE reporting;
GO
USE reporting;
GO
CREATE SCHEMA reporting;
GO
CREATE SCHEMA secret;
GO
CREATE TABLE reporting.orders (
    id INT PRIMARY KEY, amount DECIMAL(10, 2), placed DATE, at DATETIME2(0), ok BIT,
    note NVARCHAR(20)
);
WITH n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 50)
INSERT INTO reporting.orders
SELECT i, i * 1.25, DATEADD(DAY, i, '2026-01-01'), DATEADD(HOUR, i, '2026-01-01'),
       IIF(i % 2 = 0, 1, 0), CONCAT(N'note ', i)
FROM n;
CREATE TABLE reporting.n (i INT PRIMARY KEY);
WITH n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 1000)
INSERT INTO reporting.n SELECT i FROM n OPTION (MAXRECURSION 1000);
CREATE TABLE reporting.everything (
    a_bit BIT, a_tinyint TINYINT, a_smallint SMALLINT, an_int INT, a_bigint BIGINT,
    a_real REAL, a_float FLOAT, a_decimal DECIMAL(9, 3), a_numeric NUMERIC(5, 1),
    a_money MONEY, a_smallmoney SMALLMONEY, a_char CHAR(3), a_varchar VARCHAR(10),
    a_varchar_max VARCHAR(MAX), an_nchar NCHAR(2), an_nvarchar NVARCHAR(10),
    an_nvarchar_max NVARCHAR(MAX), a_text TEXT, an_ntext NTEXT, an_xml XML,
    a_uniqueidentifier UNIQUEIDENTIFIER, a_date DATE, a_time TIME(3), a_datetime DATETIME,
    a_datetime2 DATETIME2(3), a_smalldatetime SMALLDATETIME, a_datetimeoffset DATETIMEOFFSET(0),
    a_binary BINARY(2), a_varbinary VARBINARY(4), an_image IMAGE, a_variant SQL_VARIANT,
    a_hierarchyid HIERARCHYID
);
INSERT INTO reporting.everything VALUES (
    1, 255, -2, 3, 9007199254740993, 1.5, 2.25, 123456.789, 1234.5, 12.3456, 1.25,
    'abc', 'v', 'max', N'nc', N'é', N'nmax', 'text', N'ntext', '<a>1</a>',
    '6f9619ff-8b86-d011-b42d-00c04fc964ff', '2026-03-04', '12:34:56.789',
    '2026-03-04 05:06:07.123', '2026-03-04 05:06:07.123', '2026-03-04 05:06',
    '2026-03-04 05:06:07 +02:00', 0x0102, 0x0A0B, 0x0C, CAST(7 AS INT), '/1/'
);
CREATE TABLE secret.payroll (who NVARCHAR(20), pay DECIMAL(10, 2));
INSERT INTO secret.payroll VALUES (N'ada', 100);
CREATE LOGIN ssc_writer WITH PASSWORD = '{WRITER_PASSWORD}', CHECK_POLICY = ON;
CREATE USER ssc_writer FOR LOGIN ssc_writer;
ALTER ROLE db_datareader ADD MEMBER ssc_writer;
ALTER ROLE db_datawriter ADD MEMBER ssc_writer;
GO
"""
ENTRYPOINT = (
    "set -e; mkdir -p /var/opt/mssql/ssc; "
    'printf "%s" "$SSC_TLS_CERT" > /var/opt/mssql/ssc/server.crt; '
    'printf "%s" "$SSC_TLS_KEY" > /var/opt/mssql/ssc/server.key; '
    "chmod 600 /var/opt/mssql/ssc/server.key; "
    'printf "%s" "$SSC_SEED" > /var/opt/mssql/ssc/seed.sql; '
    'printf "%s" "$SSC_SETUP" > /var/opt/mssql/ssc/setup.sql; '
    'printf "[network]\\ntlscert = /var/opt/mssql/ssc/server.crt\\n'
    'tlskey = /var/opt/mssql/ssc/server.key\\ntlsprotocols = 1.2\\nforceencryption = 1\\n" '
    "> /var/opt/mssql/mssql.conf; "
    "exec /opt/mssql/bin/sqlservr"
)
READY = "SQL Server is now ready for client connections"
SLOW = (
    "SELECT COUNT_BIG(*) FROM reporting.n a CROSS JOIN reporting.n b CROSS JOIN reporting.n c "
    "CROSS JOIN reporting.n d CROSS JOIN reporting.n e CROSS JOIN reporting.n f "
    "WHERE a.i + b.i + c.i + d.i + e.i + f.i > 0"
)
"""Real work: 1000 rows six ways is 10^18 combinations, which the ``WHERE`` keeps SQL Server
from answering from the table counts. ``WAITFOR`` is refused by the classifier."""
WRITES = (
    "INSERT INTO reporting.orders (id) VALUES (999)",
    "UPDATE reporting.orders SET note = N'x'",
    "DELETE FROM reporting.orders",
    "SELECT * INTO reporting.copy FROM reporting.orders",
    "EXEC sp_who",
    "SELECT * FROM OPENROWSET('SQLNCLI', 'Server=x;Trusted_Connection=yes;', 'SELECT 1')",
    "WAITFOR DELAY '00:00:05'",
    "SELECT 1; SELECT 2",
)
DATABASE_REFUSES = {
    "insert": ("INSERT INTO reporting.orders (id) VALUES (999)", "42501"),
    "update": ("UPDATE reporting.orders SET note = N'x'", "42501"),
    "delete": ("DELETE FROM reporting.orders", "42501"),
    "create": ("CREATE TABLE reporting.made (x INT)", "42501"),
    "select into": ("SELECT * INTO reporting.copy FROM reporting.orders", "42501"),
    "another schema": ("SELECT who FROM secret.payroll", "42501"),
    "unknown table": ("SELECT * FROM reporting.missing", "42P01"),
    "unbound name": ("SELECT q.id FROM reporting.orders o", "42P01"),
    "unknown column": ("SELECT nope FROM reporting.orders", "42703"),
    "ambiguous column": (
        "SELECT id FROM reporting.orders a CROSS JOIN reporting.orders b",
        "42702",
    ),
    "syntax": ("SELECT FROM WHERE", "42601"),
    "unknown function": ("SELECT nofunc(1)", "42883"),
    "unknown schema function": ("SELECT reporting.nofunc(1)", "42883"),
    "division by zero": ("SELECT 1 / 0", "22012"),
    "bad number": ("SELECT CAST('x' AS INT)", "22P02"),
    "bad decimal": ("SELECT CAST('x' AS DECIMAL(5, 1))", "22P02"),
    "bad date": ("SELECT CAST('not a date' AS DATETIME)", "22P02"),
    "overflow": ("SELECT CAST(300 AS TINYINT)", "22003"),
    "overflow of an int": ("SELECT CAST(10000000000 AS INT)", "22003"),
    "many in a subquery": ("SELECT (SELECT id FROM reporting.orders) AS one", "21000"),
}
"""What the database refuses with the classifier off, and its SQLSTATE: the login's grants stop
a write, a ``CREATE`` and every schema it was not given."""


def _sqlcmd(*args: str) -> list[str]:
    return [SQLCMD, "-S", "localhost", "-U", "sa", "-P", SA_PASSWORD, "-C", "-b", *args]


@dataclass(frozen=True)
class Db:
    """One running SQL Server, its PKI, and ways in."""

    container: DockerContainer
    pki: Pki
    host: str
    port: int
    cafile: str

    def target(
        self, *, user: str = LOGIN, password: str = PASSWORD, ca: str | None = None
    ) -> SqlServerTarget:
        return SqlServerTarget(
            host=self.host,
            port=self.port,
            database="reporting",
            user=user,
            password=password,  # pyright: ignore[reportArgumentType]
            ca=self.pki.ca if ca is None else ca,
        )

    def sqlcmd(self, *args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
        """sqlcmd in the container as sa, with ``env`` in its environment."""
        wrapped = self.container.get_wrapped_container()
        code, out = cast(
            "tuple[int, bytes]", wrapped.exec_run(_sqlcmd(*args), environment=env or {})
        )
        return code, out.decode()

    def until_sa_logs_in(self, within: float = 60) -> None:
        """SQL Server says it is ready before the first start has set the sa password."""
        deadline = time.monotonic() + within
        while (got := self.sqlcmd("-l", "5", "-Q", "SELECT 1"))[0] != 0:
            if time.monotonic() > deadline:
                raise AssertionError(got[1])
            time.sleep(1)

    def setup(self, **variables: str) -> tuple[int, str]:
        """``sqlserver_setup.sql`` as its header says: the password from the environment."""
        env = {"password": variables.pop("password")} if "password" in variables else {}
        flags = ["-v", *(f"{k}={v}" for k, v in variables.items())] if variables else []
        return self.sqlcmd("-d", "reporting", "-i", "/var/opt/mssql/ssc/setup.sql", *flags, env=env)

    def admin(self) -> Any:
        """sa over TLS, autocommitting."""
        return cast("Callable[..., Any]", pytds.connect)(  # pyright: ignore[reportUnknownMemberType]
            dsn=self.host,
            port=self.port,
            database="reporting",
            user="sa",
            password=SA_PASSWORD,
            cafile=self.cafile,
            validate_host=False,
            autocommit=True,
        )

    async def scalar(self, sql: str, *params: object) -> object:
        def work() -> object:
            with self.admin() as conn, conn.cursor() as cursor:
                cursor.execute(sql, params)
                row = cast("Sequence[object] | None", cursor.fetchone())
            return None if row is None else row[0]

        return await asyncio.to_thread(work)


@pytest.fixture(scope="module")
def db() -> Iterator[Db]:
    pki = make_pki()
    container = (
        DockerContainer(IMAGE)
        .with_env("ACCEPT_EULA", "Y")
        .with_env("MSSQL_SA_PASSWORD", SA_PASSWORD)
        .with_env("MSSQL_PID", "Developer")
        .with_env("SSC_TLS_CERT", pki.cert)
        .with_env("SSC_TLS_KEY", pki.key)
        .with_env("SSC_SEED", SEED)
        .with_env("SSC_SETUP", SETUP)
        .with_exposed_ports(1433)
        .with_kwargs(entrypoint=["bash", "-c", ENTRYPOINT])
        .waiting_for(LogMessageWaitStrategy(READY).with_startup_timeout(180))
    )
    handle, cafile = tempfile.mkstemp(suffix=".pem")
    with os.fdopen(handle, "w") as out:
        out.write(pki.ca)
    try:
        with container as running:
            found = Db(
                running,
                pki,
                running.get_container_host_ip(),
                int(running.get_exposed_port(1433)),
                cafile,
            )
            found.until_sa_logs_in()
            code, out = found.sqlcmd("-i", "/var/opt/mssql/ssc/seed.sql")
            assert code == 0, out
            code, out = found.setup(login=LOGIN, schemas="reporting", password=PASSWORD)
            assert code == 0, out
            yield found
    finally:
        os.unlink(cafile)


def ask(
    sql: str, *params: Any, max_rows: int = 100, timeout_ms: int = 5_000, tag: str = "t"
) -> Query:
    return Query(sql=sql, params=params, max_rows=max_rows, timeout_ms=timeout_ms, tag=tag)


async def read(
    connector: SqlServerConnector, query: Query
) -> tuple[list[Any], list[Sequence[object]]]:
    async with connector.open(query) as cursor:
        return list(cursor.columns), [row async for row in cursor.rows()]


def _no_classifier(_: str) -> None:
    return None


async def until(fetch: Callable[[], Awaitable[object]], within: float = 10) -> object:
    """The first truthy result of ``fetch``, polled until ``within`` seconds pass."""
    deadline = time.monotonic() + within
    while not (got := await fetch()):
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.1)
    return got


SESSIONS = "SELECT COUNT(*) FROM sys.dm_exec_sessions WHERE login_name = %s AND program_name = %s"
RUNNING = (
    "SELECT TOP (1) r.session_id FROM sys.dm_exec_requests r "
    "JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id "
    "CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t "
    "WHERE s.login_name = %s AND s.program_name = %s AND t.text LIKE %s"
)


async def gone(db: Db) -> bool:
    return await db.scalar(SESSIONS, LOGIN, APPLICATION_NAME) == 0


def subject(db: Db) -> Subject:
    """The SQL Server connector as the connector suite sees it (GA-5)."""
    target = db.target()
    nowhere = target.model_copy(update={"host": "127.0.0.1", "port": 9})
    return Subject(
        connector=SqlServerConnector(target),
        unreachable=SqlServerConnector(nowhere, connect_seconds=2),
        credential=PASSWORD,
        secrets=(target, nowhere),
        read="SELECT id, amount, placed, at, ok, note FROM reporting.orders ORDER BY id",
        read_columns={
            "id": "integer",
            "amount": "decimal",
            "placed": "date",
            "at": "timestamp",
            "ok": "boolean",
            "note": "string",
        },
        first_row=[1, "1.25", "2026-01-02", "2026-01-01T01:00:00", False, "note 1"],
        writes=WRITES,
        bad="SELECT who FROM secret.payroll",
        slow=SLOW,
        params=("SELECT id, note FROM reporting.orders WHERE id = ?", (3,), [3, "note 3"]),
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_the_sqlserver_connector_conforms(
    db: Db, check: Callable[[Subject], Awaitable[None]]
) -> None:
    await conform(check, subject(db))
    assert await until(lambda: gone(db)) is True


async def test_a_missing_table_is_a_query_failure(db: Db) -> None:
    with pytest.raises(QueryFailedError) as e:
        await read(SqlServerConnector(db.target()), ask("SELECT * FROM reporting.missing"))
    assert e.value.sqlstate == "42P01"


@pytest.mark.parametrize("case", sorted(DATABASE_REFUSES))
async def test_done_when_the_database_refuses_by_itself(db: Db, case: str) -> None:
    sql, sqlstate = DATABASE_REFUSES[case]
    connector = SqlServerConnector(db.target(), classify=_no_classifier)
    with pytest.raises(QueryFailedError) as e:
        await read(connector, ask(sql))
    assert e.value.sqlstate == sqlstate
    name, number = str(e.value).split()
    assert (name.endswith("Error"), number.isdigit()) == (True, True)


async def test_every_type_maps_to_its_portable_column(db: Db) -> None:
    columns, rows = await read(
        SqlServerConnector(db.target()), ask("SELECT * FROM reporting.everything")
    )
    assert [(c.name, c.type, c.db_type) for c in columns] == [
        ("a_bit", "boolean", "bit"),
        ("a_tinyint", "integer", "tinyint"),
        ("a_smallint", "integer", "smallint"),
        ("an_int", "integer", "int"),
        ("a_bigint", "integer", "bigint"),
        ("a_real", "float", "real"),
        ("a_float", "float", "float"),
        ("a_decimal", "decimal", "decimal"),
        ("a_numeric", "decimal", "decimal"),
        ("a_money", "decimal", "money"),
        ("a_smallmoney", "decimal", "smallmoney"),
        ("a_char", "string", "varchar"),
        ("a_varchar", "string", "varchar"),
        ("a_varchar_max", "string", "varchar"),
        ("an_nchar", "string", "nvarchar"),
        ("an_nvarchar", "string", "nvarchar"),
        ("an_nvarchar_max", "string", "nvarchar"),
        ("a_text", "string", "text"),
        ("an_ntext", "string", "ntext"),
        ("an_xml", "string", "xml"),
        ("a_uniqueidentifier", "uuid", "uniqueidentifier"),
        ("a_date", "date", "date"),
        ("a_time", "time", "time"),
        ("a_datetime", "timestamp", "datetime"),
        ("a_datetime2", "timestamp", "datetime2"),
        ("a_smalldatetime", "timestamp", "smalldatetime"),
        ("a_datetimeoffset", "timestamp", "datetimeoffset"),
        ("a_binary", "bytes", "varbinary"),
        ("a_varbinary", "bytes", "varbinary"),
        ("an_image", "bytes", "image"),
        ("a_variant", "string", "sql_variant"),
        ("a_hierarchyid", "bytes", "hierarchyid"),
    ]
    assert list(rows[0])[:-1] == [
        True,
        255,
        -2,
        3,
        9007199254740993,
        1.5,
        2.25,
        Decimal("123456.789"),
        Decimal("1234.5"),
        Decimal("12.3456"),
        Decimal("1.2500"),
        "abc",
        "v",
        "max",
        "nc",
        "é",
        "nmax",
        "text",
        "ntext",
        "<a>1</a>",
        UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff"),
        date(2026, 3, 4),
        clock_time(12, 34, 56, 789000),
        datetime(2026, 3, 4, 5, 6, 7, 123000),
        datetime(2026, 3, 4, 5, 6, 7, 123000),
        datetime(2026, 3, 4, 5, 6),
        datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone(timedelta(hours=2))),
        b"\x01\x02",
        b"\x0a\x0b",
        b"\x0c",
        7,
    ]
    assert isinstance(rows[0][-1], bytes)


async def test_a_read_returns_one_row_past_the_cap(db: Db) -> None:
    _, rows = await read(
        SqlServerConnector(db.target()), ask("SELECT i FROM reporting.n ORDER BY i", max_rows=700)
    )
    assert len(rows) == 701
    assert list(rows[-1]) == [701]


async def test_question_mark_parameters_bind_strings_numbers_booleans_and_null(db: Db) -> None:
    connector = SqlServerConnector(db.target())
    sql = (
        "SELECT id FROM reporting.orders "
        "WHERE note = ? AND id > ? AND amount >= ? AND ok = ? AND ? IS NULL ORDER BY id"
    )
    _, rows = await read(connector, ask(sql, "note 4", 3, 0.5, True, None))
    assert [list(r) for r in rows] == [[4]]
    awkward = "it's 50% a \\ back'slash \" -- /* ? %s"
    _, rows = await read(connector, ask("SELECT ? AS s, '?' AS q, '50%' AS p", awkward))
    assert [list(r) for r in rows] == [[awkward, "?", "50%"]]


@pytest.mark.parametrize(("sql", "params"), [("SELECT ? AS a", ()), ("SELECT 1", (1,))])
async def test_a_wrong_parameter_count_is_07001(db: Db, sql: str, params: tuple[Any, ...]) -> None:
    with pytest.raises(QueryFailedError) as e:
        await read(SqlServerConnector(db.target()), ask(sql, *params))
    assert e.value.sqlstate == "07001"


async def test_the_session_is_the_one_asked_for(db: Db) -> None:
    sql = (
        "SELECT transaction_isolation_level, lock_timeout, arithabort, date_format, "
        "program_name FROM sys.dm_exec_sessions WHERE session_id = @@SPID"
    )
    connector = SqlServerConnector(db.target(), classify=_no_classifier)
    _, rows = await read(connector, ask(sql, timeout_ms=4_321))
    assert list(rows[0]) == [2, 4_321, True, "ymd", APPLICATION_NAME]


async def test_done_when_a_read_past_its_timeout_ends_its_session(db: Db) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await read(SqlServerConnector(db.target()), ask(SLOW, timeout_ms=1_000))
    assert time.monotonic() - started < 8
    assert await until(lambda: gone(db)) is True


async def test_done_when_a_cancelled_read_ends_its_session_and_the_tag_shows(db: Db) -> None:
    connector = SqlServerConnector(db.target())
    task = asyncio.create_task(read(connector, ask(SLOW, timeout_ms=60_000, tag="ledger/prod")))
    spid = await until(
        lambda: db.scalar(RUNNING, LOGIN, APPLICATION_NAME, "/* ledger/prod */ SELECT COUNT_BIG%")
    )
    assert isinstance(spid, int)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(10):
            await task
    assert await until(lambda: gone(db)) is True


async def test_tls_with_another_ca_is_refused(db: Db) -> None:
    with pytest.raises(UpstreamUnavailableError) as e:
        await read(SqlServerConnector(db.target(ca=db.pki.other_ca)), ask("SELECT 1"))
    assert str(e.value).startswith("cannot connect: ")


def test_a_ca_that_is_not_pem_is_refused_at_build() -> None:
    target = SqlServerTarget(
        host="db.example.com",
        database="reporting",
        user=LOGIN,
        password=PASSWORD,  # pyright: ignore[reportArgumentType]
        ca="not a certificate",
    )
    with pytest.raises(ValueError):
        SqlServerConnector(target)


async def test_a_wrong_password_or_database_is_unavailable(db: Db) -> None:
    wrong_database = db.target().model_copy(update={"database": "nowhere"})
    for target in (db.target(password=PASSWORD + "x"), wrong_database):
        started = time.monotonic()
        with pytest.raises(UpstreamUnavailableError) as e:
            await read(SqlServerConnector(target, connect_seconds=2), ask("SELECT 1"))
        assert time.monotonic() - started < 6
        assert PASSWORD not in f"{e.value} {e.value!r}"


async def test_a_login_with_more_than_select_is_refused_before_any_read(db: Db) -> None:
    for target, why in (
        (db.target(user="ssc_writer", password=WRITER_PASSWORD), "the login may write"),
        (db.target(user="sa", password=SA_PASSWORD), "the login is a sysadmin"),
    ):
        with pytest.raises(UpstreamUnavailableError) as e:
            await read(SqlServerConnector(target), ask("SELECT 1"))
        assert str(e.value) == why


async def test_the_secret_stays_out_of_reprs_errors_and_logs(
    db: Db, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    target = db.target(password=PASSWORD + "x")
    assert PASSWORD not in f"{target!r} {target!s} {target.model_dump()}"
    with pytest.raises(UpstreamUnavailableError) as e:
        await read(SqlServerConnector(target), ask("SELECT 1"))
    assert PASSWORD not in f"{e.value!r} {e.value.__cause__!r} {e.value.__context__!r}"
    assert all(PASSWORD not in r.getMessage() for r in caplog.records)


async def test_no_log_line_holds_the_statement(db: Db, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    marker = "statement_marker_4471"
    await read(SqlServerConnector(db.target()), ask(f"SELECT 1 AS {marker}"))
    assert all(marker not in r.getMessage() for r in caplog.records)


async def test_the_setup_script_runs_again_and_leaves_only_select(db: Db) -> None:
    code, out = db.setup(login=LOGIN, schemas="reporting", password=PASSWORD)
    assert code == 0, out
    assert "ready" in out
    roles = (
        "SELECT COUNT(*) FROM sys.database_role_members "
        "WHERE member_principal_id = DATABASE_PRINCIPAL_ID(%s)"
    )
    assert await db.scalar(roles, LOGIN) == 0
    connector = SqlServerConnector(db.target(), classify=_no_classifier)
    for sql in ("INSERT INTO reporting.orders (id) VALUES (998)", "CREATE TABLE dbo.made (x INT)"):
        with pytest.raises(QueryFailedError) as e:
            await read(connector, ask(sql))
        assert e.value.sqlstate == "42501"
    _, rows = await read(connector, ask("SELECT COUNT(*) FROM reporting.orders"))
    assert list(rows[0]) == [50]


async def test_the_setup_script_takes_the_login_out_of_a_role(db: Db) -> None:
    code, out = db.sqlcmd("-d", "reporting", "-Q", f"ALTER ROLE db_datawriter ADD MEMBER {LOGIN}")
    assert code == 0, out
    with pytest.raises(UpstreamUnavailableError) as e:
        await read(SqlServerConnector(db.target()), ask("SELECT 1"))
    assert str(e.value) == "the login may write"
    code, out = db.setup(login=LOGIN, schemas="reporting", password=PASSWORD)
    assert code == 0, out
    _, rows = await read(SqlServerConnector(db.target()), ask("SELECT 1 AS one"))
    assert list(rows[0]) == [1]


async def test_the_setup_script_stops_on_a_grant_it_did_not_make(db: Db) -> None:
    grant = f"GRANT INSERT ON SCHEMA::reporting TO {LOGIN}"
    code, out = db.sqlcmd("-d", "reporting", "-Q", grant)
    assert code == 0, out
    try:
        with pytest.raises(UpstreamUnavailableError) as e:
            await read(SqlServerConnector(db.target()), ask("SELECT 1"))
        assert str(e.value) == "the login holds a grant beyond SELECT"
        code, out = db.setup(login=LOGIN, schemas="reporting", password=PASSWORD)
        assert code != 0
        assert "may still write" in out
    finally:
        code, out = db.sqlcmd("-d", "reporting", "-Q", grant.replace("GRANT", "REVOKE"))
        assert code == 0, out


async def test_the_setup_script_refuses_a_missing_schema(db: Db) -> None:
    code, out = db.setup(login=LOGIN, schemas="reporting,nowhere", password=PASSWORD)
    assert code != 0
    assert "does not exist" in out


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


TARGET = SqlServerTarget(
    host="db.example.com",
    database="reporting",
    user=LOGIN,
    password=PASSWORD,  # pyright: ignore[reportArgumentType]
    ca=make_pki().ca,
)


def fake_connect(monkeypatch: pytest.MonkeyPatch, *outcomes: object) -> list[float]:
    """``_DIAL`` and ``_CONNECT`` replaced: each connect takes the next outcome."""
    seconds: list[float] = []
    left = list(outcomes)

    def dial(_: object, timeout: float) -> socket.socket:
        ours, theirs = socket.socketpair()
        theirs.close()
        return ours

    def connect(**kwargs: Any) -> object:
        seconds.append(cast("float", kwargs["login_timeout"]))
        outcome = left.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(sqlserver, "_DIAL", dial)
    monkeypatch.setattr(sqlserver, "_CONNECT", connect)
    return seconds


async def test_a_new_instance_tries_a_timed_out_connect_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    seconds = fake_connect(monkeypatch, TimeoutError(), TimeoutError(), "conn")
    c = SqlServerConnector(TARGET, warmup=Warmup(0.0, clock, clock.sleep))
    assert await c._connect(sqlserver._Line(), 1_000) == "conn"  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    assert seconds == [WARMUP_CONNECT_SECONDS] * 3


async def test_a_login_error_behind_a_timeout_is_unavailable_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    cause = tds_base.OperationalError("cannot open the database")
    cause.number = 4060
    timeout = TimeoutError()
    timeout.__cause__ = cause
    seconds = fake_connect(monkeypatch, timeout, "conn")
    c = SqlServerConnector(TARGET, warmup=Warmup(0.0, clock, clock.sleep))
    with pytest.raises(UpstreamUnavailableError, match="OperationalError 4060"):
        await c._connect(sqlserver._Line(), 1_000)  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    assert seconds == [WARMUP_CONNECT_SECONDS]


async def test_a_redirect_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class Redirected:
        closed = False

        def close(self) -> None:
            self.closed = True

    conn = Redirected()

    def dial(_: object, timeout: float) -> socket.socket:
        ours, theirs = socket.socketpair()
        theirs.close()
        return ours

    def connect(**kwargs: Any) -> object:
        cast("socket.socket", kwargs["sock"]).close()
        return conn

    monkeypatch.setattr(sqlserver, "_DIAL", dial)
    monkeypatch.setattr(sqlserver, "_CONNECT", connect)
    c = SqlServerConnector(TARGET)
    with pytest.raises(UpstreamUnavailableError, match="the server's own address"):
        await c._connect(sqlserver._Line(), 1_000)  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    assert conn.closed


READBACK_OK = Readback(
    spid=60,
    database_name="reporting",
    sysadmin=0,
    db_owner=0,
    datawriter=0,
    ddladmin=0,
    can_insert=0,
    can_update=0,
    can_delete=0,
    can_alter=0,
    can_control=0,
    can_execute=0,
    direct_grants=0,
)


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({}, None),
        ({"database_name": "REPORTING"}, None),
        ({"database_name": "master"}, "the session is not in the connection's database"),
        ({"database_name": None}, "the session is not in the connection's database"),
        ({"sysadmin": 1}, "the login is a sysadmin"),
        ({"sysadmin": None}, "the login is a sysadmin"),
        ({"db_owner": 1}, "the login owns or controls the database"),
        ({"can_control": 1}, "the login owns or controls the database"),
        ({"datawriter": 1}, "the login may write"),
        ({"can_insert": 1}, "the login may write"),
        ({"can_update": None}, "the login may write"),
        ({"can_delete": 1}, "the login may write"),
        ({"ddladmin": 1}, "the login may create or alter objects"),
        ({"can_alter": 1}, "the login may create or alter objects"),
        ({"can_execute": 1}, "the login may execute procedures"),
        ({"direct_grants": 2}, "the login holds a grant beyond SELECT"),
        ({"direct_grants": None}, "the login holds a grant beyond SELECT"),
    ],
)
def test_a_session_that_is_not_the_one_asked_for_is_refused(
    changes: dict[str, Any], why: str | None
) -> None:
    assert session_problem("reporting", replace(READBACK_OK, **changes)) == why


class NVarCharMaxSerializer:
    pass


class NText72Serializer:
    pass


class UDT72Serializer:
    type_name = "Geography"


@pytest.mark.parametrize(
    ("type_id", "serializer", "portable", "db_type"),
    [
        (56, object(), "integer", "int"),
        (106, object(), "decimal", "decimal"),
        (99, NVarCharMaxSerializer(), "string", "nvarchar"),
        (99, NText72Serializer(), "string", "ntext"),
        (0, UDT72Serializer(), "bytes", "geography"),
        (36, object(), "uuid", "uniqueidentifier"),
        (43, object(), "timestamp", "datetimeoffset"),
        (12345, object(), "string", "type 12345"),
    ],
)
def test_a_type_id_maps_to_its_portable_column(
    type_id: int, serializer: object, portable: str, db_type: str
) -> None:
    got = column("c", type_id, serializer)
    assert (got.name, got.type, got.db_type) == ("c", portable, db_type)


def test_bind_turns_each_placeholder_into_a_driver_parameter() -> None:
    sql = (
        "SELECT ? AS a, '?' AS b, [x?] AS c, ? AS d /* ? */ FROM t "
        "WHERE y LIKE '50%' AND z = ? -- ?\n"
    )
    assert bind(sql, ("a", None, 1.5)) == (
        "SELECT %s AS a, '?' AS b, [x?] AS c, %s AS d /* ? */ FROM t "
        "WHERE y LIKE '50%%' AND z = %s -- ?\n",
        ("a", None, 1.5),
    )
    assert bind("SELECT 1", ()) == ("SELECT 1", ())
    with pytest.raises(QueryFailedError) as e:
        bind("SELECT ?, ?", (1,))
    assert e.value.sqlstate == "07001"


def test_the_tag_cannot_end_its_comment() -> None:
    assert tagged("SELECT 1", "ledger/prod") == "/* ledger/prod */ SELECT 1"
    assert tagged("SELECT 1", "a*/b") == "/* ab */ SELECT 1"
    assert tagged("SELECT 1", "*//**/x/*") == "/* x */ SELECT 1"
    assert tagged("SELECT 1", "50%") == "/* 50%% */ SELECT 1"
    assert tagged("SELECT 1", "x" * 200) == f"/* {'x' * 128} */ SELECT 1"


def _numbered(kind: type[Exception], number: int) -> Exception:
    exc = kind("the server's own text, which names things")
    exc.number = number  # pyright: ignore[reportAttributeAccessIssue]
    return exc


@pytest.mark.parametrize(
    ("number", "sqlstate"),
    [
        (208, "42P01"),
        (207, "42703"),
        (102, "42601"),
        (156, "42601"),
        (229, "42501"),
        (230, "42501"),
        (297, "42501"),
        (916, "42501"),
        (262, "42501"),
        (8134, "22012"),
        (245, "22P02"),
        (8114, "22P02"),
        (241, "22P02"),
        (242, "22P02"),
        (8115, "22003"),
        (220, "22003"),
        (512, "21000"),
        (1205, "40P01"),
        (4104, "42P01"),
        (209, "42702"),
        (195, "42883"),
        (4121, "42883"),
        (50000, None),
    ],
)
def test_an_error_number_maps_to_its_sqlstate(number: int, sqlstate: str | None) -> None:
    got = sqlserver._failure(_numbered(tds_base.ProgrammingError, number))  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    assert isinstance(got, QueryFailedError)
    assert (str(got), got.sqlstate) == (f"ProgrammingError {number}", sqlstate)


@pytest.mark.parametrize("number", [18456, 4060, 701, 1204, 17809])
def test_an_error_number_about_the_database_is_unavailable(number: int) -> None:
    got = sqlserver._failure(_numbered(tds_base.OperationalError, number))  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    assert isinstance(got, UpstreamUnavailableError)
    assert "names things" not in str(got)


def test_a_lock_timeout_or_a_lost_connection_is_what_it_is() -> None:
    failure = sqlserver._failure  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    assert isinstance(failure(_numbered(tds_base.OperationalError, 1222)), TimeoutError)
    assert isinstance(failure(TimeoutError()), TimeoutError)
    for exc in (
        tds_base.ClosedConnectionError(),
        tds_base.InterfaceError("x"),
        ConnectionResetError(),
    ):
        assert isinstance(failure(exc), UpstreamUnavailableError)


async def test_a_refused_statement_never_reaches_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def connect(**_: Any) -> object:
        raise AssertionError("connected")

    monkeypatch.setattr(sqlserver, "_CONNECT", connect)
    with pytest.raises(QueryRefusedError):
        await read(SqlServerConnector(TARGET), ask("SELECT * FROM #temp"))
