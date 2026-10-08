"""The REST connector against a TLS server in-process (GA-5 B2).

Each test that reads starts a FastAPI app under ``uvicorn`` on 127.0.0.1 with a server
certificate for ``localhost`` from the test PKI, so every read crosses a real TLS socket. The app
serves 50 orders behind a bearer token, the same records under a wrapper, one object, bare
scalars, and each answer the connector must map: slow, a redirect, a body past the cap, text, a
5xx, a 429 and a 403. The connector suite runs against it; the path rules, the target model and
the record rules are unit-tested without it."""

import asyncio
import logging
import socket
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from connector_suite import CHECKS, Subject, ask, conform, read
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from pki import Pki, make_pki
from pydantic import ValidationError

from ssc_datagw.connectors import (
    Column,
    QueryFailedError,
    QueryRefusedError,
    UpstreamUnavailableError,
)
from ssc_datagw.rest import (
    BODY_BYTES,
    TYPE_SAMPLE,
    RestConnector,
    RestTarget,
    columns_for,
    records_at,
    request_path,
    rows_for,
)

TOKEN = "fake-" + "rest-" + "token"
ORDERS = [
    {
        "id": i,
        "amount": f"{i * 1.25:.2f}",
        "placed": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
        "ok": i % 2 == 0,
        "meta": {"n": i},
        "note": f"note {i}",
        "ratio": i / 4,
    }
    for i in range(1, 51)
]
CHUNK = b'"' + b"x" * (2**20 - 3) + b'",'


@dataclass
class Seen:
    """What the app saw: the ``X-SSC-Query`` of each read of ``/orders``."""

    tags: list[str | None] = field(default_factory=list[str | None])


def app(seen: Seen) -> FastAPI:
    api = FastAPI()

    @api.get("/orders")
    async def orders(request: Request) -> Any:
        seen.tags.append(request.headers.get("x-ssc-query"))
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return JSONResponse({"error": "no"}, status_code=401)
        return ORDERS

    @api.get("/wrapped")
    async def wrapped() -> Any:
        return {"data": {"orders": ORDERS[:3]}}

    @api.get("/one")
    async def one() -> Any:
        return {"id": 7, "name": "seven"}

    @api.get("/scalars")
    async def scalars() -> Any:
        return [1, 2, 3]

    @api.get("/slow")
    async def slow() -> Any:
        await asyncio.sleep(30)
        return []

    @api.get("/redirect")
    async def redirect() -> Any:
        return RedirectResponse("/orders", status_code=302)

    @api.get("/big")
    async def big() -> Any:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"["
            for _ in range(BODY_BYTES // len(CHUNK) + 2):
                yield CHUNK
            yield b"0]"

        return StreamingResponse(chunks(), media_type="application/json")

    @api.get("/text")
    async def text() -> Any:
        return PlainTextResponse("not json")

    @api.get("/boom")
    async def boom() -> Any:
        return JSONResponse({"error": "boom"}, status_code=500)

    @api.get("/limit")
    async def limit() -> Any:
        return JSONResponse({"error": "slow down"}, status_code=429)

    @api.get("/secret")
    async def secret() -> Any:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    return api


@dataclass(frozen=True)
class Source:
    """The running app, its PKI and ways in."""

    pki: Pki
    base_url: str
    seen: Seen

    def target(self, **update: Any) -> RestTarget:
        return RestTarget.model_validate(
            {"base_url": self.base_url, "token": TOKEN, "ca": self.pki.ca} | update
        )

    def connector(self, **update: Any) -> RestConnector:
        return RestConnector(self.target(**update))


@pytest.fixture(scope="module")
def pki() -> Pki:
    return make_pki()


@pytest.fixture
async def source(pki: Pki, tmp_path: Path) -> AsyncIterator[Source]:
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    cert.write_text(pki.cert)
    key.write_text(pki.key)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    seen = Seen()
    config = uvicorn.Config(
        app(seen),
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
    """The REST connector as the connector suite sees it (GA-5)."""
    target = s.target()
    nowhere = target.model_copy(update={"base_url": "https://localhost:9"})
    return Subject(
        connector=RestConnector(target),
        unreachable=RestConnector(nowhere, connect_seconds=2),
        credential=TOKEN,
        secrets=(target, nowhere),
        read="/orders",
        read_columns={
            "id": "integer",
            "amount": "string",
            "placed": "string",
            "ok": "boolean",
            "meta": "json",
            "note": "string",
            "ratio": "float",
        },
        first_row=[1, "1.25", "2026-01-02", False, {"n": 1}, "note 1", 0.25],
        writes=("/orders?x=1 DELETE", "/orders; rm", "../etc/passwd"),
        bad="/secret",
        slow="/slow",
        params=None,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_the_rest_connector_conforms(
    source: Source, check: Callable[[Subject], Awaitable[None]]
) -> None:
    await conform(check, subject(source))


async def test_a_read_types_columns_by_json_type(source: Source) -> None:
    columns, rows = await read(source.connector(), ask("/orders", max_rows=1_000))
    assert columns == [
        Column("id", "integer", "number"),
        Column("amount", "string", "string"),
        Column("placed", "string", "string"),
        Column("ok", "boolean", "boolean"),
        Column("meta", "json", "object"),
        Column("note", "string", "string"),
        Column("ratio", "float", "number"),
    ]
    assert len(rows) == 50
    assert rows[49] == [50, "62.50", "2026-02-20", True, {"n": 50}, "note 50", 12.5]


async def test_a_refused_credential_is_28000_and_a_missing_path_42p01(source: Source) -> None:
    for connector in (source.connector(token=None), source.connector(token="fake-wrong")):
        with pytest.raises(QueryFailedError) as refused:
            await read(connector, ask("/orders"))
        assert refused.value.sqlstate == "28000"
        assert str(refused.value) == "the source refused the credential"
    with pytest.raises(QueryFailedError) as missing:
        await read(source.connector(), ask("/nowhere"))
    assert missing.value.sqlstate == "42P01"
    assert str(missing.value) == "no such path"


async def test_rate_limiting_and_a_5xx_are_unavailable(source: Source) -> None:
    with pytest.raises(UpstreamUnavailableError, match="^the source is rate limiting$"):
        await read(source.connector(), ask("/limit"))
    with pytest.raises(UpstreamUnavailableError, match="^the source answered 500$"):
        await read(source.connector(), ask("/boom"))


async def test_a_redirect_is_not_followed(source: Source) -> None:
    with pytest.raises(QueryFailedError) as redirected:
        await read(source.connector(), ask("/redirect"))
    assert str(redirected.value) == "the source answered 302"
    assert redirected.value.sqlstate is None
    assert source.seen.tags == [], "the redirect's target was not asked for"


async def test_a_body_past_32_mib_is_refused(source: Source) -> None:
    with pytest.raises(QueryFailedError) as big:
        await read(source.connector(), ask("/big", timeout_ms=30_000))
    assert str(big.value) == "the body is larger than 32 MiB"
    assert big.value.sqlstate is None


async def test_a_body_that_is_not_json_is_22p02(source: Source) -> None:
    with pytest.raises(QueryFailedError) as text:
        await read(source.connector(), ask("/text"))
    assert str(text.value) == "the body is not JSON"
    assert text.value.sqlstate == "22P02"


async def test_the_items_path_finds_the_records(source: Source) -> None:
    columns, rows = await read(source.connector(items="data.orders"), ask("/wrapped"))
    assert [c.name for c in columns][:2] == ["id", "amount"]
    assert [r[0] for r in rows] == [1, 2, 3]
    for items in ("data.nope", "data.orders.id", "nope"):
        with pytest.raises(QueryFailedError) as missing:
            await read(source.connector(items=items), ask("/wrapped"))
        assert missing.value.sqlstate == "42P01", items


async def test_a_single_object_is_one_row(source: Source) -> None:
    columns, rows = await read(source.connector(), ask("/one"))
    assert columns == [Column("id", "integer", "number"), Column("name", "string", "string")]
    assert rows == [[7, "seven"]]


async def test_scalars_become_a_value_column(source: Source) -> None:
    columns, rows = await read(source.connector(), ask("/scalars"))
    assert columns == [Column("value", "integer", "number")]
    assert rows == [[1], [2], [3]]


async def test_a_read_yields_max_rows_plus_one(source: Source) -> None:
    for cap, expected in ((0, 1), (5, 6), (49, 50), (50, 50), (5_000, 50)):
        _, rows = await read(source.connector(), ask("/orders", max_rows=cap))
        assert len(rows) == expected, cap


async def test_the_tag_arrives_as_a_header(source: Source) -> None:
    await read(source.connector(), ask("/orders", tag="ssc:app_1:env_1:req-1"))
    assert source.seen.tags == ["ssc:app_1:env_1:req-1"]


async def test_another_ca_or_the_system_store_is_refused(source: Source) -> None:
    for connector in (
        source.connector(ca=source.pki.other_ca),
        source.connector(ca=None),
    ):
        with pytest.raises(UpstreamUnavailableError, match="^cannot connect: "):
            await read(connector, ask("/orders"))
    assert source.seen.tags == [], "no request crossed a refused handshake"


async def test_the_token_is_absent_from_errors(
    source: Source, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _, rows = await read(source.connector(), ask("/orders"))
    assert rows, "the token was sent and accepted"
    nowhere = RestConnector(source.target(base_url="https://localhost:9"), connect_seconds=2)
    failing = [
        (source.connector(), ask(path))
        for path in ("/secret", "/nowhere", "/boom", "/limit", "/redirect", "/text")
    ]
    failing += [
        (source.connector(ca=source.pki.other_ca), ask("/orders")),
        (nowhere, ask("/orders")),
        (source.connector(), ask("/slow", timeout_ms=200)),
    ]
    for connector, query in failing:
        with pytest.raises((QueryFailedError, UpstreamUnavailableError, TimeoutError)) as failed:
            await read(connector, query)
        shown = "".join(traceback.format_exception(failed.value)) + repr(failed.value)
        assert TOKEN not in shown, query.sql
    assert TOKEN not in caplog.text
    assert "rest read: status=200 bytes=" in caplog.text


async def test_the_scheme_and_header_are_the_targets() -> None:
    """Through an injected transport: a bare token in a custom header, the fixed headers."""
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json=[{"a": 1}])

    target = RestTarget(
        base_url="https://api.example.com/v1/",
        token=TOKEN,  # pyright: ignore[reportArgumentType]
        header="X-Api-Key",
        scheme="",
    )
    connector = RestConnector(target, transport=httpx2.MockTransport(answer))
    _, rows = await read(connector, ask("/orders?limit=5", tag="ssc:t"))
    assert rows == [[1]]
    (request,) = sent
    assert str(request.url) == "https://api.example.com/v1/orders?limit=5"
    assert request.method == "GET"
    assert request.headers["x-api-key"] == TOKEN
    assert "authorization" not in request.headers
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == "ssc-datagw"
    assert request.headers["x-ssc-query"] == "ssc:t"


def test_an_http_base_url_is_refused_by_the_model() -> None:
    for base_url in (
        "http://api.example.com",
        "ftp://api.example.com",
        "https://user:pw@api.example.com",
        "https://api.example.com/v1?key=x",
        "https://api.example.com/v1#x",
        "https://api.example.com/" + "a" * 2000,
        "api.example.com",
    ):
        with pytest.raises(ValidationError):
            RestTarget(base_url=base_url)
    for base_url in ("https://api.example.com", "https://localhost:8443/v1/"):
        assert RestTarget(base_url=base_url).base_url == base_url


@pytest.mark.parametrize("header", ["Host", "accept", "User-Agent", "X-SSC-Query", "COOKIE"])
def test_the_credential_header_cannot_be_one_the_connector_sets(header: str) -> None:
    with pytest.raises(ValidationError):
        RestTarget(base_url="https://api.example.com", header=header)


@pytest.mark.parametrize(
    "token", ["", "fake token", "fake-token\r\nX-Evil: 1", "fake-é", "a" * 4097]
)
def test_a_token_is_1_to_4096_visible_ascii_characters_and_its_error_hides_it(token: str) -> None:
    with pytest.raises(ValidationError) as refused:
        RestTarget(base_url="https://api.example.com", token=token)  # pyright: ignore[reportArgumentType]
    if token:
        assert token not in str(refused.value)
        assert token not in repr(refused.value)


def test_a_target_hides_its_token() -> None:
    target = RestTarget(base_url="https://api.example.com", token=TOKEN)  # pyright: ignore[reportArgumentType]
    assert TOKEN not in repr(target)
    assert TOKEN not in str(target)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/orders",
        "/v1/orders?limit=5&next=https://example.com/x",
        "/a/./b",
        "/a?b=..",
        "/a/%2e/b",
        "/a..b/c",
    ],
)
def test_request_path_admits_a_get_path(path: str) -> None:
    assert request_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "orders",
        "//evil.example.com/x",
        "https://evil.example.com/x",
        "../etc/passwd",
        "/../x",
        "/a/../b",
        "/a/..",
        "/a/%2e%2e/b",
        "/a/%2E%2E",
        "/a%2f..%2fb",
        "/a#frag",
        "/a\\b",
        "/a b",
        "/a\tb",
        "/a\nb",
        "/é",
        "/orders?x=1 DELETE",
        "/orders; rm",
        "/" + "a" * 2000,
    ],
)
def test_request_path_refuses_the_rest(path: str) -> None:
    with pytest.raises(QueryRefusedError, match="^the request is not a GET path$"):
        request_path(path)


async def test_params_are_refused_before_the_source() -> None:
    connector = RestConnector(RestTarget(base_url="https://localhost:9"))
    with pytest.raises(QueryRefusedError, match="^a REST read takes no parameters$"):
        await read(connector, ask("/orders", 1))


def test_columns_take_their_type_from_the_first_non_null_value() -> None:
    records: list[Any] = [
        {"b": None, "i": None, "f": 1.5, "s": "x", "a": [1], "o": {"k": 1}, "n": None},
        {"b": True, "i": 3, "f": 2, "s": None, "a": None, "o": None, "n": None},
    ]
    assert columns_for(records) == [
        Column("b", "boolean", "boolean"),
        Column("i", "integer", "number"),
        Column("f", "float", "number"),
        Column("s", "string", "string"),
        Column("a", "json", "array"),
        Column("o", "json", "object"),
        Column("n", "string", "null"),
    ]


def test_only_the_first_100_records_decide_a_type() -> None:
    records: list[Any] = [{"x": None}] * TYPE_SAMPLE + [{"x": 5}]
    assert columns_for(records) == [Column("x", "string", "null")]
    assert columns_for(records[1:]) == [Column("x", "integer", "number")]


def test_records_keep_missing_keys_as_null_drop_extra_keys_and_keep_odd_values() -> None:
    records: list[Any] = [{"a": 1, "b": 2}, {"b": "two", "c": 3}, {"a": "one"}]
    columns = columns_for(records)
    assert [c.name for c in columns] == ["a", "b"]
    assert rows_for(records, columns, 10) == [[1, 2], [None, "two"], ["one", None]]
    assert rows_for(records, columns, 2) == [[1, 2], [None, "two"]]


def test_a_non_object_among_objects_is_a_row_of_nulls() -> None:
    records: list[Any] = [{"a": 1, "b": 2}, 7, [1, 2], None]
    columns = columns_for(records)
    assert rows_for(records, columns, 10) == [[1, 2], [None, None], [None, None], [None, None]]


def test_an_object_among_scalars_goes_into_value() -> None:
    records: list[Any] = [None, "x", {"a": 1}, [2]]
    columns = columns_for(records)
    assert columns == [Column("value", "string", "string")]
    assert rows_for(records, columns, 10) == [[None], ["x"], [{"a": 1}], [[2]]]


def test_zero_records_are_zero_columns_and_no_rows() -> None:
    assert columns_for([]) == []
    assert rows_for([], [], 10) == []
    assert records_at({"data": []}, "data") == []


def test_records_at_walks_object_keys() -> None:
    body: Any = {"data": {"orders": [{"id": 1}], "one": {"id": 2}, "n": 3}}
    assert records_at(body, "data.orders") == [{"id": 1}]
    assert records_at(body, "data.one") == [{"id": 2}]
    assert records_at([1, 2], None) == [1, 2]
    for items in ("data.missing", "data.orders.id", "data.n.x", "nope"):
        with pytest.raises(QueryFailedError) as missing:
            records_at(body, items)
        assert missing.value.sqlstate == "42P01", items


@pytest.mark.parametrize("body", [3, "text", None, True])
def test_items_that_are_not_an_array_or_object_are_22p02(body: Any) -> None:
    with pytest.raises(QueryFailedError) as odd:
        records_at({"data": body}, "data")
    assert odd.value.sqlstate == "22P02"
    with pytest.raises(QueryFailedError):
        records_at(body, None)
