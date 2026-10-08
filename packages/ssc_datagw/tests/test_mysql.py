"""The MySQL connector on MySQL 8.4 and 9 over TLS (GA-5).

Each version runs in a container that requires TLS, with a server certificate from a CA made
here, seeded with a ``reporting`` schema the user may read, a ``secret`` schema it may not, and a
user that existed before the setup script ran. The adversarial matrix is refused twice: by the
classifier, and, with the classifier switched off, by the database alone (the read-only
transaction and the ``SELECT``-only user ``mysql_setup.sql`` makes)."""

import asyncio
import shlex
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from typing import Any, cast

import asyncmy
import pytest
from connector_suite import CHECKS, Subject, conform
from pki import Pki, make_pki
from testcontainers.community.mysql import MySqlContainer

from ssc_datagw.connectors import (
    Query,
    QueryFailedError,
    QueryRefusedError,
    UpstreamUnavailableError,
)
from ssc_datagw.mysql import (
    SQL_MODE,
    FieldInfo,
    MySqlConnector,
    MySqlTarget,
    Readback,
    bind,
    column,
    session_problem,
    tagged,
)
from ssc_datagw.tls import tls_context

USER = "ssc_datagw"
USER_PASSWORD = "fake-" + "datagw-" + "password"
ANALYST_PASSWORD = "fake-" + "analyst-" + "password"
ROOT_PASSWORD = "fake-" + "root-" + "password"
_CONNECT = cast("Callable[..., Any]", asyncmy.connect)  # pyright: ignore[reportUnknownMemberType]
SETUP = files("ssc_datagw").joinpath("mysql_setup.sql").read_text()
SEED = f"""
SET time_zone = '+00:00';
CREATE TABLE reporting.orders (
    id INT PRIMARY KEY, amount DECIMAL(10, 2), placed DATE, at TIMESTAMP, ok BOOL, meta JSON,
    note VARCHAR(20), took TIME, raw VARBINARY(4), ratio DOUBLE
);
INSERT INTO reporting.orders
WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 50)
SELECT i, i * 1.25, DATE_ADD('2026-01-01', INTERVAL i DAY),
       TIMESTAMP('2026-01-01 00:00:00') + INTERVAL i HOUR, i % 2 = 0,
       JSON_OBJECT('n', i, 'even', IF(i % 2 = 0, CAST('true' AS JSON), CAST('false' AS JSON))),
       CONCAT('note ', i), SEC_TO_TIME(i * 60), UNHEX(LPAD(HEX(i), 2, '0')), i / 4
FROM n;
CREATE VIEW reporting.big_orders AS SELECT id, amount FROM reporting.orders WHERE amount > 50;
CREATE SCHEMA secret;
CREATE TABLE secret.payroll (who VARCHAR(20), pay DECIMAL(10, 2));
INSERT INTO secret.payroll VALUES ('ada', 100);
CREATE USER 'analyst'@'%' IDENTIFIED BY '{ANALYST_PASSWORD}';
GRANT SELECT, CREATE TEMPORARY TABLES ON reporting.* TO 'analyst'@'%';
"""
ENTRYPOINT = (
    'printf "%s" "$SSC_TLS_CERT" > /etc/mysql/server.crt && '
    'printf "%s" "$SSC_TLS_KEY" > /etc/mysql/server.key && '
    'printf "%s" "$SSC_TLS_CA" > /etc/mysql/ca.crt && '
    'printf "%s" "$SSC_SEED" > /docker-entrypoint-initdb.d/seed.sql && '
    'printf "%s" "$SSC_SETUP" > /tmp/setup.sql && '
    "chown mysql /etc/mysql/server.crt /etc/mysql/server.key /etc/mysql/ca.crt "
    "/docker-entrypoint-initdb.d/seed.sql /tmp/setup.sql && chmod 600 /etc/mysql/server.key && "
    "exec docker-entrypoint.sh mysqld --ssl-ca=/etc/mysql/ca.crt "
    "--ssl-cert=/etc/mysql/server.crt --ssl-key=/etc/mysql/server.key "
    "--require-secure-transport=ON"
)
SLOW = (
    "SELECT COUNT(*) FROM reporting.orders a JOIN reporting.orders b JOIN reporting.orders c "
    "JOIN reporting.orders d JOIN reporting.orders e JOIN reporting.orders f "
    "WHERE a.id + b.id + c.id + d.id + e.id + f.id > 0"
)
"""Real work: 50 rows six ways is 15.6 billion combinations. Without the ``WHERE`` MySQL answers
``COUNT(*)`` over a cross join from the table counts at once; ``SLEEP`` would not do either,
since it returns 1 when interrupted instead of failing."""
MATRIX = {
    "set transaction read write": "SET TRANSACTION READ WRITE",
    "temp table": "CREATE TEMPORARY TABLE t AS SELECT 1",
    "multi-statement": "SELECT 1; DELETE FROM reporting.orders",
    "load_file": "SELECT LOAD_FILE('/etc/passwd')",
    "get_lock": "SELECT GET_LOCK('x', 1)",
    "into outfile": "SELECT 1 INTO OUTFILE '/tmp/x'",
    "for update": "SELECT id FROM reporting.orders FOR UPDATE",
    "show": "SHOW TABLES",
    "do sleep": "DO SLEEP(1)",
    "select sleep": "SELECT SLEEP(1)",
}
DATABASE_REFUSES = {
    "temp table": ("CREATE TEMPORARY TABLE t (x INT)", "25006"),
    "set transaction read write": ("SET TRANSACTION READ WRITE", "25001"),
    "delete": ("DELETE FROM reporting.orders", "42000"),
    "another schema": ("SELECT who FROM secret.payroll", "42000"),
    "into outfile": ("SELECT 1 INTO OUTFILE '/tmp/x'", "42000"),
    "for update": ("SELECT id FROM reporting.orders FOR UPDATE", "42000"),
    "unknown table": ("SELECT * FROM reporting.missing", "42S02"),
    "unknown column": ("SELECT nope FROM reporting.orders", "42S22"),
}
"""What the database refuses with the classifier off, and its SQLSTATE: the read-only
transaction stops a temporary table and a change of the transaction, the ``SELECT``-only user
stops a write, a locking read, a file and every schema it was not granted."""


@dataclass(frozen=True)
class Db:
    """One running MySQL, its PKI, and ways in."""

    container: MySqlContainer
    pki: Pki
    host: str
    port: int

    def target(
        self, *, user: str = USER, password: str = USER_PASSWORD, ca: str | None = None
    ) -> MySqlTarget:
        return MySqlTarget(
            host=self.host,
            port=self.port,
            database="reporting",
            user=user,
            password=password,  # pyright: ignore[reportArgumentType]
            ca=self.pki.ca if ca is None else ca,
        )

    def mysql(self, sql: str) -> tuple[int | None, str]:
        """``sql`` through the ``mysql`` client in the container as root, over the socket."""
        result = self.container.exec(["mysql", "-uroot", f"-p{ROOT_PASSWORD}", "-e", sql])
        return result.exit_code, result.output.decode()

    def setup(
        self, *, admin: tuple[str, str] = ("root", ROOT_PASSWORD), **variables: str
    ) -> tuple[int | None, str]:
        """``mysql_setup.sql`` run as the header says by ``admin`` (name, password), with
        ``variables`` set first."""
        init = ", ".join(f"@{name} = '{value}'" for name, value in variables.items())
        flag = f"--init-command={shlex.quote('SET ' + init)} " if init else ""
        name, password = admin
        command = f"mysql -u{name} -p{password} {flag}reporting < /tmp/setup.sql"
        result = self.container.exec(["bash", "-c", command])
        return result.exit_code, result.output.decode()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Any]:
        """Root over TLS, autocommitting, with a buffered cursor."""
        conn = await _CONNECT(
            host=self.host,
            port=self.port,
            user="root",
            password=ROOT_PASSWORD,
            db="reporting",
            ssl=tls_context(self.pki.ca),
            autocommit=True,
        )
        try:
            yield conn
        finally:
            conn.close()


async def scalar(conn: Any, sql: str, *args: object) -> object:
    async with conn.cursor() as cursor:
        await cursor.execute(sql, args or None)
        row = cast("Sequence[object] | None", await cursor.fetchone())
    return None if row is None else row[0]


@pytest.fixture(scope="module", params=["mysql:8.4", "mysql:9"])
def db(request: pytest.FixtureRequest) -> Iterator[Db]:
    pki = make_pki()
    container = (
        MySqlContainer(
            cast("str", request.param),
            username="root",
            password=ROOT_PASSWORD,
            dbname="reporting",
            wait_strategy_check_string=r".*ready for connections.*port: 3306.*",
        )
        .with_env("SSC_TLS_CERT", pki.cert)
        .with_env("SSC_TLS_KEY", pki.key)
        .with_env("SSC_TLS_CA", pki.ca)
        .with_env("SSC_SEED", SEED)
        .with_env("SSC_SETUP", SETUP)
        .with_kwargs(entrypoint=["bash", "-c", ENTRYPOINT])
    )
    with container as my:
        found = Db(my, pki, my.get_container_host_ip(), int(my.get_exposed_port(3306)))
        code, out = found.setup(password=USER_PASSWORD, schemas="reporting")
        assert code == 0, out
        yield found


def ask(
    sql: str, *params: Any, max_rows: int = 100, timeout_ms: int = 5_000, tag: str = "t"
) -> Query:
    return Query(sql=sql, params=params, max_rows=max_rows, timeout_ms=timeout_ms, tag=tag)


async def read(connector: MySqlConnector, query: Query) -> tuple[list[Any], list[Sequence[object]]]:
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
        await asyncio.sleep(0.05)
    return got


def subject(db: Db) -> Subject:
    """The MySQL connector as the connector suite sees it (GA-5)."""
    target = db.target()
    nowhere = target.model_copy(update={"host": "127.0.0.1", "port": 9})
    return Subject(
        connector=MySqlConnector(target),
        unreachable=MySqlConnector(nowhere, connect_seconds=2),
        credential=USER_PASSWORD,
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
        first_row=[1, "1.25", "2026-01-02", "2026-01-01T01:00:00+00:00", False, "note 1"],
        writes=(
            "DELETE FROM reporting.orders",
            "SELECT 1; SELECT 2",
            "CREATE TEMPORARY TABLE t (x INT)",
        ),
        bad="SELECT who FROM secret.payroll",
        slow=SLOW,
        params=("SELECT id, note FROM reporting.orders WHERE id = ?", (3,), [3, "note 3"]),
        table="orders",
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_the_mysql_connector_conforms(
    db: Db, check: Callable[[Subject], Awaitable[None]]
) -> None:
    await conform(check, subject(db))


@pytest.mark.parametrize("case", sorted(MATRIX))
async def test_done_when_the_adversarial_matrix_is_refused(db: Db, case: str) -> None:
    with pytest.raises(QueryRefusedError):
        await read(MySqlConnector(db.target()), ask(MATRIX[case]))


@pytest.mark.parametrize("case", sorted(DATABASE_REFUSES))
async def test_done_when_the_database_refuses_the_matrix_by_itself(db: Db, case: str) -> None:
    sql, sqlstate = DATABASE_REFUSES[case]
    connector = MySqlConnector(db.target(), classify=_no_classifier)
    with pytest.raises(QueryFailedError) as e:
        await read(connector, ask(sql))
    assert e.value.sqlstate == sqlstate


async def test_a_second_statement_past_the_classifier_runs_and_is_denied(db: Db) -> None:
    """The driver always sends ``MULTI_STATEMENTS``, so with the classifier off the ``DELETE``
    after ``SELECT 1`` does reach the server: the connector answers the first result, and the
    ``DELETE`` meets the same ``SELECT``-only user and read-only transaction and changes
    nothing. Only the classifier holds a text to one statement."""
    connector = MySqlConnector(db.target(), classify=_no_classifier)
    _, rows = await read(connector, ask("SELECT 1; DELETE FROM reporting.orders"))
    assert [list(r) for r in rows] == [[1]]
    async with db.connect() as admin:
        assert await scalar(admin, "SELECT COUNT(*) FROM reporting.orders") == 50


async def test_a_statement_past_the_classifier_without_a_result_set_answers_no_rows(
    db: Db,
) -> None:
    connector = MySqlConnector(db.target(), classify=_no_classifier)
    columns, rows = await read(connector, ask("DO 0"))
    assert (columns, rows) == ([], [])


async def test_a_read_returns_typed_columns_and_one_row_past_the_cap(db: Db) -> None:
    columns, rows = await read(
        MySqlConnector(db.target()),
        ask("SELECT * FROM reporting.orders ORDER BY id", max_rows=10),
    )
    assert [(c.name, c.type) for c in columns] == [
        ("id", "integer"),
        ("amount", "decimal"),
        ("placed", "date"),
        ("at", "timestamp"),
        ("ok", "boolean"),
        ("meta", "json"),
        ("note", "string"),
        ("took", "interval"),
        ("raw", "bytes"),
        ("ratio", "float"),
    ]
    assert len(rows) == 11
    assert list(rows[1]) == [
        2,
        Decimal("2.50"),
        date(2026, 1, 3),
        datetime(2026, 1, 1, 2, tzinfo=UTC),
        True,
        {"n": 2, "even": True},
        "note 2",
        timedelta(minutes=2),
        b"\x02",
        0.5,
    ]


async def test_a_read_of_fewer_rows_than_the_cap_returns_them_all(db: Db) -> None:
    _, rows = await read(MySqlConnector(db.target()), ask("SELECT id FROM reporting.big_orders"))
    assert len(rows) == 10


def _refuse_everything(_: str) -> str:
    return "refused"


async def test_a_description_names_the_databases_tables_typed_as_a_read_types_them(
    db: Db,
) -> None:
    connector = MySqlConnector(db.target(), classify=_refuse_everything)
    tables = {t.name: t for t in await connector.describe(schemas=["secret"], timeout_ms=5_000)}
    assert sorted(tables) == ["big_orders", "orders"], "the connection's database alone"
    columns, _ = await read(
        MySqlConnector(db.target()), ask("SELECT * FROM reporting.orders", max_rows=1)
    )
    described = tables["orders"].columns
    assert [(c.name, c.type) for c in described] == [(c.name, c.type) for c in columns]
    assert [c.db_type for c in described][:5] == [
        "int",
        "decimal",
        "date",
        "timestamp",
        "tinyint(1)",
    ]


async def test_a_description_stops_at_500_tables_of_500_columns(db: Db) -> None:
    wide = ", ".join(f"c{i} INT" for i in range(1, 502))
    narrow = " ".join(f"CREATE TABLE wide.t{i:03} (x INT);" for i in range(1, 503))
    code, out = db.mysql(f"CREATE DATABASE wide; CREATE TABLE wide.t000 ({wide}); {narrow}")
    assert code == 0, out
    password = "fake-" + "wide-" + "password"
    code, out = db.setup(user="ssc_wide", password=password, schemas="wide")
    assert code == 0, out
    target = db.target(user="ssc_wide", password=password).model_copy(update={"database": "wide"})
    tables = await MySqlConnector(target).describe(schemas=None, timeout_ms=10_000)
    assert len(tables) == 500
    assert (tables[0].name, len(tables[0].columns)) == ("t000", 500)
    assert tables[0].columns[-1].name == "c500"
    assert tables[-1].name == "t499"


async def test_a_description_past_its_timeout_times_out(db: Db) -> None:
    with pytest.raises(TimeoutError):
        await MySqlConnector(db.target()).describe(schemas=None, timeout_ms=1)


async def test_a_description_runs_the_session_checks(db: Db) -> None:
    connector = MySqlConnector(db.target(user="root", password=ROOT_PASSWORD))
    with pytest.raises(UpstreamUnavailableError, match="the user holds a global privilege"):
        await connector.describe(schemas=None, timeout_ms=5_000)


async def test_question_mark_parameters_bind_strings_numbers_booleans_and_null(db: Db) -> None:
    connector = MySqlConnector(db.target())
    sql = (
        "SELECT id FROM reporting.orders "
        "WHERE note = ? AND id > ? AND ratio >= ? AND ok = ? AND ? IS NULL ORDER BY id"
    )
    _, rows = await read(connector, ask(sql, "note 4", 3, 0.5, True, None))
    assert [list(r) for r in rows] == [[4]]
    awkward = "it's a \\ back'slash \" -- /* ?"
    _, rows = await read(connector, ask("SELECT ? AS s", awkward))
    assert [list(r) for r in rows] == [[awkward]]


@pytest.mark.parametrize(("sql", "params"), [("SELECT ? AS a", ()), ("SELECT 1", (1,))])
async def test_a_wrong_parameter_count_is_07001(db: Db, sql: str, params: tuple[Any, ...]) -> None:
    with pytest.raises(QueryFailedError) as e:
        await read(MySqlConnector(db.target()), ask(sql, *params))
    assert e.value.sqlstate == "07001"


async def test_a_question_mark_in_a_string_is_not_a_placeholder(db: Db) -> None:
    _, rows = await read(MySqlConnector(db.target()), ask("SELECT '?' AS q"))
    assert [list(r) for r in rows] == [["?"]]


async def test_a_percent_sign_is_left_alone(db: Db) -> None:
    sql = "SELECT note FROM reporting.orders WHERE note LIKE 'note 1%' ORDER BY id"
    _, rows = await read(MySqlConnector(db.target()), ask(sql))
    assert len(rows) == 11


async def test_a_read_past_its_statement_timeout_times_out(db: Db) -> None:
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(15):
            await read(MySqlConnector(db.target()), ask(SLOW, timeout_ms=300))


async def test_the_tag_lands_in_the_process_list(db: Db) -> None:
    sql = "SELECT info FROM information_schema.processlist WHERE id = CONNECTION_ID()"
    _, rows = await read(MySqlConnector(db.target()), ask(sql, tag="ledger/prod"))
    assert str(rows[0][0]).startswith("/* ledger/prod */")


async def test_done_when_a_cancelled_read_ends_its_query(db: Db) -> None:
    connector = MySqlConnector(db.target())
    killed: list[int] = []
    kill = connector._kill  # noqa: SLF001

    async def spy(thread_id: int) -> None:
        killed.append(thread_id)
        await kill(thread_id)

    connector._kill = spy  # noqa: SLF001
    task = asyncio.create_task(read(connector, ask(SLOW, timeout_ms=60_000)))
    running = (
        "SELECT id FROM information_schema.processlist WHERE user = %s AND info LIKE %s LIMIT 1"
    )
    busy = "SELECT COUNT(*) FROM information_schema.processlist WHERE id = %s AND info IS NOT NULL"
    async with db.connect() as admin:
        thread_id = await until(lambda: scalar(admin, running, USER, "%JOIN reporting.orders f%"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert killed == [thread_id]

        async def stopped() -> bool:
            return await scalar(admin, busy, thread_id) == 0

        assert await until(stopped) is True


async def test_a_read_killed_from_outside_the_gateway_is_a_query_failure(db: Db) -> None:
    task = asyncio.create_task(read(MySqlConnector(db.target()), ask(SLOW, timeout_ms=60_000)))
    running = (
        "SELECT id FROM information_schema.processlist WHERE user = %s AND info LIKE %s LIMIT 1"
    )
    async with db.connect() as admin:
        thread_id = await until(lambda: scalar(admin, running, USER, "%JOIN reporting.orders f%"))
        async with admin.cursor() as cursor:
            await cursor.execute(f"KILL QUERY {int(cast('int', thread_id))}")
    with pytest.raises(QueryFailedError) as e:
        async with asyncio.timeout(15):
            await task
    assert e.value.sqlstate == "70100"


async def test_tls_with_another_ca_or_without_the_pasted_ca_is_refused(db: Db) -> None:
    for target in (
        db.target(ca=db.pki.other_ca),
        db.target().model_copy(update={"ca": None}),
    ):
        with pytest.raises(UpstreamUnavailableError):
            await read(MySqlConnector(target), ask("SELECT 1"))


async def test_a_user_with_more_than_select_is_refused_before_any_read(db: Db) -> None:
    for target, why in (
        (
            db.target(user="analyst", password=ANALYST_PASSWORD),
            "the user holds a privilege beyond SELECT",
        ),
        (db.target(user="root", password=ROOT_PASSWORD), "the user holds a global privilege"),
    ):
        with pytest.raises(UpstreamUnavailableError) as e:
            await read(MySqlConnector(target), ask("SELECT 1"))
        assert str(e.value) == why


async def test_the_setup_script_runs_again_and_keeps_what_other_users_had(db: Db) -> None:
    code, out = db.setup(password=USER_PASSWORD, schemas="reporting")
    assert code == 0, out
    assert "reporting\tSELECT\t2" in out
    async with db.connect() as admin:
        analyst = (
            "SELECT COUNT(*) FROM information_schema.schema_privileges "
            "WHERE grantee = %s AND table_schema = 'reporting'"
        )
        assert await scalar(admin, analyst, "'analyst'@'%'") == 2
        assert await scalar(admin, analyst, f"'{USER}'@'%'") == 1
        procedures = "SELECT COUNT(*) FROM information_schema.routines WHERE routine_name = %s"
        assert await scalar(admin, procedures, "ssc_setup") == 0
    _, rows = await read(MySqlConnector(db.target()), ask("SELECT COUNT(*) FROM reporting.orders"))
    assert list(rows[0]) == [50]


async def test_the_setup_script_revokes_a_role_granted_to_the_user(db: Db) -> None:
    code, out = db.mysql(
        "CREATE ROLE IF NOT EXISTS ssc_writer; "
        "GRANT INSERT, DELETE ON reporting.* TO ssc_writer; "
        f"GRANT ssc_writer TO '{USER}'@'%'; "
        f"SET DEFAULT ROLE ALL TO '{USER}'@'%'"
    )
    assert code == 0, out
    connector = MySqlConnector(db.target())
    with pytest.raises(UpstreamUnavailableError) as e:
        await read(connector, ask("SELECT 1"))
    assert str(e.value) == "the user has an active role"
    code, out = db.setup(password=USER_PASSWORD, schemas="reporting")
    assert code == 0, out
    async with db.connect() as admin:
        edges = "SELECT COUNT(*) FROM mysql.role_edges WHERE to_user = %s"
        assert await scalar(admin, edges, USER) == 0
    _, rows = await read(connector, ask("SELECT CURRENT_ROLE()"))
    assert list(rows[0]) == ["NONE"]


async def test_the_setup_script_refuses_to_run_without_schemas_or_password(db: Db) -> None:
    code, out = db.setup(password=USER_PASSWORD)
    assert code != 0
    assert "name at least one schema in @schemas" in out
    code, out = db.setup(schemas="reporting")
    assert code != 0
    assert "set @password first" in out
    _, rows = await read(MySqlConnector(db.target()), ask("SELECT COUNT(*) FROM reporting.orders"))
    assert list(rows[0]) == [50]


def grants_of_the_user(db: Db) -> list[str]:
    code, out = db.mysql(f"SHOW GRANTS FOR '{USER}'@'%'")
    assert code == 0, out
    return sorted(line for line in out.splitlines() if line.startswith("GRANT "))


ONLY_SELECT = sorted(
    [f"GRANT USAGE ON *.* TO `{USER}`@`%`", f"GRANT SELECT ON `reporting`.* TO `{USER}`@`%`"]
)


async def test_the_setup_script_revokes_each_grant_on_its_own_level(db: Db) -> None:
    account = f"'{USER}'@'%'"
    code, out = db.mysql(
        f"GRANT PROCESS, BACKUP_ADMIN ON *.* TO {account} WITH GRANT OPTION; "
        f"GRANT INSERT, UPDATE ON reporting.* TO {account} WITH GRANT OPTION; "
        f"GRANT SELECT ON secret.* TO {account}; "
        f"GRANT DELETE ON reporting.orders TO {account}; "
        f"GRANT UPDATE (note, ratio), INSERT (note) ON reporting.orders TO {account}; "
        "CREATE PROCEDURE reporting.ssc_noop() SELECT 1; "
        f"GRANT EXECUTE, ALTER ROUTINE ON PROCEDURE reporting.ssc_noop TO {account} "
        "WITH GRANT OPTION; "
        f"GRANT PROXY ON 'analyst'@'%' TO {account}"
    )
    assert code == 0, out
    assert len(grants_of_the_user(db)) > len(ONLY_SELECT) + 4
    code, out = db.setup(password=USER_PASSWORD, schemas="reporting")
    assert code == 0, out
    assert grants_of_the_user(db) == ONLY_SELECT
    code, out = db.mysql("DROP PROCEDURE reporting.ssc_noop")
    assert code == 0, out


async def test_the_setup_script_runs_for_an_admin_with_partial_revokes(db: Db) -> None:
    """Cloud SQL's root may not touch mysql and sys (partial revokes), so a blanket ``REVOKE
    ALL PRIVILEGES, GRANT OPTION FROM`` the user stopped with ERROR 3879 there (2026-10-08)."""
    writes = (
        "INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER, CREATE TEMPORARY TABLES, "
        "LOCK TABLES, CREATE VIEW, CREATE ROUTINE, ALTER ROUTINE"
    )
    code, out = db.mysql(
        "SET GLOBAL partial_revokes = ON; "
        f"CREATE USER IF NOT EXISTS 'cloudroot'@'%' IDENTIFIED BY '{ROOT_PASSWORD}'; "
        "GRANT ALL PRIVILEGES ON *.* TO 'cloudroot'@'%' WITH GRANT OPTION; "
        f"REVOKE {writes} ON mysql.* FROM 'cloudroot'@'%'; "
        f"REVOKE {writes} ON sys.* FROM 'cloudroot'@'%'; "
        f"GRANT PROCESS ON *.* TO '{USER}'@'%'; "
        f"GRANT INSERT ON reporting.* TO '{USER}'@'%'"
    )
    assert code == 0, out
    blanket = db.container.exec(
        [
            "mysql",
            "-ucloudroot",
            f"-p{ROOT_PASSWORD}",
            "-e",
            f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM '{USER}'@'%'",
        ]
    )
    assert blanket.exit_code != 0
    assert "ERROR 3879" in blanket.output.decode()
    code, out = db.setup(
        admin=("cloudroot", ROOT_PASSWORD), password=USER_PASSWORD, schemas="reporting"
    )
    assert code == 0, out
    assert "reporting\tSELECT\t2" in out
    assert grants_of_the_user(db) == ONLY_SELECT
    _, rows = await read(MySqlConnector(db.target()), ask("SELECT COUNT(*) FROM reporting.orders"))
    assert list(rows[0]) == [50]


READBACK_OK = Readback(
    id=7,
    read_only=1,
    timeout_ms=5_000,
    sql_mode=SQL_MODE,
    time_zone="+00:00",
    roles="NONE",
    global_grants=0,
    schema_grants=0,
    table_grants=0,
    column_grants=0,
)


@pytest.mark.parametrize(
    ("changes", "server_id", "why"),
    [
        ({}, 7, None),
        ({}, 8, "the session is not the announced one: a pooler is between"),
        ({"read_only": 0}, 7, "the transaction is not read-only"),
        ({"timeout_ms": 0}, 7, "the statement timeout did not land"),
        ({"sql_mode": SQL_MODE + ",ANSI_QUOTES"}, 7, "sql_mode is not the one set"),
        ({"time_zone": "SYSTEM"}, 7, "the session is not in UTC"),
        ({"roles": "`writer`@`%`"}, 7, "the user has an active role"),
        ({"global_grants": 1}, 7, "the user holds a global privilege"),
        ({"schema_grants": 1}, 7, "the user holds a privilege beyond SELECT"),
        ({"table_grants": 1}, 7, "the user holds a privilege beyond SELECT"),
        ({"column_grants": 1}, 7, "the user holds a privilege beyond SELECT"),
    ],
)
def test_a_session_that_is_not_the_one_asked_for_is_refused(
    changes: dict[str, Any], server_id: int, why: str | None
) -> None:
    assert session_problem(server_id, 5_000, replace(READBACK_OK, **changes)) == why


@pytest.mark.parametrize(
    ("type_code", "charsetnr", "flags", "length", "portable", "db_type"),
    [
        (3, 63, 0, 11, "integer", "int"),
        (3, 63, 32, 10, "integer", "int"),
        (8, 63, 0, 20, "integer", "bigint"),
        (1, 63, 0, 1, "boolean", "tinyint(1)"),
        (1, 63, 0, 4, "integer", "tinyint"),
        (13, 63, 96, 4, "integer", "year"),
        (16, 63, 32, 1, "integer", "bit"),
        (246, 63, 0, 12, "decimal", "decimal"),
        (4, 63, 0, 12, "float", "float"),
        (5, 63, 0, 22, "float", "double"),
        (10, 63, 128, 10, "date", "date"),
        (7, 63, 128, 19, "timestamp", "timestamp"),
        (12, 63, 128, 19, "timestamp", "datetime"),
        (11, 63, 128, 10, "interval", "time"),
        (245, 63, 144, 4294967295, "json", "json"),
        (255, 63, 144, 4294967295, "bytes", "geometry"),
        (253, 45, 0, 80, "string", "varchar"),
        (253, 63, 128, 4, "bytes", "varbinary"),
        (253, 63, 0, 4, "bytes", "varbinary"),
        (252, 63, 144, 65535, "bytes", "blob"),
        (252, 45, 16, 262140, "string", "text"),
        (254, 45, 256, 8, "string", "enum"),
        (254, 45, 2048, 20, "string", "set"),
        (254, 45, 0, 40, "string", "char"),
    ],
)
def test_a_field_maps_to_its_portable_column(
    type_code: int, charsetnr: int, flags: int, length: int, portable: str, db_type: str
) -> None:
    got = column(FieldInfo("c", type_code, charsetnr, flags, length))
    assert (got.name, got.type, got.db_type) == ("c", portable, db_type)


def test_bind_replaces_each_placeholder_with_an_escaped_literal() -> None:
    def escape(value: object) -> str:
        return f"<{value!r}>"

    sql = "SELECT ? AS a, '?' AS b, ? AS c, ? AS d, x FROM t WHERE y LIKE '%?' AND z = ?"
    got = bind(sql, ("a", True, None, 1.5), escape)
    assert got == (
        "SELECT <'a'> AS a, '?' AS b, TRUE AS c, <None> AS d, x FROM t "
        "WHERE y LIKE '%?' AND z = <1.5>"
    )
    assert bind("SELECT ?", (False,), escape) == "SELECT FALSE"
    with pytest.raises(QueryFailedError) as e:
        bind("SELECT ?, ?", (1,), escape)
    assert e.value.sqlstate == "07001"


def test_the_tag_cannot_end_its_comment() -> None:
    assert tagged("SELECT 1", "ledger/prod") == "/* ledger/prod */ SELECT 1"
    assert tagged("SELECT 1", "a*/b\nc\x00d") == "/* abcd */ SELECT 1"
    assert tagged("SELECT 1", "**// SELECT 2; /*") == "/*  SELECT 2; /* */ SELECT 1"
    assert tagged("SELECT 1", "*\x00/x") == "/* x */ SELECT 1"
    assert tagged("SELECT 1", "x" * 100) == f"/* {'x' * 64} */ SELECT 1"
