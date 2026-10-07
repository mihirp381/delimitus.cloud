"""The Postgres connector on Postgres 17 and 18 over TLS (SSC-051).

Each version runs in a container with a server certificate from a CA made here, seeded with a
``reporting`` schema the role may read, a ``secret`` schema it may not, and a role that existed
before the setup script ran. The adversarial matrix is refused twice: by the classifier, and, with
the classifier switched off, by the database alone (the read-only transaction, the prepared
statement, the role ``postgres_setup.sql`` makes)."""

import asyncio
import json
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
import httpx2
import pytest
from connector_suite import CHECKS, Subject, conform
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from datagw_world import (
    AUDIENCE,
    CERTS,
    ENV,
    PROJECT,
    SALES,
    bearer,
    certs_transport,
    publish,
    store,
)
from testcontainers.community.postgres import PostgresContainer

from ssc_datagw.connectors import (
    Query,
    QueryFailedError,
    QueryRefusedError,
    UpstreamUnavailableError,
)
from ssc_datagw.postgres import (
    PostgresConnector,
    PostgresTarget,
    Readback,
    bind,
    session_problem,
    tls_context,
)
from ssc_datagw.server import production_app
from ssc_datagw.workload import GoogleWorkloads

ROLE = "ssc_datagw"
ROLE_PASSWORD = "fake-" + "datagw-" + "password"
ANALYST_PASSWORD = "fake-" + "analyst-" + "password"
SUPER, SUPER_PASSWORD = "test", "test"
SETUP = files("ssc_datagw").joinpath("postgres_setup.sql").read_text()
SEED = f"""
CREATE ROLE analyst LOGIN PASSWORD '{ANALYST_PASSWORD}';
CREATE SCHEMA reporting;
CREATE TABLE reporting.orders (
    id int PRIMARY KEY, amount numeric(10, 2), placed date, at timestamptz, ok boolean,
    ref uuid, meta jsonb, tags text[], note text, took interval, raw bytea, ratio float8
);
INSERT INTO reporting.orders
SELECT i, i * 1.25, date '2026-01-01' + i,
       timestamptz '2026-01-01 00:00:00+00' + i * interval '1 hour',
       i % 2 = 0, ('00000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid,
       jsonb_build_object('n', i, 'even', i % 2 = 0), ARRAY['t' || i, 'all'], 'note ' || i,
       i * interval '1 minute', decode(lpad(to_hex(i), 2, '0'), 'hex'), i / 4.0
FROM generate_series(1, 50) AS i;
CREATE VIEW reporting.big_orders AS SELECT id, amount FROM reporting.orders WHERE amount > 50;
CREATE SCHEMA secret;
CREATE TABLE secret.payroll (who text, pay numeric);
INSERT INTO secret.payroll VALUES ('ada', 100);
"""
ENTRYPOINT = (
    'printf "%s" "$SSC_TLS_CERT" > /tmp/server.crt && '
    'printf "%s" "$SSC_TLS_KEY" > /tmp/server.key && '
    'printf "%s" "$SSC_SETUP" > /tmp/setup.sql && '
    "chown postgres /tmp/server.crt /tmp/server.key && chmod 600 /tmp/server.key && "
    "exec docker-entrypoint.sh postgres -c ssl=on "
    "-c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key"
)
XML_WRITE = "SELECT query_to_xml('DELETE FROM reporting.orders RETURNING 1', true, false, '')"
MATRIX = {
    "set transaction read write": "SET TRANSACTION READ WRITE",
    "temp table": "CREATE TEMP TABLE t AS SELECT 1 AS x",
    "multi-statement": "SELECT 1; DELETE FROM reporting.orders",
    "query_to_xml": XML_WRITE,
    "notify": "NOTIFY ssc",
    "pg_terminate_backend": "SELECT pg_terminate_backend(pg_backend_pid())",
}
DATABASE_REFUSES = {
    "set transaction read write": ("SET TRANSACTION READ WRITE", "25001"),
    "temp table": ("CREATE TEMP TABLE t AS SELECT 1 AS x", "25006"),
    "multi-statement": ("SELECT 1; DELETE FROM reporting.orders", "42601"),
    "query_to_xml": (XML_WRITE, "0A000"),
    "ts_stat": (
        "SELECT * FROM ts_stat('DELETE FROM reporting.orders RETURNING to_tsvector(note)')",
        "0A000",
    ),
}
"""What the database refuses with the classifier off, and its SQLSTATE: the read-only
transaction stops a write however it arrives, a prepared statement holds one command, and
``query_to_xml`` and ``ts_stat`` refuse a write in the text they run.
Postgres lets ``NOTIFY`` run in a read-only transaction but delivers it only at commit, which
never comes; ``pg_terminate_backend`` against another role's session is refused, against the
role's own sessions only the classifier stops it. Both are below."""


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _ca(cn: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@dataclass(frozen=True)
class Pki:
    """The server's CA, its certificate and key, and a CA that signed nothing here."""

    ca: str
    cert: str
    key: str
    other_ca: str


def _pki() -> Pki:
    ca_key, ca = _ca("ssc test ca")
    _, other = _ca("ssc other ca")
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("localhost"))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(IPv4Address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return Pki(_pem(ca), _pem(cert), key_pem, _pem(other))


@dataclass(frozen=True)
class Db:
    """One running Postgres, its PKI, and ways in."""

    container: PostgresContainer
    pki: Pki
    host: str
    port: int

    def target(
        self, *, user: str = ROLE, password: str = ROLE_PASSWORD, ca: str | None = None
    ) -> PostgresTarget:
        return PostgresTarget(
            host=self.host,
            port=self.port,
            database="test",
            user=user,
            password=password,  # pyright: ignore[reportArgumentType]
            ca=self.pki.ca if ca is None else ca,
        )

    def psql(self, *args: str) -> tuple[int | None, str]:
        """``psql`` in the container as the superuser, stopping at the first error."""
        result = self.container.exec(
            ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", SUPER, "-d", "test", *args]
        )
        return result.exit_code, result.output.decode()

    def setup(self, *variables: str) -> tuple[int | None, str]:
        names = [a for v in variables for a in ("-v", v)]
        return self.psql(*names, "-f", "/tmp/setup.sql")

    @asynccontextmanager
    async def connect(
        self, user: str = SUPER, password: str = SUPER_PASSWORD
    ) -> AsyncIterator[Any]:
        conn = await asyncpg.connect(  # pyright: ignore[reportUnknownMemberType]
            host=self.host,
            port=self.port,
            user=user,
            password=password,
            database="test",
            ssl=tls_context(self.pki.ca),
        )
        try:
            yield conn
        finally:
            await conn.close()


@pytest.fixture(scope="module", params=["postgres:17", "postgres:18"])
def db(request: pytest.FixtureRequest) -> Iterator[Db]:
    pki = _pki()
    container = (
        PostgresContainer(cast("str", request.param), driver=None)
        .with_env("SSC_TLS_CERT", pki.cert)
        .with_env("SSC_TLS_KEY", pki.key)
        .with_env("SSC_SETUP", SETUP)
        .with_kwargs(entrypoint=["bash", "-c", ENTRYPOINT])
    )
    with container as pg:
        found = Db(pg, pki, pg.get_container_host_ip(), int(pg.get_exposed_port(5432)))
        code, out = found.psql("-c", SEED)
        assert code == 0, out
        code, out = found.setup("schemas=reporting", f"password={ROLE_PASSWORD}")
        assert code == 0, out
        yield found


def ask(
    sql: str, *params: Any, max_rows: int = 100, timeout_ms: int = 5_000, tag: str = "t"
) -> Query:
    return Query(sql=sql, params=params, max_rows=max_rows, timeout_ms=timeout_ms, tag=tag)


async def read(
    connector: PostgresConnector, query: Query
) -> tuple[list[Any], list[Sequence[object]]]:
    async with connector.open(query) as cursor:
        return list(cursor.columns), [row async for row in cursor.rows()]


def _no_classifier(_: str) -> None:
    return None


@pytest.mark.parametrize("case", sorted(MATRIX))
async def test_done_when_the_adversarial_matrix_is_refused(db: Db, case: str) -> None:
    connector = PostgresConnector(db.target())
    with pytest.raises(QueryRefusedError):
        await read(connector, ask(MATRIX[case]))


@pytest.mark.parametrize("case", sorted(DATABASE_REFUSES))
async def test_done_when_the_database_refuses_the_matrix_by_itself(db: Db, case: str) -> None:
    sql, sqlstate = DATABASE_REFUSES[case]
    connector = PostgresConnector(db.target(), classify=_no_classifier)
    with pytest.raises(QueryFailedError) as e:
        await read(connector, ask(sql))
    assert e.value.sqlstate == sqlstate


async def test_done_when_a_notify_past_the_classifier_is_never_delivered(db: Db) -> None:
    heard: list[str] = []
    connector = PostgresConnector(db.target(), classify=_no_classifier)
    async with db.connect() as listener, db.connect() as admin:
        await listener.add_listener("ssc", lambda *args: heard.append(args[-1]))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
        _, rows = await read(connector, ask("NOTIFY ssc, 'from the gateway'"))
        assert rows == []
        await admin.execute("NOTIFY ssc, 'control'")
        await until(lambda: asyncio.sleep(0, heard))
    assert heard == ["control"]


async def test_done_when_the_role_cannot_end_another_roles_session(db: Db) -> None:
    connector = PostgresConnector(db.target(), classify=_no_classifier)
    async with db.connect() as admin:
        pid = cast("int", await admin.fetchval("SELECT pg_backend_pid()"))
        with pytest.raises(QueryFailedError) as e:
            await read(connector, ask("SELECT pg_terminate_backend($1)", pid))
        assert e.value.sqlstate == "42501"
        assert await admin.fetchval("SELECT 1") == 1


async def test_the_role_alone_can_create_nothing_write_nothing_and_read_only_its_schemas(
    db: Db,
) -> None:
    attempts = [
        "CREATE TEMP TABLE t (x int)",
        "CREATE TABLE reporting.t (x int)",
        "CREATE TABLE public.t (x int)",
        "CREATE SCHEMA mine",
        "DELETE FROM reporting.orders",
        "SELECT * FROM secret.payroll",
    ]
    async with db.connect(ROLE, ROLE_PASSWORD) as conn:
        await conn.execute("SET default_transaction_read_only = off")
        for sql in attempts:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(sql)


async def test_a_read_returns_typed_columns_and_one_row_past_the_cap(db: Db) -> None:
    columns, rows = await read(
        PostgresConnector(db.target()),
        ask("SELECT * FROM reporting.orders ORDER BY id", max_rows=10),
    )
    assert [(c.name, c.type) for c in columns] == [
        ("id", "integer"),
        ("amount", "decimal"),
        ("placed", "date"),
        ("at", "timestamp"),
        ("ok", "boolean"),
        ("ref", "uuid"),
        ("meta", "json"),
        ("tags", "array"),
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
        UUID("00000000-0000-0000-0000-000000000002"),
        {"n": 2, "even": True},
        ["t2", "all"],
        "note 2",
        timedelta(minutes=2),
        b"\x02",
        0.5,
    ]


def subject(db: Db) -> Subject:
    """The Postgres connector as the connector suite sees it (GA-5)."""
    target = db.target()
    nowhere = target.model_copy(update={"host": "127.0.0.1", "port": 9})
    return Subject(
        connector=PostgresConnector(target),
        unreachable=PostgresConnector(nowhere, connect_seconds=2),
        credential=ROLE_PASSWORD,
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
            "CREATE TEMP TABLE t (x int)",
        ),
        bad="SELECT who FROM secret.payroll",
        slow="SELECT pg_sleep(30)",
        params=("SELECT id, note FROM reporting.orders WHERE id = $1", (3,), [3, "note 3"]),
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_the_postgres_connector_conforms(
    db: Db, check: Callable[[Subject], Awaitable[None]]
) -> None:
    await conform(check, subject(db))


async def test_a_read_of_fewer_rows_than_the_cap_returns_them_all(db: Db) -> None:
    _, rows = await read(PostgresConnector(db.target()), ask("SELECT id FROM reporting.big_orders"))
    assert len(rows) == 10


async def test_string_parameters_bind_to_dates_uuids_and_numbers(db: Db) -> None:
    sql = (
        "SELECT id FROM reporting.orders "
        "WHERE placed >= $1 AND amount > $2 AND ref <> $3 AND at < $4 ORDER BY id"
    )
    _, rows = await read(
        PostgresConnector(db.target()),
        ask(
            sql,
            "2026-02-15",
            50,
            "00000000-0000-0000-0000-000000000048",
            "2026-01-03T00:00:00+00:00",
        ),
    )
    assert [r[0] for r in rows] == [45, 46, 47]


@pytest.mark.parametrize(
    ("sql", "params", "sqlstate"),
    [
        ("SELECT $1::int", [], "08P01"),
        ("SELECT $1::int", [1, 2], "08P01"),
        ("SELECT $1::date", ["not a date"], "22P02"),
        ("SELECT $1::int", ["abc"], "22000"),
        ("SELECT 1 / $1::int", [0], "22012"),
        ("SELECT * FROM secret.payroll", [], "42501"),
        ("SELECT * FROM reporting.missing", [], "42P01"),
    ],
)
async def test_a_statement_the_database_refuses_is_a_query_failure_with_its_sqlstate(
    db: Db, sql: str, params: list[Any], sqlstate: str
) -> None:
    with pytest.raises(QueryFailedError) as e:
        await read(PostgresConnector(db.target()), ask(sql, *params))
    assert e.value.sqlstate == sqlstate


async def test_the_transaction_carries_the_timeout_and_the_tag(db: Db) -> None:
    sql = "SELECT current_setting('statement_timeout'), current_setting('application_name')"
    _, rows = await read(
        PostgresConnector(db.target()), ask(sql, timeout_ms=1234, tag="ledger/prod")
    )
    assert list(rows[0]) == ["1234ms", "ledger/prod"]


async def test_a_read_past_its_statement_timeout_times_out(db: Db) -> None:
    with pytest.raises(TimeoutError):
        await read(PostgresConnector(db.target()), ask("SELECT pg_sleep(5)", timeout_ms=200))


async def until(fetch: Callable[[], Awaitable[object]], within: float = 10) -> object:
    """The first truthy result of ``fetch``, polled until ``within`` seconds pass."""
    deadline = time.monotonic() + within
    while not (got := await fetch()):
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)
    return got


async def test_done_when_a_cancelled_read_ends_its_backend(db: Db) -> None:
    connector = PostgresConnector(db.target())
    ended: list[int] = []
    terminate = connector._terminate  # noqa: SLF001

    async def spy(pid: int) -> None:
        ended.append(pid)
        await terminate(pid)

    connector._terminate = spy  # noqa: SLF001
    task = asyncio.create_task(read(connector, ask("SELECT pg_sleep(30)", timeout_ms=60_000)))
    sleeping = (
        "SELECT pid FROM pg_stat_activity WHERE usename = $1 AND query = 'SELECT pg_sleep(30)'"
    )
    alive = "SELECT count(*) = 0 FROM pg_stat_activity WHERE pid = $1"
    async with db.connect() as admin:
        pid = await until(lambda: admin.fetchval(sleeping, ROLE))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ended == [pid]
        assert await until(lambda: admin.fetchval(alive, pid)) is True


async def test_tls_with_another_ca_or_without_the_pasted_ca_is_refused(db: Db) -> None:
    for target in (
        db.target(ca=db.pki.other_ca),
        db.target().model_copy(update={"ca": None}),
    ):
        with pytest.raises(UpstreamUnavailableError):
            await read(PostgresConnector(target), ask("SELECT 1"))


async def test_a_role_with_more_than_select_is_refused_before_any_read(db: Db) -> None:
    for target, why in (
        (db.target(user=SUPER, password=SUPER_PASSWORD), "the role is a superuser"),
        (
            db.target(user="analyst", password=ANALYST_PASSWORD),
            "the role may create temporary tables",
        ),
    ):
        with pytest.raises(UpstreamUnavailableError) as e:
            await read(PostgresConnector(target), ask("SELECT 1"))
        assert str(e.value) == why


async def test_the_setup_script_runs_again_and_keeps_what_other_roles_had(db: Db) -> None:
    code, out = db.setup("schemas=reporting", f"password={ROLE_PASSWORD}")
    assert code == 0, out
    assert "may read 2 tables and views" in out
    code, out = db.psql("-c", "CREATE ROLE newcomer LOGIN")
    assert code == 0, out
    async with db.connect() as admin:
        temp = "SELECT has_database_privilege($1, 'test', 'TEMPORARY')"
        assert await admin.fetchval(temp, "analyst") is True
        assert await admin.fetchval(temp, ROLE) is False
        assert await admin.fetchval(temp, "newcomer") is False
    _, rows = await read(
        PostgresConnector(db.target()), ask("SELECT count(*) FROM reporting.orders")
    )
    assert list(rows[0]) == [50]


async def test_the_setup_script_can_grant_single_relations_and_refuses_to_grant_nothing(
    db: Db,
) -> None:
    narrow = "fake-" + "narrow-" + "password"
    code, out = db.setup("role=ssc_narrow", "relations=reporting.big_orders", f"password={narrow}")
    assert code == 0, out
    connector = PostgresConnector(db.target(user="ssc_narrow", password=narrow))
    _, rows = await read(connector, ask("SELECT count(*) FROM reporting.big_orders"))
    assert list(rows[0]) == [10]
    with pytest.raises(QueryFailedError) as e:
        await read(connector, ask("SELECT count(*) FROM reporting.orders"))
    assert e.value.sqlstate == "42501"
    code, out = db.setup("role=ssc_empty", "password=x")
    assert code != 0
    assert "name at least one schema" in out


async def test_done_when_the_gateway_serves_a_capped_read_and_refuses_the_matrix(
    db: Db, tmp_path: Path
) -> None:
    blobs = store(tmp_path)
    await publish(blobs, 1)
    target = {**db.target().model_dump(exclude={"password"}), "password": ROLE_PASSWORD}
    env = {**ENV, "SSC_CONNECTION_" + SALES.upper(): json.dumps(target)}
    workloads = GoogleWorkloads(
        audience=AUDIENCE, project_id=PROJECT, transport=certs_transport(), certs_url=CERTS
    )
    app = production_app(env, store=blobs, workloads=workloads)
    url = "/v1/connections/sales/query"
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url="http://datagw") as http,
    ):
        served = await http.post(
            url,
            json={"sql": "SELECT id FROM reporting.orders ORDER BY id", "max_rows": 5},
            headers=bearer(),
        )
        refused = await http.post(url, json={"sql": "NOTIFY ssc"}, headers=bearer())
        failed = await http.post(
            url, json={"sql": "SELECT * FROM secret.payroll"}, headers=bearer()
        )
    assert served.status_code == 200, served.text
    body = served.json()
    assert [r[0] for r in body["rows"]] == [1, 2, 3, 4, 5]
    assert (body["truncated"], body["truncated_reason"]) == (True, "max_rows")
    assert refused.json()["error"]["code"] == "QUERY_REFUSED"
    assert (failed.json()["error"]["code"], failed.json()["error"]["sqlstate"]) == (
        "QUERY_FAILED",
        "42501",
    )


READBACK_OK = Readback(
    pid=7,
    read_only="on",
    conforming="on",
    default_read_only="on",
    superuser=False,
    db_create=False,
    db_temp=False,
    schema_create=False,
)


@pytest.mark.parametrize(
    ("changes", "server_pid", "why"),
    [
        ({}, 7, None),
        ({}, 8, "the backend pid is not the announced one: a pooler is between"),
        ({"default_read_only": "off"}, 7, "the startup settings did not land: a pooler is between"),
        ({"read_only": "off"}, 7, "the transaction is not read-only"),
        ({"conforming": "off"}, 7, "standard_conforming_strings is off"),
        ({"superuser": True}, 7, "the role is a superuser"),
        ({"db_create": True}, 7, "the role may create objects"),
        ({"schema_create": True}, 7, "the role may create objects"),
        ({"db_temp": True}, 7, "the role may create temporary tables"),
    ],
)
def test_a_session_behind_a_pooler_or_with_a_wider_role_is_refused(
    changes: dict[str, Any], server_pid: int, why: str | None
) -> None:
    assert session_problem(server_pid, replace(READBACK_OK, **changes)) == why


def test_parameters_are_coerced_by_the_placeholder_types() -> None:
    got = bind(
        [
            "2026-01-02",
            "2026-01-02T03:04:05+00:00",
            "03:04",
            "00000000-0000-0000-0000-000000000001",
            1.5,
            "x",
            None,
            True,
        ],
        ["date", "timestamptz", "time", "uuid", "numeric", "text", "date", "bool"],
    )
    assert got == [
        date(2026, 1, 2),
        datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        datetime(2026, 1, 1, 3, 4).time(),
        UUID("00000000-0000-0000-0000-000000000001"),
        Decimal("1.5"),
        "x",
        None,
        True,
    ]


def test_tls_is_always_verified() -> None:
    assert tls_context(None).verify_mode == ssl.CERT_REQUIRED
    assert tls_context(None).check_hostname is True
    pki = _pki()
    pasted = tls_context(pki.ca)
    assert pasted.verify_mode == ssl.CERT_REQUIRED
    assert pasted.check_hostname is False
    with pytest.raises(ValueError, match="not PEM"):
        tls_context("not a certificate")
