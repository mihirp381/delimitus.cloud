"""The BigQuery connector against a fake BigQuery API over TLS in-process (GA-5 B5).

Each test that reads starts a FastAPI app under ``uvicorn`` on 127.0.0.1 with a server
certificate for ``localhost`` from the test PKI, so every request crosses a real TLS socket. The
app answers ``jobs.query``, ``getQueryResults`` and ``jobs.cancel`` as Google does: it verifies
the bearer JWT against the test service account's public key (RS256, ``kid``, ``iss`` = ``sub``
= the email, ``aud`` exact, at most an hour) and answers Google-shaped errors otherwise. Its
answers are keyed by the query text: a complete job, one that needs two polls, one that comes in
pages, one whose poll sleeps 20 s, one of every type, a job that fails after its poll, and
``invalidQuery``, ``notFound``, ``accessDenied`` and ``bytesBilledLimitExceeded``. It records
every request body, poll and cancel. The service account's key is made here, never read from
disk. The connector suite runs against it; the conversions, the parameters, the target model
and the error map are unit-tested without it."""

import asyncio
import json
import logging
import socket
import time
import traceback
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
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
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pki import Pki, make_pki
from pydantic import ValidationError

from ssc_datagw.bigquery import (
    AUDIENCE,
    BigQueryConnector,
    BigQueryTarget,
    SchemaField,
    failure,
    interval,
    label,
    page,
    placeholders,
    query_parameters,
    row,
    scalar,
    schema_fields,
)
from ssc_datagw.connectors import (
    Column,
    QueryFailedError,
    QueryRefusedError,
    UpstreamUnavailableError,
    jsonable,
)
from ssc_datagw.tls import tls_context

PROJECT = "fake-project"
DATASET = "reporting"
LOCATION = "EU"
EMAIL = "reader@fake-project.iam.gserviceaccount.com"
BODY_MARK = "do-not-echo-the-body"
"""In every error body the fake sends; no error may carry it."""
PLACED = 1_704_164_645_123_456
"""2024-01-02T03:04:05.123456Z in microseconds."""

READ = "SELECT id, amount, ok, note, placed FROM reporting.orders ORDER BY id"
PARAMS = "SELECT id, note FROM reporting.orders WHERE id = ?"
POLLED = "SELECT 'polled' AS x"
PAGED = "SELECT 'paged' AS x"
SLOW = "SELECT 'slow' AS x"
TYPES = "SELECT 'types' AS x"
LATE = "SELECT 'late' AS x"
INVALID = "SELECT 'invalid' AS x"
NOT_FOUND = "SELECT * FROM reporting.nope"
DENIED = "SELECT * FROM secret.t"
BILLED = "SELECT 'billed' AS x"

ORDERS_SCHEMA: list[dict[str, Any]] = [
    {"name": "id", "type": "INTEGER", "mode": "NULLABLE"},
    {"name": "amount", "type": "NUMERIC", "mode": "NULLABLE"},
    {"name": "ok", "type": "BOOLEAN", "mode": "NULLABLE"},
    {"name": "note", "type": "STRING", "mode": "NULLABLE"},
    {"name": "placed", "type": "TIMESTAMP", "mode": "NULLABLE"},
]


def _order(i: int) -> dict[str, Any]:
    cells = [str(i), f"{i}.25", "true" if i % 2 == 0 else "false", f"note {i}"]
    cells.append(str(PLACED + (i - 1) * 1_000_000))
    return {"f": [{"v": c} for c in cells]}


ORDERS = [_order(i) for i in range(1, 4)]
X_SCHEMA: list[dict[str, Any]] = [{"name": "x", "type": "STRING", "mode": "NULLABLE"}]


def _xs(n: int) -> list[dict[str, Any]]:
    return [{"f": [{"v": f"x{i}"}]} for i in range(1, n + 1)]


TYPES_SCHEMA: list[dict[str, Any]] = [
    {"name": "i", "type": "INTEGER"},
    {"name": "f", "type": "FLOAT"},
    {"name": "n", "type": "NUMERIC"},
    {"name": "bn", "type": "BIGNUMERIC"},
    {"name": "b", "type": "BOOLEAN"},
    {"name": "s", "type": "STRING"},
    {"name": "by", "type": "BYTES"},
    {"name": "d", "type": "DATE"},
    {"name": "t", "type": "TIME"},
    {"name": "dt", "type": "DATETIME"},
    {"name": "ts", "type": "TIMESTAMP"},
    {"name": "g", "type": "GEOGRAPHY"},
    {"name": "j", "type": "JSON"},
    {"name": "iv", "type": "INTERVAL"},
    {"name": "r", "type": "RANGE"},
    {"name": "a", "type": "INTEGER", "mode": "REPEATED"},
    {
        "name": "st",
        "type": "RECORD",
        "mode": "NULLABLE",
        "fields": [
            {"name": "k", "type": "STRING"},
            {"name": "vs", "type": "NUMERIC", "mode": "REPEATED"},
        ],
    },
    {
        "name": "rs",
        "type": "RECORD",
        "mode": "REPEATED",
        "fields": [{"name": "d", "type": "DATE"}],
    },
    {"name": "nul", "type": "STRING"},
]
TYPES_ROW: dict[str, Any] = {
    "f": [
        {"v": "-42"},
        {"v": "1.5"},
        {"v": "123.456"},
        {"v": "1E+40"},
        {"v": "true"},
        {"v": "é"},
        {"v": "AAH/"},
        {"v": "2024-02-29"},
        {"v": "23:59:58.5"},
        {"v": "2024-01-02T03:04:05.000006"},
        {"v": str(PLACED)},
        {"v": "POINT(1 2)"},
        {"v": '{"a": [1, null]}'},
        {"v": "1-2 3 4:5:6.5"},
        {"v": "[2024-01-01, UNBOUNDED)"},
        {"v": [{"v": "1"}, {"v": "2"}]},
        {"v": {"f": [{"v": "key"}, {"v": [{"v": "0.10"}]}]}},
        {"v": [{"v": {"f": [{"v": "2024-01-01"}]}}, {"v": {"f": [{"v": None}]}}]},
        {"v": None},
    ]
}


@dataclass(frozen=True)
class Answer:
    """What the fake answers a query: rows after ``polls`` incomplete answers, ``page_size``
    rows a page, the poll sleeping ``sleep`` seconds; or an ``error`` on the POST, or a
    ``late`` reason after the poll."""

    schema: list[dict[str, Any]] = field(default_factory=lambda: X_SCHEMA)
    rows: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    polls: int = 0
    page_size: int | None = None
    sleep: float = 0
    error: tuple[int, str] | None = None
    late: str | None = None


ANSWERS: dict[str, Answer] = {
    READ: Answer(ORDERS_SCHEMA, ORDERS),
    POLLED: Answer(rows=_xs(2), polls=2),
    PAGED: Answer(rows=_xs(5), page_size=2),
    SLOW: Answer(rows=_xs(1), polls=1, sleep=20),
    TYPES: Answer(TYPES_SCHEMA, [TYPES_ROW]),
    LATE: Answer(polls=1, late="invalidQuery"),
    INVALID: Answer(error=(400, "invalidQuery")),
    NOT_FOUND: Answer(error=(404, "notFound")),
    DENIED: Answer(error=(403, "accessDenied")),
    BILLED: Answer(error=(400, "bytesBilledLimitExceeded")),
}


@dataclass(frozen=True)
class Account:
    """A service account: its email, key id and RSA key, and the JSON key file Google gives."""

    email: str
    kid: str
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

    def json(self, **update: Any) -> str:
        return json.dumps(
            {
                "type": "service_account",
                "project_id": PROJECT,
                "private_key_id": self.kid,
                "private_key": self.pem,
                "client_email": self.email,
                "client_id": "1",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
            | update
        )


def make_account(email: str = EMAIL, kid: str | None = None) -> Account:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return Account(email, kid or uuid.uuid4().hex, key)


@dataclass
class Job:
    answer: Answer
    params: list[Any]
    polls_left: int


@dataclass
class Seen:
    """What the app saw: every bearer token, the header and claims of each it accepted, each
    query body, each poll's query string, each cancelled job and each job made."""

    bearers: list[str] = field(default_factory=list[str])
    headers: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    claims: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    bodies: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    polls: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    cancels: list[str] = field(default_factory=list[str])
    jobs: dict[str, Job] = field(default_factory=dict[str, Job])


def _error(code: int, reason: str) -> JSONResponse:
    message = f"{reason}: {BODY_MARK}"
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "errors": [{"message": message, "domain": "global", "reason": reason}],
                "status": "INVALID_ARGUMENT",
            }
        },
        status_code=code,
    )


def _rows(job: Job) -> list[dict[str, Any]]:
    if job.params:
        wanted = job.params[0]["parameterValue"]["value"]
        return [r for r in ORDERS if r["f"][0]["v"] == wanted]
    return job.answer.rows


def _complete(job_id: str, job: Job, location: str, offset: int, size: int) -> dict[str, Any]:
    rows = _rows(job)
    size = min(size, job.answer.page_size or size)
    answer: dict[str, Any] = {
        "kind": "bigquery#queryResponse",
        "jobReference": {"projectId": PROJECT, "jobId": job_id, "location": location},
        "jobComplete": True,
        "schema": {"fields": ORDERS_SCHEMA[::3] if job.params else job.answer.schema},
        "totalRows": str(len(rows)),
    }
    found = rows[offset : offset + size]
    if job.params:
        found = [{"f": [r["f"][0], r["f"][3]]} for r in found]
    if found:
        answer["rows"] = found
    if offset + size < len(rows):
        answer["pageToken"] = str(offset + size)
    return answer


def app(account: Account, seen: Seen) -> FastAPI:  # noqa: C901  (one fake API)
    api = FastAPI()
    public = account.key.public_key()

    def verified(request: Request) -> bool:
        bearer = request.headers.get("authorization", "")
        if not bearer.startswith("Bearer "):
            return False
        token = bearer.removeprefix("Bearer ")
        seen.bearers.append(token)
        try:
            header = jwt.get_unverified_header(token)
            claims = jwt.decode(
                token,
                public,
                algorithms=["RS256"],
                audience=AUDIENCE,
                issuer=account.email,
                options={"require": ["iss", "sub", "aud", "iat", "exp"]},
            )
        except jwt.PyJWTError:
            return False
        if header.get("kid") != account.kid or claims["sub"] != account.email:
            return False
        if claims["exp"] - claims["iat"] > 3600:
            return False
        seen.headers.append(header)
        seen.claims.append(claims)
        return True

    @api.post("/bigquery/v2/projects/{project}/queries")
    async def query(project: str, request: Request) -> Any:
        if not verified(request):
            return _error(401, "authError")
        if project != PROJECT:
            return _error(404, "notFound")
        body = await request.json()
        seen.bodies.append(body)
        answer = ANSWERS[READ] if body["query"] == PARAMS else ANSWERS.get(body["query"])
        if answer is None:
            return _error(400, "invalidQuery")
        if answer.error is not None:
            return _error(*answer.error)
        job_id = f"job_{uuid.uuid4().hex}"
        job = Job(answer, body["queryParameters"], answer.polls)
        seen.jobs[job_id] = job
        if job.polls_left:
            return {
                "kind": "bigquery#queryResponse",
                "jobReference": {
                    "projectId": PROJECT,
                    "jobId": job_id,
                    "location": body["location"],
                },
                "jobComplete": False,
            }
        return _complete(job_id, job, body["location"], 0, body["maxResults"])

    @api.get("/bigquery/v2/projects/{project}/queries/{job_id}")
    async def results(project: str, job_id: str, request: Request) -> Any:
        if not verified(request):
            return _error(401, "authError")
        job = seen.jobs.get(job_id) if project == PROJECT else None
        if job is None:
            return _error(404, "notFound")
        params = dict(request.query_params)
        seen.polls.append(params)
        location = params["location"]
        if job.polls_left:
            await asyncio.sleep(job.answer.sleep)
            job.polls_left -= 1
            if job.polls_left:
                return {
                    "jobReference": {"projectId": PROJECT, "jobId": job_id, "location": location},
                    "jobComplete": False,
                }
            if job.answer.late is not None:
                return {
                    "jobReference": {"projectId": PROJECT, "jobId": job_id, "location": location},
                    "jobComplete": True,
                    "errors": [{"reason": job.answer.late, "message": BODY_MARK}],
                }
        offset = int(params.get("pageToken", "0"))
        return _complete(job_id, job, location, offset, int(params["maxResults"]))

    @api.post("/bigquery/v2/projects/{project}/jobs/{job_id}/cancel")
    async def cancel(project: str, job_id: str, request: Request) -> Any:
        if not verified(request):
            return _error(401, "authError")
        if project != PROJECT or job_id not in seen.jobs:
            return _error(404, "notFound")
        seen.cancels.append(job_id)
        return {"kind": "bigquery#jobCancelResponse", "job": {}}

    return api


@dataclass(frozen=True)
class Source:
    """The running app, its PKI, its service account and ways in."""

    pki: Pki
    base_url: str
    account: Account
    seen: Seen

    def target(self, **update: Any) -> BigQueryTarget:
        return BigQueryTarget.model_validate(
            {
                "project": PROJECT,
                "dataset": DATASET,
                "location": LOCATION,
                "service_account": self.account.json(),
            }
            | update
        )

    def connector(self, base_url: str | None = None, **update: Any) -> BigQueryConnector:
        return BigQueryConnector(
            self.target(**update),
            base_url=base_url or self.base_url,
            transport=httpx2.AsyncHTTPTransport(verify=tls_context(self.pki.ca), trust_env=False),
            connect_seconds=2,
        )

    def secrets(self) -> list[str]:
        """Everything of the credential that must never show: the JSON, each line of the key,
        the email, the key id and every JWT sent."""
        return [
            self.account.json(),
            self.account.pem,
            *self.account.body,
            self.account.email,
            self.account.kid,
            *self.seen.bearers,
        ]


@pytest.fixture(scope="module")
def pki() -> Pki:
    return make_pki()


@pytest.fixture(scope="module")
def account() -> Account:
    return make_account()


@pytest.fixture
async def source(pki: Pki, account: Account, tmp_path: Path) -> AsyncIterator[Source]:
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    cert.write_text(pki.cert)
    key.write_text(pki.key)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    seen = Seen()
    config = uvicorn.Config(
        app(account, seen),
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
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
        yield Source(pki, f"https://localhost:{port}", account, seen)
    finally:
        server.should_exit = True
        await task
        sock.close()


def subject(s: Source) -> Subject:
    """The BigQuery connector as the connector suite sees it (GA-5)."""
    target = s.target()
    return Subject(
        connector=s.connector(),
        unreachable=s.connector(base_url="https://localhost:9"),
        credential="".join(s.account.body)[100:140],
        secrets=(target,),
        read=READ,
        read_columns={
            "id": "integer",
            "amount": "decimal",
            "ok": "boolean",
            "note": "string",
            "placed": "timestamp",
        },
        first_row=[1, "1.25", False, "note 1", "2024-01-02T03:04:05.123456+00:00"],
        writes=(
            "INSERT INTO reporting.orders (id) VALUES (1)",
            "UPDATE reporting.orders SET note = 'x' WHERE true",
            "DELETE FROM reporting.orders WHERE true",
            "CREATE TABLE reporting.copy AS SELECT 1 AS x",
            "EXECUTE IMMEDIATE 'DELETE FROM reporting.orders WHERE true'",
            "SELECT 1; SELECT 2",
            "SELECT * FROM EXTERNAL_QUERY('conn', 'DELETE FROM t')",
            "SELECT * FROM other-project.d.t",
        ),
        bad=NOT_FOUND,
        slow=SLOW,
        params=(PARAMS, (2,), [2, "note 2"]),
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_the_bigquery_connector_conforms(
    source: Source, check: Callable[[Subject], Awaitable[None]]
) -> None:
    await conform(check, subject(source))


async def test_a_read_types_every_column(source: Source) -> None:
    columns, rows = await read(source.connector(), ask(TYPES))
    assert [(c.name, c.type, c.db_type) for c in columns] == [
        ("i", "integer", "INT64"),
        ("f", "float", "FLOAT64"),
        ("n", "decimal", "NUMERIC"),
        ("bn", "decimal", "BIGNUMERIC"),
        ("b", "boolean", "BOOL"),
        ("s", "string", "STRING"),
        ("by", "bytes", "BYTES"),
        ("d", "date", "DATE"),
        ("t", "time", "TIME"),
        ("dt", "timestamp", "DATETIME"),
        ("ts", "timestamp", "TIMESTAMP"),
        ("g", "string", "GEOGRAPHY"),
        ("j", "json", "JSON"),
        ("iv", "interval", "INTERVAL"),
        ("r", "string", "RANGE"),
        ("a", "json", "ARRAY<INT64>"),
        ("st", "json", "STRUCT"),
        ("rs", "json", "ARRAY<STRUCT>"),
        ("nul", "string", "STRING"),
    ]
    (found,) = rows
    assert list(found) == [
        -42,
        1.5,
        Decimal("123.456"),
        Decimal("1E+40"),
        True,
        "é",
        b"\x00\x01\xff",
        date(2024, 2, 29),
        clock(23, 59, 58, 500_000),
        datetime(2024, 1, 2, 3, 4, 5, 6),
        datetime(2024, 1, 2, 3, 4, 5, 123_456, tzinfo=UTC),
        "POINT(1 2)",
        {"a": [1, None]},
        "P1Y2M3DT4H5M6.5S",
        "[2024-01-01, UNBOUNDED)",
        [1, 2],
        {"k": "key", "vs": [Decimal("0.10")]},
        [{"d": date(2024, 1, 1)}, {"d": None}],
        None,
    ]
    assert jsonable(found[6]) == "AAH/"


async def test_the_request_carries_the_cost_guard_the_dataset_the_location_and_the_tag(
    source: Source,
) -> None:
    connector = source.connector(max_bytes_billed=5 * 2**20)
    await read(connector, ask(PARAMS, 2, max_rows=7, timeout_ms=4_000, tag="ssc:App1:prod:R-9"))
    (body,) = source.seen.bodies
    assert body == {
        "query": PARAMS,
        "useLegacySql": False,
        "parameterMode": "POSITIONAL",
        "queryParameters": [{"parameterType": {"type": "INT64"}, "parameterValue": {"value": "2"}}],
        "defaultDataset": {"projectId": PROJECT, "datasetId": DATASET},
        "location": LOCATION,
        "maximumBytesBilled": str(5 * 2**20),
        "timeoutMs": 4_000,
        "jobTimeoutMs": "4000",
        "maxResults": 8,
        "formatOptions": {"useInt64Timestamp": True},
        "labels": {"ssc-tag": "ssc_app1_prod_r-9"},
    }


async def test_the_jwt_is_self_signed_for_the_bigquery_api(source: Source) -> None:
    before = int(time.time())
    await read(source.connector(), ask(POLLED))
    assert len(source.seen.claims) == 3, "the query and both polls sent a JWT the fake verified"
    assert len(set(source.seen.bearers)) == 1, "one JWT a read"
    header, claims = source.seen.headers[-1], source.seen.claims[-1]
    assert header["alg"] == "RS256"
    assert header["kid"] == source.account.kid
    assert claims["iss"] == claims["sub"] == EMAIL
    assert claims["aud"] == "https://bigquery.googleapis.com/"
    assert claims["exp"] - claims["iat"] == 3600
    assert before - 5 <= claims["iat"] <= int(time.time()) + 5
    assert set(claims) == {"iss", "sub", "aud", "iat", "exp"}


async def test_an_incomplete_job_is_polled_until_it_completes(
    source: Source, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _, rows = await read(source.connector(), ask(POLLED, max_rows=10, timeout_ms=3_000))
    assert rows == [["x1"], ["x2"]]
    assert (
        source.seen.polls
        == [
            {
                "location": LOCATION,
                "timeoutMs": "3000",
                "maxResults": "11",
                "formatOptions.useInt64Timestamp": "true",
            }
        ]
        * 2
    )
    assert "bigquery read: pages=1 polls=2 bytes=" in caplog.text


async def test_more_pages_follow_the_page_token(source: Source) -> None:
    _, rows = await read(source.connector(), ask(PAGED, max_rows=100))
    assert rows == [["x1"], ["x2"], ["x3"], ["x4"], ["x5"]]
    assert [p.get("pageToken") for p in source.seen.polls] == ["2", "4"]
    assert [p["maxResults"] for p in source.seen.polls] == ["99", "97"]
    source.seen.polls.clear()
    _, rows = await read(source.connector(), ask(PAGED, max_rows=2))
    assert rows == [["x1"], ["x2"], ["x3"]], "max_rows plus one, no more"
    assert [(p["pageToken"], p["maxResults"]) for p in source.seen.polls] == [("2", "1")]


@pytest.mark.parametrize(
    ("sql", "sqlstate", "message"),
    [
        (INVALID, "42601", "bigquery refused the read: invalidQuery"),
        (NOT_FOUND, "42P01", "bigquery refused the read: notFound"),
        (DENIED, "42501", "bigquery refused the read: accessDenied"),
        (BILLED, "53400", "the read would bill more than max_bytes_billed"),
        (LATE, "42601", "bigquery refused the read: invalidQuery"),
    ],
)
async def test_what_bigquery_refuses_is_a_query_failure_by_its_reason(
    source: Source, sql: str, sqlstate: str, message: str
) -> None:
    with pytest.raises(QueryFailedError) as failed:
        await read(source.connector(), ask(sql))
    assert failed.value.sqlstate == sqlstate
    assert str(failed.value) == message


async def test_another_key_or_email_is_28000(source: Source) -> None:
    other_key = make_account(kid=source.account.kid)
    other_email = make_account(email="other@fake-project.iam.gserviceaccount.com")
    for account in (other_key, other_email):
        connector = source.connector(service_account=account.json())
        with pytest.raises(QueryFailedError) as refused:
            await read(connector, ask(READ))
        assert refused.value.sqlstate == "28000"
        assert str(refused.value) == "the source refused the credential"
    assert source.seen.bodies == []


async def test_a_read_past_its_timeout_cancels_its_job(
    source: Source, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="^the read passed timeout_ms$"):
        await read(source.connector(), ask(SLOW, timeout_ms=1_500))
    assert time.monotonic() - started < 8
    assert source.seen.cancels == list(source.seen.jobs), "the job the query made was cancelled"
    assert "could not cancel" not in caplog.text


async def test_a_cancelled_read_cancels_its_job(source: Source) -> None:
    task = asyncio.create_task(read(source.connector(), ask(SLOW, timeout_ms=60_000)))
    await asyncio.sleep(1.0)
    assert source.seen.polls, "the read is waiting on its poll"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(source.seen.cancels) == 1
    assert source.seen.cancels == list(source.seen.jobs)


async def test_a_cancel_that_fails_is_logged_by_class_only(
    account: Account, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    cancels: list[str] = []

    async def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/cancel"):
            cancels.append(request.url.path)
            return httpx2.Response(503, json={"error": {"message": BODY_MARK}})
        if request.method == "POST":
            reference = {"jobId": "job_1", "location": "US"}
            return httpx2.Response(200, json={"jobComplete": False, "jobReference": reference})
        await asyncio.sleep(20)
        return httpx2.Response(500)

    with pytest.raises(TimeoutError):
        await read(_mock(account, answer), ask(READ, timeout_ms=300))
    assert cancels == [f"/bigquery/v2/projects/{PROJECT}/jobs/job_1/cancel"]
    assert "could not cancel the job: UpstreamUnavailableError" in caplog.text
    assert BODY_MARK not in caplog.text


async def test_another_ca_is_refused(source: Source) -> None:
    connector = BigQueryConnector(
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
    stranger = make_account(kid=source.account.kid)
    failing = [
        (source.connector(), ask(sql))
        for sql in (INVALID, NOT_FOUND, DENIED, BILLED, LATE, "DELETE FROM t WHERE true")
    ]
    failing += [
        (source.connector(service_account=stranger.json()), ask(READ)),
        (source.connector(project="other-project"), ask(READ)),
        (source.connector(base_url="https://localhost:9"), ask(READ)),
        (source.connector(), ask(SLOW, timeout_ms=200)),
        (source.connector(), ask(READ, 1)),
        (source.connector(), ask(PARAMS, 2**63)),
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
    assert "bigquery read: pages=1 polls=0 bytes=" in caplog.text
    assert READ not in caplog.text, "no query text in a log line"


def test_a_target_hides_its_service_account(account: Account) -> None:
    target = BigQueryTarget(project=PROJECT, dataset=DATASET, service_account=account.json())  # pyright: ignore[reportArgumentType]
    connector = BigQueryConnector(target)
    for shown in (repr(target), str(target), repr(connector), str(connector)):
        for secret in (account.json(), account.email, account.kid, *account.body):
            assert secret not in shown


def test_a_service_account_that_is_not_a_key_is_refused_without_quoting_it(
    account: Account,
) -> None:
    for service_account in ("not json " + account.email, account.json(private_key_id=7)):
        with pytest.raises(ValidationError) as refused:
            BigQueryTarget(project=PROJECT, dataset=DATASET, service_account=service_account)  # pyright: ignore[reportArgumentType]
        shown = str(refused.value) + "".join(traceback.format_exception(refused.value))
        assert "the service account is not a JSON key" in shown
        for secret in (service_account, account.email, account.kid, *account.body):
            assert secret not in shown


def test_the_target_model_takes_the_control_planes_address(account: Account) -> None:
    target = BigQueryTarget.model_validate(
        {"project": "a2345-z", "dataset": "D_1", "service_account": account.json()}
    )
    assert (target.kind, target.project, target.dataset, target.location) == (
        "bigquery",
        "a2345-z",
        "D_1",
        "US",
    )
    assert target.max_bytes_billed == 2**30
    for bytes_billed in (2**20, 2**40):
        assert (
            BigQueryTarget.model_validate(
                {"project": PROJECT, "dataset": DATASET, "service_account": account.json()}
                | {"max_bytes_billed": bytes_billed, "location": "europe-west2"}
            ).max_bytes_billed
            == bytes_billed
        )


@pytest.mark.parametrize(
    "update",
    [
        {"project": "abcd"},
        {"project": "Fake-project"},
        {"project": "fake-project-"},
        {"project": "1fake-project"},
        {"project": "a" * 31},
        {"dataset": ""},
        {"dataset": "a-b"},
        {"dataset": "a.b"},
        {"location": ""},
        {"location": "us/central"},
        {"location": "a" * 33},
        {"max_bytes_billed": 2**20 - 1},
        {"max_bytes_billed": 2**40 + 1},
        {"kind": "gsheets"},
        {"extra": 1},
    ],
)
def test_the_target_model_refuses_a_bad_address(account: Account, update: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BigQueryTarget.model_validate(
            {"project": PROJECT, "dataset": DATASET, "service_account": account.json()} | update
        )


def _mock(
    account: Account,
    answer: Callable[[httpx2.Request], httpx2.Response | Awaitable[httpx2.Response]],
    **update: Any,
) -> BigQueryConnector:
    target = BigQueryTarget.model_validate(
        {"project": PROJECT, "dataset": DATASET, "service_account": account.json()} | update
    )
    return BigQueryConnector(target, transport=httpx2.MockTransport(answer))  # pyright: ignore[reportArgumentType]


def _done(rows: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    reference = {"projectId": PROJECT, "jobId": "job_1", "location": "US"}
    body = {"jobComplete": True, "jobReference": reference, "schema": {"fields": X_SCHEMA}}
    return body | ({"rows": rows} if rows is not None else {}) | extra


async def test_the_request_is_one_post_to_jobs_query_on_googles_host(account: Account) -> None:
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json=_done(_xs(1)))

    _, rows = await read(_mock(account, answer), ask("SELECT 'a' AS x"))
    assert rows == [["x1"]]
    (request,) = sent
    assert request.method == "POST"
    assert (
        str(request.url)
        == f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/queries"
    )
    assert request.headers["authorization"].startswith("Bearer ey")
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == "ssc-datagw"
    body = json.loads(request.content)
    assert (body["location"], body["queryParameters"]) == ("US", [])


async def test_a_parameter_count_that_differs_is_07001_before_the_request(
    account: Account,
) -> None:
    sent: list[httpx2.Request] = []
    connector = _mock(account, sent.append)  # pyright: ignore[reportArgumentType]
    for sql, params in ((PARAMS, ()), (PARAMS, (1, 2)), ("SELECT '?' AS x -- ?", (1,))):
        with pytest.raises(QueryFailedError) as failed:
            await read(connector, ask(sql, *params))
        assert failed.value.sqlstate == "07001"
    with pytest.raises(QueryFailedError) as wide:
        await read(connector, ask(PARAMS, -(2**63) - 1))
    assert wide.value.sqlstate == "22003"
    with pytest.raises(QueryRefusedError):
        await read(connector, ask("SELECT SESSION_USER()"))
    assert sent == []


@pytest.mark.parametrize(
    ("status", "reason", "error", "sqlstate"),
    [
        (400, "invalidQuery", QueryFailedError, "42601"),
        (404, "notFound", QueryFailedError, "42P01"),
        (403, "accessDenied", QueryFailedError, "42501"),
        (403, "billingNotEnabled", QueryFailedError, "42501"),
        (403, "billingTierLimitExceeded", QueryFailedError, "42501"),
        (403, None, QueryFailedError, "42501"),
        (401, "authError", QueryFailedError, "28000"),
        (401, None, QueryFailedError, "28000"),
        (400, "bytesBilledLimitExceeded", QueryFailedError, "53400"),
        (403, "responseTooLarge", QueryFailedError, "54000"),
        (400, "stopped", QueryFailedError, "57014"),
        (400, "invalid", QueryFailedError, "22023"),
        (403, "rateLimitExceeded", UpstreamUnavailableError, None),
        (403, "quotaExceeded", UpstreamUnavailableError, None),
        (500, "backendError", UpstreamUnavailableError, None),
        (400, "jobRateLimitExceeded", UpstreamUnavailableError, None),
        (500, "internalError", UpstreamUnavailableError, None),
        (429, None, UpstreamUnavailableError, None),
        (503, None, UpstreamUnavailableError, None),
        (400, "timeout", TimeoutError, None),
        (409, "duplicate", QueryFailedError, None),
        (302, None, QueryFailedError, None),
    ],
)
async def test_each_status_and_reason_maps_to_its_error(
    account: Account, status: int, reason: str | None, error: type[Exception], sqlstate: str | None
) -> None:
    errors = [{"reason": reason, "message": BODY_MARK}] if reason else []
    body = {"error": {"code": status, "message": BODY_MARK, "errors": errors}}
    connector = _mock(account, lambda _: httpx2.Response(status, json=body))
    with pytest.raises(error) as failed:
        await read(connector, ask("SELECT 1"))
    assert getattr(failed.value, "sqlstate", None) == sqlstate
    assert BODY_MARK not in str(failed.value)
    if isinstance(failed.value, QueryFailedError) and sqlstate not in ("28000", "53400"):
        assert str(failed.value).endswith(reason or "unknown")


async def test_a_reason_that_is_not_a_plain_word_is_not_quoted(account: Account) -> None:
    assert str(failure(400, None)) == "bigquery answered 400: unknown"
    for reason in ("x y <script>", 7, "a" * 65):
        body = {"error": {"errors": [{"reason": reason}]}}
        connector = _mock(account, lambda _, b=body: httpx2.Response(400, json=b))
        with pytest.raises(QueryFailedError, match="^bigquery answered 400: unknown$"):
            await read(connector, ask("SELECT 1"))
    connector = _mock(account, lambda _: httpx2.Response(502, content=b"<html>"))
    with pytest.raises(UpstreamUnavailableError, match="^bigquery answered 502: unknown$"):
        await read(connector, ask("SELECT 1"))


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b"{}",
        b'{"jobComplete": "yes"}',
        b'{"jobComplete": false}',
        b'{"jobComplete": false, "jobReference": {"jobId": "../x"}}',
        b'{"jobComplete": false, "jobReference": {"jobId": "j", "location": "a/b"}}',
        b'{"jobComplete": true}',
        b'{"jobComplete": true, "schema": {"fields": {}}}',
        b'{"jobComplete": true, "schema": {"fields": [{"name": "x"}]}}',
        b'{"jobComplete": true, "schema": {"fields": [{"name": "x", "type": "RECORD"}]}}',
        b'{"jobComplete": true, "schema": {"fields": [{"name": "x", "type": "STRING"}]},'
        b' "rows": {}}',
        b'{"jobComplete": true, "schema": {"fields": [{"name": "x", "type": "STRING"}]},'
        b' "rows": [{"f": []}]}',
        b'{"jobComplete": true, "schema": {"fields": [{"name": "x", "type": "STRING"}]},'
        b' "rows": [{"f": [{"w": 1}]}]}',
        b'{"jobComplete": true, "schema": {"fields": [{"name": "x", "type": "STRING"}]},'
        b' "rows": [{"f": [{"v": 1}]}]}',
        b'{"jobComplete": true, "schema": {"fields": [{"name": "x", "type": "INT64"}]},'
        b' "rows": [{"f": [{"v": "1.5"}]}]}',
        b'{"jobComplete": true, "schema": {"fields": [{"name": "x", "type": "STRING"}]},'
        b' "pageToken": 7}',
    ],
)
async def test_a_body_that_is_not_a_bigquery_result_is_22p02(account: Account, body: bytes) -> None:
    connector = _mock(account, lambda _: httpx2.Response(200, content=body))
    with pytest.raises(QueryFailedError) as odd:
        await read(connector, ask("SELECT 1"))
    assert odd.value.sqlstate == "22P02"
    assert str(odd.value) == "the body is not a BigQuery result"


def test_errors_beside_a_schema_are_warnings() -> None:
    found = page(_done(_xs(1), errors=[{"reason": "invalidQuery"}]), "US")
    assert found.complete
    assert found.rows == _xs(1)


def test_query_parameters_are_typed_by_value() -> None:
    assert query_parameters([True, False, 7, -(2**63), 2**63 - 1, 1.5, "s", None]) == [
        {"parameterType": {"type": "BOOL"}, "parameterValue": {"value": "true"}},
        {"parameterType": {"type": "BOOL"}, "parameterValue": {"value": "false"}},
        {"parameterType": {"type": "INT64"}, "parameterValue": {"value": "7"}},
        {"parameterType": {"type": "INT64"}, "parameterValue": {"value": str(-(2**63))}},
        {"parameterType": {"type": "INT64"}, "parameterValue": {"value": str(2**63 - 1)}},
        {"parameterType": {"type": "FLOAT64"}, "parameterValue": {"value": "1.5"}},
        {"parameterType": {"type": "STRING"}, "parameterValue": {"value": "s"}},
        {"parameterType": {"type": "STRING"}, "parameterValue": {"value": None}},
    ]
    assert query_parameters([float("nan"), float("inf"), float("-inf")]) == [
        {"parameterType": {"type": "FLOAT64"}, "parameterValue": {"value": v}}
        for v in ("NaN", "Infinity", "-Infinity")
    ]
    for wide in (2**63, -(2**63) - 1):
        with pytest.raises(QueryFailedError) as failed:
            query_parameters([wide])
        assert failed.value.sqlstate == "22003"


def test_placeholders_are_counted_as_bigquery_reads_them() -> None:
    assert placeholders("SELECT ?, ? FROM t WHERE a = ?") == 3
    assert placeholders("SELECT '?', \"?\", r'?', '''?''' -- ?\n/* ? */") == 0
    assert placeholders("SELECT `?` FROM t") == 0


def test_the_label_is_the_tag_made_safe_for_a_label_value() -> None:
    assert label("ssc:app_1:Prod:0f-9") == "ssc_app_1_prod_0f-9"
    assert label("ssc:é:x y") == "ssc___x_y"
    assert label("ssc:" + "a" * 100) == "ssc_" + "a" * 59
    assert len(label("x" * 200)) == 63


@pytest.mark.parametrize(
    ("kind", "text", "expected"),
    [
        ("INT64", "-9223372036854775808", -(2**63)),
        ("FLOAT64", "1e-3", 0.001),
        ("FLOAT64", "Infinity", float("inf")),
        ("FLOAT64", "-Infinity", float("-inf")),
        ("NUMERIC", "-0.000000001", Decimal("-0.000000001")),
        (
            "BIGNUMERIC",
            "5.7896044618658097711785492504343953926634992332820282019728792003956564819967E+38",
            Decimal(
                "5.7896044618658097711785492504343953926634992332820282019728792003956564819967E+38"
            ),
        ),
        ("BOOL", "false", False),
        ("STRING", "", ""),
        ("BYTES", "", b""),
        ("DATE", "0001-01-01", date(1, 1, 1)),
        ("TIME", "00:00:00", clock(0)),
        ("DATETIME", "9999-12-31T23:59:59.999999", datetime(9999, 12, 31, 23, 59, 59, 999_999)),
        ("DATETIME", "2024-01-02 03:04:05", datetime(2024, 1, 2, 3, 4, 5)),
        ("TIMESTAMP", "0", datetime(1970, 1, 1, tzinfo=UTC)),
        ("TIMESTAMP", "-1", datetime(1969, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC)),
        ("TIMESTAMP", "1.704164645123456E9", datetime(2024, 1, 2, 3, 4, 5, 123_456, tzinfo=UTC)),
        ("TIMESTAMP", "1704164645.5", datetime(2024, 1, 2, 3, 4, 5, 500_000, tzinfo=UTC)),
        ("JSON", "null", None),
        ("JSON", '"s"', "s"),
        ("GEOGRAPHY", "POINT(0 0)", "POINT(0 0)"),
        ("INTERVAL", "0-0 0 0:0:0", "PT0S"),
        ("INTERVAL", "1-0 0 0:0:0", "P1Y"),
        ("INTERVAL", "-1-2 3 -4:5:6.000001", "P-1Y-2M3DT-4H-5M-6.000001S"),
        ("INTERVAL", "0-0 -10 0:0:0", "P-10D"),
        ("INTERVAL", "0-0 0 0:30:0", "PT30M"),
        ("RANGE", "[1, 2)", "[1, 2)"),
    ],
)
def test_each_value_converts_by_its_columns_type(kind: str, text: str, expected: object) -> None:
    assert scalar(kind, text) == expected


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("INT64", "1.0"),
        ("INT64", ""),
        ("FLOAT64", "one"),
        ("NUMERIC", "NaN"),
        ("NUMERIC", "x"),
        ("BOOL", "TRUE"),
        ("BOOL", "1"),
        ("BYTES", "not base64!"),
        ("DATE", "2024-02-30"),
        ("TIME", "25:00:00"),
        ("TIMESTAMP", "Infinity"),
        ("TIMESTAMP", "soon"),
        ("TIMESTAMP", "1" * 30),
        ("JSON", "{"),
        ("INTERVAL", "P1Y"),
        ("INTERVAL", "1-2 3"),
    ],
)
def test_a_value_that_does_not_convert_is_22p02(kind: str, text: str) -> None:
    fields = schema_fields([{"name": "x", "type": kind}])
    with pytest.raises(QueryFailedError) as odd:
        row(fields, {"f": [{"v": text}]})
    assert odd.value.sqlstate == "22P02"


def test_interval_is_an_iso_8601_duration() -> None:
    assert interval("10000-0 3660000 87840000:0:0") == "P10000Y3660000DT87840000H"
    assert interval("0-11 0 -0:0:0.5") == "P11MT-0.5S"


def test_nested_values_unwrap_into_plain_json() -> None:
    fields = schema_fields(
        [
            {
                "name": "outer",
                "type": "RECORD",
                "mode": "REPEATED",
                "fields": [
                    {"name": "tags", "type": "STRING", "mode": "REPEATED"},
                    {
                        "name": "inner",
                        "type": "STRUCT",
                        "fields": [{"name": "at", "type": "TIMESTAMP"}],
                    },
                ],
            }
        ]
    )
    (column,) = fields
    assert (column.db_type, column.portable) == ("ARRAY<STRUCT>", "json")
    raw = {
        "f": [
            {
                "v": [
                    {"v": {"f": [{"v": [{"v": "a"}, {"v": "b"}]}, {"v": {"f": [{"v": "0"}]}}]}},
                    {"v": {"f": [{"v": []}, {"v": None}]}},
                ]
            }
        ]
    }
    (value,) = row(fields, raw)
    assert jsonable(value) == [
        {"tags": ["a", "b"], "inner": {"at": "1970-01-01T00:00:00+00:00"}},
        {"tags": [], "inner": None},
    ]
    for bad in ({"f": [{"v": {"v": 1}}]}, {"f": [{"v": [{"v": {"f": []}}]}]}):
        with pytest.raises(QueryFailedError):
            row(fields, bad)


def test_db_type_takes_googlesqls_names() -> None:
    fields = schema_fields(
        [
            {"name": "a", "type": "integer"},
            {"name": "b", "type": "FLOAT", "mode": "REPEATED"},
            {"name": "c", "type": "BOOLEAN"},
            {"name": "d", "type": "RECORD", "fields": []},
            {"name": "e", "type": "STRUCT", "mode": "repeated", "fields": []},
            {"name": "f", "type": "NEWTYPE"},
        ]
    )
    assert [(f.db_type, f.portable) for f in fields] == [
        ("INT64", "integer"),
        ("ARRAY<FLOAT64>", "json"),
        ("BOOL", "boolean"),
        ("STRUCT", "json"),
        ("ARRAY<STRUCT>", "json"),
        ("NEWTYPE", "string"),
    ]
    assert fields[0] == SchemaField("a", "INT64")
    assert [Column(f.name, f.portable, f.db_type) for f in fields[:1]] == [
        Column("a", "integer", "INT64")
    ]


def test_pages_with_rows_from_a_complete_job() -> None:
    found = page(_done(_xs(2), pageToken="t"), "EU")
    assert (found.complete, found.token, found.fields) == (True, "t", (SchemaField("x", "STRING"),))
    assert found.job is not None and found.job.job_id == "job_1"
    empty = page(_done(), "EU")
    assert empty.rows == []
    pending = page({"jobComplete": False, "jobReference": {"jobId": "j-1"}}, "EU")
    assert pending.job is not None and pending.job.location == "EU", "the target's, when left out"


def test_a_null_cell_is_none() -> None:
    fields: Sequence[SchemaField] = schema_fields(X_SCHEMA)
    assert row(fields, {"f": [{"v": None}]}) == [None]
