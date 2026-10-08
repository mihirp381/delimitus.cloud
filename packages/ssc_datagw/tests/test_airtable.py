"""The Airtable connector against a contract fake of the Airtable REST API (GA-5 B9).

The fake is a FastAPI app under ``uvicorn`` on 127.0.0.1 over TLS from the test PKI, as for
REST and S3. It checks the bearer token (401 ``AUTHENTICATION_REQUIRED`` otherwise) and serves
base ``appA1b2C3d4E5f6G7``: ``Orders`` (three records of mixed field types, a field missing from
one, an attachment list, a linked-record list, a checkbox, a number, a date string), ``Many``
(250 records, paged by ``offset``), ``Slow`` (20 s), ``Busy`` (429 once), ``Boom`` (500),
``Text`` and ``Odd`` (bodies that are not a record list). A formula holding ``bad(`` is 422
``INVALID_FILTER_BY_FORMULA``; another table is 403 ``INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND`` and
another base 404 ``NOT_FOUND``, as live Airtable answers them. The base's schema
(``/v0/meta/bases/{base}/tables``) lists ``Orders`` and ``Many``, or answers 403 as for a token
without ``schema.bases:read``. The connector suite runs against it; the grammar, the target, the
records and the error mapping are unit-tested without it."""

import asyncio
import logging
import socket
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest
import uvicorn
from connector_suite import CHECKS, Subject, ask, conform, read
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pki import Pki, make_pki
from pydantic import ValidationError

from ssc_datagw.airtable import (
    PAGE_PAUSE_SECONDS,
    AirtableConnector,
    AirtableTarget,
    Record,
    airtable_failure,
    airtable_request,
    error_type,
    records_page,
    table_of,
)
from ssc_datagw.connectors import (
    Column,
    QueryFailedError,
    QueryRefusedError,
    UpstreamUnavailableError,
)

TOKEN = "pat" + "FakeAirtable01." + "0123456789abcdef" * 4
BASE_ID = "appA1b2C3d4E5f6G7"
FORMULA_MESSAGE = "The formula for filtering records is invalid: Unknown function names: bad"
PHOTO = {"id": "attA1b2C3d4E5f6G7", "url": "https://dl.example/w.png", "filename": "w.png"}
ORDERS: list[dict[str, Any]] = [
    {
        "id": "rec00000000000001",
        "createdTime": "2026-01-02T03:04:05.000Z",
        "fields": {
            "Name": "widget",
            "Amount": 2.5,
            "Paid": True,
            "Placed": "2026-01-02",
            "Photos": [PHOTO],
            "Customer": ["recC0000000000001"],
        },
    },
    {
        "id": "rec00000000000002",
        "createdTime": "2026-01-03T00:00:00.000Z",
        "fields": {"Name": "gadget", "Amount": 10, "Placed": "2026-01-03", "Customer": []},
    },
    {
        "id": "rec00000000000003",
        "createdTime": "2026-01-04T12:30:00.000Z",
        "fields": {
            "Name": "gizmo",
            "Amount": 7.25,
            "Paid": True,
            "Placed": "2026-01-04",
            "Customer": ["recC0000000000002"],
            "Notes": "late",
        },
    },
]
ORDERS_COLUMNS = [
    Column("id", "string", "string"),
    Column("created_time", "timestamp", "timestamp"),
    Column("Name", "string", "string"),
    Column("Amount", "float", "number"),
    Column("Paid", "boolean", "boolean"),
    Column("Placed", "string", "string"),
    Column("Photos", "json", "array"),
    Column("Customer", "json", "array"),
    Column("Notes", "string", "string"),
]
FIRST_ROW = [
    "rec00000000000001",
    "2026-01-02T03:04:05+00:00",
    "widget",
    2.5,
    True,
    "2026-01-02",
    [PHOTO],
    ["recC0000000000001"],
    None,
]
SCHEMA: list[dict[str, Any]] = [
    {
        "id": "tblOrders00000001",
        "name": "Orders",
        "primaryFieldId": "fldName0000000001",
        "fields": [
            {"id": "fldName0000000001", "name": "Name", "type": "singleLineText"},
            {
                "id": "fldAmnt0000000001",
                "name": "Amount",
                "type": "currency",
                "options": {"precision": 2, "symbol": "$"},
            },
            {
                "id": "fldQty00000000001",
                "name": "Qty",
                "type": "number",
                "options": {"precision": 0},
            },
            {
                "id": "fldRate0000000001",
                "name": "Rate",
                "type": "number",
                "options": {"precision": 2},
            },
            {"id": "fldPaid0000000001", "name": "Paid", "type": "checkbox"},
            {"id": "fldPlcd0000000001", "name": "Placed", "type": "date"},
            {"id": "fldAt000000000001", "name": "At", "type": "dateTime"},
            {"id": "fldPhto0000000001", "name": "Photos", "type": "multipleAttachments"},
            {"id": "fldCust0000000001", "name": "Customer", "type": "multipleRecordLinks"},
            {"id": "fldNote0000000001", "name": "Notes", "type": "multilineText"},
            {"id": "fldId000000000001", "name": "id", "type": "autoNumber"},
        ],
        "views": [{"id": "viwGrid0000000001", "name": "Grid view", "type": "grid"}],
    },
    {
        "id": "tblMany0000000001",
        "name": "Many",
        "primaryFieldId": "fldN0000000000001",
        "fields": [{"id": "fldN0000000000001", "name": "n", "type": "count"}],
        "views": [],
    },
]
SCHEMA_COLUMNS = [
    Column("id", "string", "string"),
    Column("created_time", "timestamp", "timestamp"),
    Column("Name", "string", "singleLineText"),
    Column("Amount", "float", "currency"),
    Column("Qty", "integer", "number"),
    Column("Rate", "float", "number"),
    Column("Paid", "boolean", "checkbox"),
    Column("Placed", "date", "date"),
    Column("At", "timestamp", "dateTime"),
    Column("Photos", "json", "multipleAttachments"),
    Column("Customer", "json", "multipleRecordLinks"),
    Column("Notes", "string", "multilineText"),
    Column("id_2", "float", "autoNumber"),
]
MANY = [
    {
        "id": f"rec{i:014}",
        "createdTime": (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i)).isoformat(),
        "fields": {"n": i},
    }
    for i in range(250)
]


@dataclass
class Seen:
    """What the fake saw: each request's raw path and query, ``Authorization`` and agent."""

    requests: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    authorizations: list[str] = field(default_factory=list[str])
    agents: list[str] = field(default_factory=list[str])
    busy: int = 0
    no_schema_scope: bool = False
    slow_schema: bool = False


def _error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"type": kind, "message": message}}, status_code=status)


def _page(records: list[dict[str, Any]], request: Request) -> JSONResponse:
    """Airtable's paging: ``pageSize`` 1 to 100, an opaque ``offset`` holding a ``/``."""
    params = request.query_params
    size = int(params.get("pageSize", "100"))
    if not 1 <= size <= 100:
        return _error(422, "INVALID_PAGE_SIZE", "page size out of range")
    offset = params.get("offset")
    start = int(offset.removeprefix("itr").partition("/")[0]) if offset else 0
    end = start + size
    body: dict[str, Any] = {"records": records[start:end]}
    if end < len(records):
        body["offset"] = f"itr{end}/rec{end}"
    return JSONResponse(body)


def fake_airtable(seen: Seen) -> FastAPI:
    api = FastAPI()

    @api.get("/v0/meta/bases/{base}/tables")
    async def schema(base: str, request: Request) -> Any:
        seen.requests.append(
            (request.scope["raw_path"].decode(), request.scope["query_string"].decode())
        )
        seen.authorizations.append(request.headers.get("authorization", ""))
        seen.agents.append(request.headers.get("user-agent", ""))
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return _error(401, "AUTHENTICATION_REQUIRED", "Authentication required")
        if base != BASE_ID:
            return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
        if seen.no_schema_scope:
            return _error(403, "INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND", "Invalid permissions")
        if seen.slow_schema:
            await asyncio.sleep(20)
        return {"tables": SCHEMA}

    @api.get("/v0/{base}/{table:path}")
    async def records(base: str, table: str, request: Request) -> Any:
        seen.requests.append(
            (request.scope["raw_path"].decode(), request.scope["query_string"].decode())
        )
        seen.authorizations.append(request.headers.get("authorization", ""))
        seen.agents.append(request.headers.get("user-agent", ""))
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return _error(401, "AUTHENTICATION_REQUIRED", "Authentication required")
        if base != BASE_ID:
            return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
        if "bad(" in request.query_params.get("filterByFormula", ""):
            return _error(422, "INVALID_FILTER_BY_FORMULA", FORMULA_MESSAGE)
        if table in {"Orders", "Order Items/2026"}:
            return _page(ORDERS, request)
        if table == "Many":
            return _page(MANY, request)
        if table == "Slow":
            await asyncio.sleep(20)
            return _page(ORDERS, request)
        if table == "Busy":
            seen.busy += 1
            if seen.busy == 1:
                return _error(429, "TOO_MANY_REQUESTS", "Too many requests")
            return _page(ORDERS, request)
        if table == "Boom":
            return _error(500, "SERVER_ERROR", "boom")
        if table == "Text":
            return PlainTextResponse("not json")
        if table == "Odd":
            return JSONResponse({"rows": []})
        return _error(403, "INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND", "Invalid permissions")

    return api


@dataclass(frozen=True)
class Source:
    """The running fake, its PKI and ways in."""

    pki: Pki
    base_url: str
    seen: Seen

    def target(self, **update: Any) -> AirtableTarget:
        return AirtableTarget.model_validate({"base_id": BASE_ID, "token": TOKEN} | update)

    def connector(self, *, ca: str | None = "", **update: Any) -> AirtableConnector:
        return AirtableConnector(
            self.target(**update),
            base_url=self.base_url,
            ca=self.pki.ca if ca == "" else ca,
        )


@pytest.fixture(scope="module")
def pki() -> Pki:
    return make_pki()


@pytest.fixture
async def fake(pki: Pki, tmp_path: Path) -> AsyncIterator[Source]:
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    cert.write_text(pki.cert)
    key.write_text(pki.key)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    seen = Seen()
    config = uvicorn.Config(
        fake_airtable(seen),
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
        yield Source(pki, f"https://localhost:{port}", seen)
    finally:
        server.should_exit = True
        await task
        sock.close()


def subject(s: Source) -> Subject:
    """The Airtable connector as the connector suite sees it (GA-5)."""
    target = s.target()
    connector = AirtableConnector(target, base_url=s.base_url, ca=s.pki.ca)
    nowhere = AirtableConnector(
        target, base_url="https://localhost:9", ca=s.pki.ca, connect_seconds=2
    )
    return Subject(
        connector=connector,
        unreachable=nowhere,
        credential=TOKEN,
        secrets=(target, connector, nowhere),
        read="Orders",
        read_columns={c.name: c.type for c in ORDERS_COLUMNS},
        first_row=FIRST_ROW,
        writes=(
            "insert Orders",
            "delete Orders",
            "update Orders",
            "SELECT * FROM Orders",
            "Orders; drop",
        ),
        bad="Missing",
        slow="Slow",
        params=None,
        table="Orders",
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_the_airtable_connector_conforms_on_the_fake(
    fake: Source, check: Callable[[Subject], Awaitable[None]]
) -> None:
    await conform(check, subject(fake))


async def test_a_description_types_each_field_by_its_airtable_type(fake: Source) -> None:
    tables = await fake.connector().describe(schemas=["x"], timeout_ms=5_000)
    assert [t.name for t in tables] == ["Orders", "Many"]
    assert list(tables[0].columns) == SCHEMA_COLUMNS
    assert list(tables[1].columns)[2:] == [Column("n", "float", "count")]
    assert fake.seen.requests == [(f"/v0/meta/bases/{BASE_ID}/tables", "")]
    assert fake.seen.authorizations == [f"Bearer {TOKEN}"]
    assert fake.seen.agents == ["ssc-datagw (ssc:describe)"]


@pytest.mark.parametrize("table", ["Many", "tblMany0000000001"])
async def test_a_description_of_a_connection_with_a_table_names_it_alone(
    fake: Source, table: str
) -> None:
    (found,) = await fake.connector(table=table).describe(schemas=None, timeout_ms=5_000)
    assert found.name == "Many"


async def test_a_description_stops_at_500_tables_of_500_columns(
    fake: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    def table(n: int, fields: int) -> dict[str, Any]:
        return {
            "id": f"tbl{n:014}",
            "name": f"T{n:03}",
            "fields": [
                {"id": f"fld{i:014}", "name": f"f{i}", "type": "rating"} for i in range(fields)
            ],
        }

    monkeypatch.setattr(
        f"{__name__}.SCHEMA", [table(0, 600), *(table(n, 1) for n in range(1, 503))]
    )
    tables = await fake.connector().describe(schemas=None, timeout_ms=5_000)
    assert len(tables) == 500
    assert (tables[0].name, len(tables[0].columns)) == ("T000", 500)
    assert tables[0].columns[-1] == Column("f497", "float", "rating")
    assert tables[-1].name == "T499"


async def test_a_description_past_its_timeout_times_out(fake: Source) -> None:
    fake.seen.slow_schema = True
    with pytest.raises(TimeoutError):
        await fake.connector().describe(schemas=None, timeout_ms=500)


async def test_a_token_without_schema_bases_read_is_42501_without_the_token(fake: Source) -> None:
    fake.seen.no_schema_scope = True
    with pytest.raises(QueryFailedError) as denied:
        await fake.connector().describe(schemas=None, timeout_ms=5_000)
    assert denied.value.sqlstate == "42501"
    assert str(denied.value) == "the source answered 403 INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND"
    assert TOKEN not in "".join(traceback.format_exception(denied.value))


async def test_records_are_rows_with_fields_in_order_of_first_appearance(fake: Source) -> None:
    columns, rows = await read(fake.connector(), ask("Orders"))
    assert columns == ORDERS_COLUMNS
    created = [datetime.fromisoformat(r["createdTime"]) for r in ORDERS]
    assert [list(r) for r in rows] == [
        [
            "rec00000000000001",
            created[0],
            "widget",
            2.5,
            True,
            "2026-01-02",
            [PHOTO],
            ["recC0000000000001"],
            None,
        ],
        ["rec00000000000002", created[1], "gadget", 10, None, "2026-01-03", None, [], None],
        [
            "rec00000000000003",
            created[2],
            "gizmo",
            7.25,
            True,
            "2026-01-04",
            None,
            ["recC0000000000002"],
            "late",
        ],
    ]
    assert all(isinstance(r[1], datetime) and r[1].tzinfo == UTC for r in rows)


async def test_a_read_pages_by_offset_to_max_rows_plus_one(fake: Source) -> None:
    pauses: list[float] = []

    async def pause(seconds: float) -> None:
        pauses.append(seconds)

    target = fake.target()
    connector = AirtableConnector(target, base_url=fake.base_url, ca=fake.pki.ca, sleep=pause)
    _, rows = await read(connector, ask("Many", max_rows=10))
    assert [r[2] for r in rows] == list(range(11))
    assert fake.seen.requests == [(f"/v0/{BASE_ID}/Many", "pageSize=11")]
    assert pauses == []

    fake.seen.requests.clear()
    _, rows = await read(connector, ask("Many", max_rows=1000))
    assert [r[2] for r in rows] == list(range(250))
    assert [q for _, q in fake.seen.requests] == [
        "pageSize=100",
        "pageSize=100&offset=itr100%2Frec100",
        "pageSize=100&offset=itr200%2Frec200",
    ]
    assert pauses == [PAGE_PAUSE_SECONDS, PAGE_PAUSE_SECONDS]
    assert PAGE_PAUSE_SECONDS == 0.2

    fake.seen.requests.clear()
    _, rows = await read(connector, ask("Many", max_rows=100))
    assert len(rows) == 101
    assert [q for _, q in fake.seen.requests] == [
        "pageSize=100",
        "pageSize=1&offset=itr100%2Frec100",
    ]


async def test_a_view_and_a_formula_reach_the_query_string(fake: Source) -> None:
    formula = "AND({Paid}, {Amount} > 5, {Name} != 'a view & where')"
    sql = f"Order Items/2026 VIEW Paid & open Where {formula}"
    _, rows = await read(fake.connector(), ask(sql))
    assert len(rows) == 3
    ((path, query),) = fake.seen.requests
    assert path == f"/v0/{BASE_ID}/Order%20Items%2F2026"
    assert parse_qs(query) == {
        "pageSize": ["100"],
        "view": ["Paid & open"],
        "filterByFormula": [formula],
    }
    assert "%7BPaid%7D" in query


async def test_a_refused_formula_names_its_type_and_not_its_message(fake: Source) -> None:
    with pytest.raises(QueryFailedError) as refused:
        await read(fake.connector(), ask("Orders where bad({Name})"))
    assert refused.value.sqlstate == "42601"
    assert str(refused.value) == "the source answered 422 INVALID_FILTER_BY_FORMULA"
    shown = "".join(traceback.format_exception(refused.value)) + repr(refused.value)
    assert FORMULA_MESSAGE not in shown
    assert "Unknown function" not in shown


async def test_rate_limiting_and_a_5xx_are_unavailable(fake: Source) -> None:
    with pytest.raises(UpstreamUnavailableError) as busy:
        await read(fake.connector(), ask("Busy"))
    assert str(busy.value) == "the source answered 429 TOO_MANY_REQUESTS"
    assert fake.seen.busy == 1, "a 429 is not retried"
    _, rows = await read(fake.connector(), ask("Busy"))
    assert len(rows) == 3
    with pytest.raises(UpstreamUnavailableError) as boom:
        await read(fake.connector(), ask("Boom"))
    assert str(boom.value) == "the source answered 500 SERVER_ERROR"


async def test_a_wrong_token_is_28000(fake: Source) -> None:
    wrong = "pat" + "WrongToken0000." + "f" * 64
    with pytest.raises(QueryFailedError) as refused:
        await read(fake.connector(token=wrong), ask("Orders"))
    assert refused.value.sqlstate == "28000"
    assert str(refused.value) == "the source answered 401 AUTHENTICATION_REQUIRED"


async def test_another_table_is_42501_and_another_base_42p01(fake: Source) -> None:
    with pytest.raises(QueryFailedError) as table:
        await read(fake.connector(), ask("Missing"))
    assert table.value.sqlstate == "42501"
    assert str(table.value) == "the source answered 403 INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND"
    with pytest.raises(QueryFailedError) as base:
        await read(fake.connector(base_id="appZZZZZZZZZZZZZZ"), ask("Orders"))
    assert base.value.sqlstate == "42P01"
    assert str(base.value) == "the source answered 404 NOT_FOUND"


@pytest.mark.parametrize(
    ("table", "message"),
    [("Text", "the body is not JSON"), ("Odd", "the body is not an Airtable record list")],
)
async def test_a_body_that_is_not_a_record_list_is_22p02(
    fake: Source, table: str, message: str
) -> None:
    with pytest.raises(QueryFailedError) as corrupt:
        await read(fake.connector(), ask(table))
    assert corrupt.value.sqlstate == "22P02"
    assert str(corrupt.value) == message


async def test_another_table_than_the_connections_is_refused_before_the_source(
    fake: Source,
) -> None:
    connector = fake.connector(table="Orders")
    for sql in ("Many", "orders", "tblA1b2C3d4E5f6G7"):
        with pytest.raises(QueryRefusedError, match="^the query's table is not the connection"):
            await read(connector, ask(sql))
    assert fake.seen.requests == []
    _, rows = await read(connector, ask("Orders where {Paid}"))
    assert len(rows) == 3


async def test_another_ca_or_the_system_store_is_refused(fake: Source) -> None:
    for connector in (fake.connector(ca=fake.pki.other_ca), fake.connector(ca=None)):
        with pytest.raises(UpstreamUnavailableError, match="^cannot connect: "):
            await read(connector, ask("Orders"))
    assert fake.seen.requests == [], "no request crossed a refused handshake"


async def test_the_tag_travels_in_the_user_agent(fake: Source) -> None:
    await read(fake.connector(), ask("Orders", tag="ssc:app_1:env_1:req-1"))
    assert fake.seen.agents == ["ssc-datagw (ssc:app_1:env_1:req-1)"]


async def test_the_token_is_hidden_from_every_error_and_log_line(
    fake: Source, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _, rows = await read(fake.connector(), ask("Many", max_rows=1000))
    assert len(rows) == 250, "three pages carried the token and were accepted"
    nowhere = AirtableConnector(
        fake.target(), base_url="https://localhost:9", ca=fake.pki.ca, connect_seconds=2
    )
    failing = [
        (fake.connector(), ask(sql))
        for sql in ("Missing", "Busy", "Boom", "Text", "Odd", "Orders where bad(1)")
    ]
    failing += [
        (fake.connector(token="pat" + "x" * 30), ask("Orders")),
        (fake.connector(base_id="appZZZZZZZZZZZZZZ"), ask("Orders")),
        (fake.connector(ca=fake.pki.other_ca), ask("Orders")),
        (nowhere, ask("Orders")),
        (fake.connector(), ask("Slow", timeout_ms=200)),
    ]
    shown: list[str] = []
    for connector, query in failing:
        with pytest.raises((QueryFailedError, UpstreamUnavailableError, TimeoutError)) as failed:
            await read(connector, query)
        shown.append("".join(traceback.format_exception(failed.value)) + repr(failed.value))
        shown.append(repr(connector) + str(connector))
    for sql in ("insert Orders", "Orders; drop", "Orders\nx"):
        with pytest.raises(QueryRefusedError) as refused:
            await read(fake.connector(), ask(sql))
        shown.append("".join(traceback.format_exception(refused.value)))
    shown += [repr(fake.target()), str(fake.target()), caplog.text]
    assert f"Bearer {TOKEN}" in fake.seen.authorizations
    for text in shown:
        assert TOKEN not in text
        assert TOKEN[3:20] not in text
    assert "airtable read: status=200 bytes=" in caplog.text
    assert "pages=3" in caplog.text


async def test_the_default_url_and_headers() -> None:
    """Through an injected transport: the default base URL, the encoded path and query, and
    the three headers the connector sets."""
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json={"records": ORDERS})

    target = AirtableTarget(base_id=BASE_ID, token=TOKEN)  # pyright: ignore[reportArgumentType]
    connector = AirtableConnector(target, transport=httpx2.MockTransport(answer))
    await read(connector, ask("Order Items/2026 view All / open where {Paid} = 1", tag="a(b)"))
    (request,) = sent
    assert str(request.url) == (
        f"https://api.airtable.com/v0/{BASE_ID}/Order%20Items%2F2026"
        "?pageSize=100&view=All%20%2F%20open&filterByFormula=%7BPaid%7D%20%3D%201"
    )
    assert request.method == "GET"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["user-agent"] == "ssc-datagw (ab)"
    assert request.headers["accept"] == "application/json"
    assert "x-ssc-query" not in request.headers


async def test_params_are_refused_before_the_source() -> None:
    connector = AirtableConnector(AirtableTarget(base_id=BASE_ID, token=TOKEN))  # pyright: ignore[reportArgumentType]
    with pytest.raises(QueryRefusedError, match="^an Airtable read takes no parameters$"):
        await read(connector, ask("Orders", 1))


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("Orders", ("Orders", None, None)),
        ("tblA1b2C3d4E5f6G7", ("tblA1b2C3d4E5f6G7", None, None)),
        ("Order Items", ("Order Items", None, None)),
        ("Orders view Grid view", ("Orders", "Grid view", None)),
        ("Orders VIEW viwA1b2C3d4E5f6G7", ("Orders", "viwA1b2C3d4E5f6G7", None)),
        ("Orders where {Paid}", ("Orders", None, "{Paid}")),
        ("Orders WHERE {a} = 'x view y where z'", ("Orders", None, "{a} = 'x view y where z'")),
        ("Orders View Open Where NOT({Done})", ("Orders", "Open", "NOT({Done})")),
        ("Orders where {a};{b}", ("Orders", None, "{a};{b}")),
        ("Orders where  {a} ", ("Orders", None, " {a} ")),
        ("Überblick", ("Überblick", None, None)),
        ("Overview", ("Overview", None, None)),
        ("Updates", ("Updates", None, None)),
        ("Selected orders", ("Selected orders", None, None)),
        ("a" * 100, ("a" * 100, None, None)),
        ("Orders where " + "x" * 2000, ("Orders", None, "x" * 2000)),
    ],
)
def test_the_grammar_admits(sql: str, expected: tuple[str, str | None, str | None]) -> None:
    assert airtable_request(sql, None) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "",
        " Orders",
        "Orders ",
        "Orders  view Grid",
        "Orders view ",
        "Orders view  Grid",
        "Orders where ",
        "Orders view Grid ",
        "Orders\tview Grid",
        "Orders\nwhere 1",
        "Orders where 1\n",
        "Orders where \x00",
        "Ord\x7fers",
        "Orders; drop",
        "Orders;",
        "Orders view a;b",
        "insert Orders",
        "INSERT INTO Orders",
        "delete Orders",
        "update Orders",
        "Update",
        "SELECT * FROM Orders",
        "drop table Orders",
        "Orders view delete x",
        "merge Orders",
        "\ud800",
        "a" * 101,
        "Orders view " + "a" * 101,
        "Orders where " + "x" * 2001,
    ],
)
def test_the_grammar_refuses(sql: str) -> None:
    with pytest.raises(QueryRefusedError, match=r"^the query is not <table>\[ view <view>\]"):
        airtable_request(sql, None)


def test_the_connections_table_must_be_the_querys() -> None:
    assert airtable_request("Orders view Grid", "Orders") == ("Orders", "Grid", None)
    for sql in ("orders", "Many", "tblA1b2C3d4E5f6G7"):
        with pytest.raises(QueryRefusedError, match="^the query's table is not the connection"):
            airtable_request(sql, "Orders")


def test_fields_named_like_the_fixed_columns_get_a_suffix() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    records = [
        Record("rec1", at, {"b": 1, "id": "x"}),
        Record("rec2", at, {"created_time": "y", "a": None, "id_2": True}),
    ]
    columns, rows = table_of(records)
    assert [c.name for c in columns] == [
        "id",
        "created_time",
        "b",
        "id_2",
        "created_time_2",
        "a",
        "id_2_2",
    ]
    assert rows == [
        ["rec1", at, 1, "x", None, None, None],
        ["rec2", at, None, None, "y", None, True],
    ]
    assert columns[-2] == Column("a", "string", "null")


def test_no_records_are_the_two_fixed_columns_and_no_rows() -> None:
    assert table_of([]) == (
        [Column("id", "string", "string"), Column("created_time", "timestamp", "timestamp")],
        [],
    )


def test_records_page_reads_records_and_the_offset() -> None:
    body = b'{"records": [{"id": "r", "createdTime": "2026-01-01T00:00:00.000Z"}], "offset": "o"}'
    records, offset = records_page(body)
    assert records == [Record("r", datetime(2026, 1, 1, tzinfo=UTC), {})]
    assert offset == "o"
    assert records_page(b'{"records": []}') == ([], None)


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b'{"records": {}}',
        b'{"records": [], "offset": 3}',
        b'{"records": [], "offset": ""}',
        b'{"records": [1]}',
        b'{"records": [{"createdTime": "2026-01-01T00:00:00Z"}]}',
        b'{"records": [{"id": "", "createdTime": "2026-01-01T00:00:00Z"}]}',
        b'{"records": [{"id": "r"}]}',
        b'{"records": [{"id": "r", "createdTime": "yesterday"}]}',
        b'{"records": [{"id": "r", "createdTime": "2026-01-01T00:00:00"}]}',
        b'{"records": [{"id": "r", "createdTime": "2026-01-01T00:00:00Z", "fields": []}]}',
        b"not json",
    ],
)
def test_a_body_that_is_not_a_record_list_is_22p02_in_unit(body: bytes) -> None:
    with pytest.raises(QueryFailedError) as corrupt:
        records_page(body)
    assert corrupt.value.sqlstate == "22P02"


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (
            b'{"error": {"type": "INVALID_FILTER_BY_FORMULA", "message": "secret"}}',
            "INVALID_FILTER_BY_FORMULA",
        ),
        (b'{"error": "NOT_FOUND"}', "NOT_FOUND"),
        (b'{"error": {"type": "lower_case"}}', None),
        (b'{"error": {"type": "HAS SPACE"}}', None),
        (b'{"error": {"type": "' + b"A" * 65 + b'"}}', None),
        (b'{"error": {"message": "x"}}', None),
        (b"[1]", None),
        (b"<html>", None),
    ],
)
def test_error_type_is_an_upper_case_word_only(body: bytes, kind: str | None) -> None:
    assert error_type(body) == kind


@pytest.mark.parametrize(
    ("status", "error", "sqlstate"),
    [
        (401, QueryFailedError, "28000"),
        (403, QueryFailedError, "42501"),
        (404, QueryFailedError, "42P01"),
        (422, QueryFailedError, "42601"),
        (400, QueryFailedError, None),
        (302, QueryFailedError, None),
        (429, UpstreamUnavailableError, None),
        (500, UpstreamUnavailableError, None),
        (503, UpstreamUnavailableError, None),
    ],
)
def test_each_status_maps(status: int, error: type[Exception], sqlstate: str | None) -> None:
    failed = airtable_failure(status, "SOME_TYPE")
    assert isinstance(failed, error)
    assert str(failed) == f"the source answered {status} SOME_TYPE"
    assert getattr(failed, "sqlstate", None) == sqlstate
    assert str(airtable_failure(status, None)) == f"the source answered {status}"
    assert airtable_failure(200, None) is None


def _target(**update: Any) -> AirtableTarget:
    return AirtableTarget.model_validate({"base_id": BASE_ID, "token": TOKEN} | update)


@pytest.mark.parametrize(
    "update",
    [
        {"base_id": "app123"},
        {"base_id": "tblA1b2C3d4E5f6G7"},
        {"base_id": "appA1b2C3d4E5f6G7x"},
        {"base_id": "appA1b2C3d4E5f6G-"},
        {"table": ""},
        {"table": "a\nb"},
        {"table": "a" * 101},
        {"kind": "rest"},
        {"ca": "x"},
        {"base_url": "https://localhost:9"},
        {"transport": None},
    ],
)
def test_the_target_refuses_and_hides_the_token(update: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as refused:
        _target(**update)
    assert TOKEN not in str(refused.value)
    assert TOKEN not in repr(refused.value)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "pat" + "x" * 16,
        "pat" + "x" * 198,
        "key" + "x" * 30,
        "PAT" + "x" * 30,
        "pat" + "x" * 20 + " y",
        "pat" + "x" * 20 + "\r\nX-Evil: 1",
        "pat" + "x" * 20 + "é",
        "pat" + "x" * 20 + "\x7f",
    ],
)
def test_a_token_is_a_personal_access_token_and_its_error_hides_it(token: str) -> None:
    with pytest.raises(ValidationError) as refused:
        _target(token=token)
    assert "token" in str(refused.value)
    if token:
        assert token not in str(refused.value)
        assert token not in repr(refused.value)


def test_the_target_admits_the_control_planes_address() -> None:
    target = _target(table="Order Items/2026 ü;x")
    assert (target.kind, target.base_id, target.table) == (
        "airtable",
        BASE_ID,
        "Order Items/2026 ü;x",
    )
    assert _target().table is None
    assert _target(token="pat" + "x" * 17).token.get_secret_value() == "pat" + "x" * 17
    assert _target(token="pat" + "x" * 197).token.get_secret_value() == "pat" + "x" * 197
    assert TOKEN not in repr(target)
    assert TOKEN not in str(target)
