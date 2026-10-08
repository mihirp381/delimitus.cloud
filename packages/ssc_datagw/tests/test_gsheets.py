"""The Google Sheets connector against a fake Sheets API over TLS in-process (GA-5 B3).

Each test that reads starts a FastAPI app under ``uvicorn`` on 127.0.0.1 with a server
certificate for ``localhost`` from the test PKI, so every read crosses a real TLS socket. The app
answers ``GET /v4/spreadsheets/{id}/values/{range}`` as Google does: it verifies the bearer JWT
against the test service account's public key (RS256, ``kid``, ``iss`` = ``sub`` = the email,
``aud`` exact, at most an hour) and answers Google-shaped errors otherwise; an unknown tab is
400, a spreadsheet not shared with the account 403, an unknown one 404. Its tabs: 50 orders,
one whose read sleeps 20 s, one with an empty, a duplicate and a ragged header, an empty one and
a header alone. For a description it answers the spreadsheet's tab titles and
``values:batchGet``, the latter without the slow tab's sleep. The service account's key is made
here, never read from disk. The connector suite runs against it; the range grammar, the target
model and the table rules are unit-tested without it."""

import asyncio
import json
import logging
import re
import socket
import time
import traceback
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from functools import reduce
from pathlib import Path
from typing import Any

import httpx2
import jwt
import pytest
import uvicorn
from connector_suite import CHECKS, Subject, ask, conform, read
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pki import Pki, make_pki
from pydantic import ValidationError

from ssc_datagw.connectors import (
    Column,
    QueryFailedError,
    QueryRefusedError,
    UpstreamUnavailableError,
)
from ssc_datagw.gsheets import (
    AUDIENCE,
    DESCRIBE_CHUNK,
    GsheetsConnector,
    GsheetsTarget,
    a1_range,
    header_names,
    table,
    values_in,
)
from ssc_datagw.rest import TYPE_SAMPLE
from ssc_datagw.tls import tls_context

SPREADSHEET = "1Fake" + "x" * 39
NOT_SHARED = "2Fake" + "y" * 39
EMAIL = "reader@fake-project.iam.gserviceaccount.com"
ORDERS: list[list[Any]] = [["id", "amount", "ok", "note", "extra"]] + [
    [i, i + 0.25, i % 2 == 0, f"note {i}", "x"] for i in range(1, 51)
]
ODD: list[list[Any]] = [
    ["id", "", "id", "name", 2024, True],
    [1, "x", 2],
    [2, "", "", "n", 5, False, "extra"],
    [],
]
TABS: dict[str, list[list[Any]]] = {
    "Orders": ORDERS,
    "Slow": [["a"], [1]],
    "Odd": ODD,
    "Empty": [],
    "Header": [["a", "b"]],
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
                "project_id": "fake-project",
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
class Seen:
    """What the app saw: every bearer token, the header and claims of each it accepted, and the
    range of each accepted read."""

    bearers: list[str] = field(default_factory=list[str])
    headers: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    claims: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    ranges: list[str] = field(default_factory=list[str])
    titles: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    batches: list[list[str]] = field(default_factory=list[list[str]])
    slow_titles: bool = False


def _error(code: int, status: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "status": status}}, status_code=code
    )


def _column(letters: str) -> int:
    return reduce(lambda n, c: n * 26 + ord(c) - 64, letters, 0)


def _cut(values: list[list[Any]], cells: str | None) -> list[list[Any]]:
    """The part of a tab an A1 range names, as Google answers it: trailing empties dropped."""
    if cells is None:
        return values
    found = re.fullmatch(r"([A-Z]*)(\d*)(?::([A-Z]*)(\d*))?", cells.upper())
    assert found is not None, cells
    c1, r1, c2, r2 = found.groups()
    pair = found.group(3) is not None
    top = int(r1) - 1 if r1 else 0
    bottom = int(r2) if r2 else (len(values) if pair or not r1 else int(r1))
    left = _column(c1) - 1 if c1 else 0
    right = _column(c2) if c2 else (10**6 if pair or not c1 else _column(c1))
    rows = [row[left:right] for row in values[top:bottom]]
    while rows and not rows[-1]:
        rows.pop()
    return rows


def _split(rng: str) -> tuple[str | None, str | None]:
    quoted = re.fullmatch(r"'((?:[^']|'')+)'(?:!(.+))?", rng)
    if quoted:
        return quoted[1].replace("''", "'"), quoted[2]
    if "!" in rng:
        tab, cells = rng.split("!", 1)
        return tab, cells
    return (rng, None) if rng in TABS else (None, rng)


def app(account: Account, seen: Seen) -> FastAPI:
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

    def refused(spreadsheet: str, request: Request) -> JSONResponse | None:
        if not verified(request):
            return _error(401, "UNAUTHENTICATED", "Request had invalid authentication credentials.")
        if spreadsheet == NOT_SHARED:
            return _error(403, "PERMISSION_DENIED", "The caller does not have permission")
        if spreadsheet != SPREADSHEET:
            return _error(404, "NOT_FOUND", "Requested entity was not found.")
        return None

    @api.get("/v4/spreadsheets/{spreadsheet}")
    async def spreadsheet(spreadsheet: str, request: Request) -> Any:
        if (error := refused(spreadsheet, request)) is not None:
            return error
        seen.titles.append(dict(request.query_params))
        if seen.slow_titles:
            await asyncio.sleep(20)
        return {"sheets": [{"properties": {"title": title}} for title in TABS]}

    @api.get("/v4/spreadsheets/{spreadsheet}/values:batchGet")
    async def batch(spreadsheet: str, request: Request) -> Any:
        if (error := refused(spreadsheet, request)) is not None:
            return error
        ranges = request.query_params.getlist("ranges")
        seen.batches.append(ranges)
        found: list[dict[str, Any]] = []
        for rng in ranges:
            tab, cells = _split(rng)
            if tab not in TABS:
                return _error(400, "INVALID_ARGUMENT", f"Unable to parse range: {rng}")
            rows = _cut(TABS[tab], cells)
            found.append({"range": rng} | ({"values": rows} if rows else {}))
        return {"spreadsheetId": spreadsheet, "valueRanges": found}

    @api.get("/v4/spreadsheets/{spreadsheet}/values/{rng:path}")
    async def values(spreadsheet: str, rng: str, request: Request) -> Any:
        if not verified(request):
            return _error(401, "UNAUTHENTICATED", "Request had invalid authentication credentials.")
        if spreadsheet == NOT_SHARED:
            return _error(403, "PERMISSION_DENIED", "The caller does not have permission")
        if spreadsheet != SPREADSHEET:
            return _error(404, "NOT_FOUND", "Requested entity was not found.")
        tab, cells = _split(rng)
        tab = tab or next(iter(TABS))
        if tab not in TABS:
            return _error(400, "INVALID_ARGUMENT", f"Unable to parse range: {rng}")
        seen.ranges.append(rng)
        if tab == "Slow":
            await asyncio.sleep(20)
        rows = _cut(TABS[tab], cells)
        answer: dict[str, Any] = {"range": rng, "majorDimension": "ROWS"}
        return answer | ({"values": rows} if rows else {})

    return api


@dataclass(frozen=True)
class Source:
    """The running app, its PKI, its service account and ways in."""

    pki: Pki
    base_url: str
    account: Account
    seen: Seen

    def target(self, **update: Any) -> GsheetsTarget:
        return GsheetsTarget.model_validate(
            {"spreadsheet_id": SPREADSHEET, "service_account": self.account.json()} | update
        )

    def connector(self, base_url: str | None = None, **update: Any) -> GsheetsConnector:
        return GsheetsConnector(
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
    """The Google Sheets connector as the connector suite sees it (GA-5)."""
    target = s.target()
    return Subject(
        connector=s.connector(),
        unreachable=s.connector(base_url="https://localhost:9"),
        credential="".join(s.account.body)[100:140],
        secrets=(target,),
        read="Orders!A1:D",
        read_columns={"id": "integer", "amount": "float", "ok": "boolean", "note": "string"},
        first_row=[1, 1.25, False, "note 1"],
        writes=("DELETE FROM orders", "Orders!A1:D; x", '=IMPORTRANGE("x")'),
        bad="Nope!A1:B2",
        slow="Slow!A1:B",
        params=None,
        table="Orders",
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_the_gsheets_connector_conforms(
    source: Source, check: Callable[[Subject], Awaitable[None]]
) -> None:
    await conform(check, subject(source))


async def test_a_description_names_each_tab_typed_as_a_read_types_it(source: Source) -> None:
    tables = await source.connector().describe(schemas=["x"], timeout_ms=5_000)
    assert [t.name for t in tables] == list(TABS)
    for tab in ("Orders", "Odd", "Empty", "Header"):
        columns, _ = await read(source.connector(), ask(tab))
        assert next(t for t in tables if t.name == tab).columns == columns, tab
    assert source.seen.titles == [{"fields": "sheets.properties.title"}]
    assert source.seen.batches == [[f"'{tab}'!1:{1 + TYPE_SAMPLE}" for tab in TABS]]
    assert len(source.seen.claims) >= 2, "the fake verified each JWT"


async def test_a_description_of_a_connection_with_a_sheet_names_that_sheet_alone(
    source: Source,
) -> None:
    (table,) = await source.connector(sheet="Odd").describe(schemas=None, timeout_ms=5_000)
    assert (table.name, [c.name for c in table.columns][:3]) == ("Odd", ["id", "col2", "id_2"])
    assert source.seen.titles == []
    assert source.seen.batches == [["'Odd'!1:101"]]


async def test_a_description_stops_at_500_tabs_of_500_columns_in_chunks(
    source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        f"{__name__}.TABS",
        {"Wide": [[f"c{i}" for i in range(1, 502)]]} | {f"T{i:03}": [["a"]] for i in range(1, 503)},
    )
    tables = await source.connector().describe(schemas=None, timeout_ms=10_000)
    assert len(tables) == 500
    assert (tables[0].name, len(tables[0].columns)) == ("Wide", 500)
    assert tables[-1].name == "T499"
    assert [len(b) for b in source.seen.batches] == [DESCRIBE_CHUNK] * 10


async def test_a_description_past_its_timeout_times_out(source: Source) -> None:
    source.seen.slow_titles = True
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await source.connector().describe(schemas=None, timeout_ms=500)
    assert time.monotonic() - started < 5


async def test_a_description_of_a_spreadsheet_not_shared_is_42501_without_the_credential(
    source: Source,
) -> None:
    with pytest.raises(QueryFailedError) as denied:
        await source.connector(spreadsheet_id=NOT_SHARED).describe(schemas=None, timeout_ms=5_000)
    assert denied.value.sqlstate == "42501"
    shown = "".join(traceback.format_exception(denied.value))
    assert all(secret not in shown for secret in source.secrets())


async def test_a_read_types_columns_by_cell_type(source: Source) -> None:
    columns, rows = await read(source.connector(), ask("Orders", max_rows=1_000))
    assert columns == [
        Column("id", "integer", "number"),
        Column("amount", "float", "number"),
        Column("ok", "boolean", "boolean"),
        Column("note", "string", "string"),
        Column("extra", "string", "string"),
    ]
    assert len(rows) == 50
    assert rows[49] == [50, 50.25, True, "note 50", "x"]
    assert source.seen.ranges == ["'Orders'"]


async def test_the_jwt_is_self_signed_for_the_sheets_api(source: Source) -> None:
    before = int(time.time())
    for _ in range(2):
        await read(source.connector(), ask("Orders!A1:B2"))
    assert len(source.seen.claims) == 2, "each read sent a JWT the fake verified"
    header, claims = source.seen.headers[-1], source.seen.claims[-1]
    assert header["alg"] == "RS256"
    assert header["kid"] == source.account.kid
    assert claims["iss"] == claims["sub"] == EMAIL
    assert claims["aud"] == "https://sheets.googleapis.com/"
    assert claims["exp"] - claims["iat"] == 3600
    assert before - 5 <= claims["iat"] <= int(time.time()) + 5
    assert set(claims) == {"iss", "sub", "aud", "iat", "exp"}


async def test_another_key_or_email_is_28000(source: Source) -> None:
    other_key = make_account(kid=source.account.kid)
    other_email = make_account(email="other@fake-project.iam.gserviceaccount.com")
    for account in (other_key, other_email):
        connector = source.connector(service_account=account.json())
        with pytest.raises(QueryFailedError) as refused:
            await read(connector, ask("Orders!A1:B2"))
        assert refused.value.sqlstate == "28000"
        assert str(refused.value) == "the source refused the credential"
    assert source.seen.ranges == []


async def test_a_spreadsheet_not_shared_is_42501_and_an_unknown_one_42p01(source: Source) -> None:
    with pytest.raises(QueryFailedError) as denied:
        await read(source.connector(spreadsheet_id=NOT_SHARED), ask("Orders!A1:B2"))
    assert denied.value.sqlstate == "42501"
    assert str(denied.value) == "the service account may not read this spreadsheet"
    with pytest.raises(QueryFailedError) as unknown:
        await read(source.connector(spreadsheet_id="3Fake" + "z" * 39), ask("Orders!A1:B2"))
    assert unknown.value.sqlstate == "42P01"


async def test_a_missing_tab_is_42p01(source: Source) -> None:
    for sql in ("Nope!A1:B2", "Nope", "'No such tab'!A:B"):
        with pytest.raises(QueryFailedError) as missing:
            await read(source.connector(), ask(sql))
        assert missing.value.sqlstate == "42P01", sql
        assert str(missing.value) == "no such sheet or range"


async def test_the_connections_sheet_prefixes_a_bare_range_and_refuses_another(
    source: Source,
) -> None:
    connector = source.connector(sheet="Orders")
    _, rows = await read(connector, ask("A2:B3"))
    assert rows == [[2, 2.25]]
    await read(connector, ask("orders!A1:B2"))
    await read(connector, ask("ORDERS"))
    assert source.seen.ranges == ["'Orders'!A2:B3", "'Orders'!A1:B2", "'Orders'"]
    for sql in ("Odd!A1:B2", "Odd", "'Orders 2'!A1"):
        with pytest.raises(QueryRefusedError, match="^the range names another sheet"):
            await read(connector, ask(sql))
    assert len(source.seen.ranges) == 3, "nothing refused was sent"


async def test_an_unprefixed_range_reads_the_first_tab(source: Source) -> None:
    columns, rows = await read(source.connector(), ask("A1:B3"))
    assert [c.name for c in columns] == ["id", "amount"]
    assert rows == [[1, 1.25], [2, 2.25]]
    assert source.seen.ranges == ["A1:B3"]


async def test_odd_headers_are_named_and_ragged_rows_fit(source: Source) -> None:
    columns, rows = await read(source.connector(), ask("Odd"))
    assert columns == [
        Column("id", "integer", "number"),
        Column("col2", "string", "string"),
        Column("id_2", "integer", "number"),
        Column("name", "string", "string"),
        Column("2024", "integer", "number"),
        Column("true", "boolean", "boolean"),
    ]
    assert rows == [
        [1, "x", 2, None, None, None],
        [2, None, None, "n", 5, False],
        [None] * 6,
    ]


async def test_an_empty_tab_and_a_header_alone(source: Source) -> None:
    assert await read(source.connector(), ask("Empty")) == ([], [])
    columns, rows = await read(source.connector(), ask("Header"))
    assert columns == [Column("a", "string", "string"), Column("b", "string", "string")]
    assert rows == []


async def test_a_read_yields_max_rows_plus_one(source: Source) -> None:
    for cap, expected in ((0, 1), (5, 6), (49, 50), (50, 50), (5_000, 50)):
        _, rows = await read(source.connector(), ask("Orders!A:D", max_rows=cap))
        assert len(rows) == expected, cap


async def test_another_ca_is_refused(source: Source) -> None:
    connector = GsheetsConnector(
        source.target(),
        base_url=source.base_url,
        transport=httpx2.AsyncHTTPTransport(verify=tls_context(source.pki.other_ca)),
    )
    with pytest.raises(UpstreamUnavailableError, match="^cannot connect: "):
        await read(connector, ask("Orders"))
    assert source.seen.bearers == [], "no request crossed a refused handshake"


async def test_the_credential_is_absent_from_errors_and_logs(
    source: Source, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _, rows = await read(source.connector(), ask("Orders"))
    assert rows, "the JWT was sent and accepted"
    stranger = make_account(kid=source.account.kid)
    failing = [
        (source.connector(), ask(sql))
        for sql in ("Nope!A1", "DELETE FROM orders", "Orders!A1; x", "'Odd'!A1:B2;")
    ]
    failing += [
        (source.connector(sheet="Orders"), ask("Odd!A1")),
        (source.connector(service_account=stranger.json()), ask("Orders")),
        (source.connector(spreadsheet_id=NOT_SHARED), ask("Orders")),
        (source.connector(spreadsheet_id="3Fake" + "z" * 39), ask("Orders")),
        (source.connector(base_url="https://localhost:9"), ask("Orders")),
        (source.connector(), ask("Slow", timeout_ms=200)),
        (source.connector(), ask("Orders", 1)),
    ]
    for connector, query in failing:
        with pytest.raises(
            (QueryFailedError, QueryRefusedError, UpstreamUnavailableError, TimeoutError)
        ) as failed:
            await read(connector, query)
        shown = "".join(traceback.format_exception(failed.value)) + repr(failed.value)
        for secret in source.secrets():
            assert secret not in shown, query.sql
    assert len(source.seen.bearers) >= 4
    for secret in source.secrets():
        assert secret not in caplog.text
    assert "gsheets read: status=200 bytes=" in caplog.text


def test_a_target_hides_its_service_account(account: Account) -> None:
    target = GsheetsTarget(spreadsheet_id=SPREADSHEET, service_account=account.json())  # pyright: ignore[reportArgumentType]
    connector = GsheetsConnector(target)
    for shown in (repr(target), str(target), repr(connector), str(connector)):
        for secret in (account.json(), account.email, account.kid, *account.body):
            assert secret not in shown


def _ec_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_a_service_account_that_is_not_a_key_is_refused_without_quoting_it(
    account: Account,
) -> None:
    full = json.loads(account.json())
    bad = [
        "not json " + account.email,
        json.dumps([account.email]),
        json.dumps({k: v for k, v in full.items() if k != "private_key"}),
        json.dumps({k: v for k, v in full.items() if k != "client_email"}),
        json.dumps({k: v for k, v in full.items() if k != "private_key_id"}),
        account.json(client_email=""),
        account.json(private_key_id=7),
        account.json(private_key="-----BEGIN PRIVATE KEY-----\nnot a key\n"),
        account.json(private_key=account.pem.replace(account.body[3], account.body[3][::-1])),
        account.json(private_key=_ec_pem()),
        account.json()[:-40],
    ]
    for service_account in bad:
        with pytest.raises(ValidationError) as refused:
            GsheetsTarget(spreadsheet_id=SPREADSHEET, service_account=service_account)  # pyright: ignore[reportArgumentType]
        shown = (
            str(refused.value)
            + repr(refused.value)
            + "".join(traceback.format_exception(refused.value))
        )
        assert "the service account is not a JSON key" in shown
        for secret in (service_account, account.email, account.kid, *account.body):
            assert secret not in shown


@pytest.mark.parametrize(
    "update",
    [
        {"spreadsheet_id": "x" * 19},
        {"spreadsheet_id": "x" * 129},
        {"spreadsheet_id": SPREADSHEET[:-1] + "/"},
        {"spreadsheet_id": SPREADSHEET[:-1] + "."},
        {"sheet": ""},
        {"sheet": "a" * 101},
        {"sheet": "Q1!A1"},
        {"sheet": "It's"},
        {"sheet": "A:B"},
        {"sheet": "tab\n"},
        {"kind": "rest"},
        {"extra": 1},
    ],
)
def test_the_target_model_refuses_a_bad_address(account: Account, update: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GsheetsTarget.model_validate(
            {"spreadsheet_id": SPREADSHEET, "service_account": account.json()} | update
        )


def test_the_target_model_takes_the_control_planes_address(account: Account) -> None:
    for spreadsheet_id, sheet in (("a" * 20, None), ("A-b_" * 32, "Q1 sales"), (SPREADSHEET, "é")):
        target = GsheetsTarget.model_validate(
            {"spreadsheet_id": spreadsheet_id, "sheet": sheet, "service_account": account.json()}
        )
        assert (target.kind, target.spreadsheet_id, target.sheet) == (
            "gsheets",
            spreadsheet_id,
            sheet,
        )


@pytest.mark.parametrize(
    ("sql", "sheet", "sent"),
    [
        ("A1", None, "A1"),
        ("a1:d100", None, "a1:d100"),
        ("A1:D100", None, "A1:D100"),
        ("A2:D", None, "A2:D"),
        ("A:D", None, "A:D"),
        ("1:100", None, "1:100"),
        ("ZZZ9999999", None, "ZZZ9999999"),
        ("Orders!A1:D", None, "'Orders'!A1:D"),
        ("Sheet_1!B2", None, "'Sheet_1'!B2"),
        ("'Q1 sales'!B2:C", None, "'Q1 sales'!B2:C"),
        ("'It''s'!A1", None, "'It''s'!A1"),
        ("'Données'!A:A", None, "'Données'!A:A"),
        ("'a!b:c'!A1", None, "'a!b:c'!A1"),
        ("Orders", None, "'Orders'"),
        ("'Q1 sales'", None, "'Q1 sales'"),
        ("A0", None, "'A0'"),
        ("AAAA1", None, "'AAAA1'"),
        ("A1:D", "Orders", "'Orders'!A1:D"),
        ("orders!A1", "Orders", "'Orders'!A1"),
        ("'ORDERS'", "Orders", "'Orders'"),
        ("1:2", "Q1 sales", "'Q1 sales'!1:2"),
    ],
)
def test_a1_range_admits_a_range_or_a_tab(sql: str, sheet: str | None, sent: str) -> None:
    assert a1_range(sql, sheet) == sent


@pytest.mark.parametrize(
    "sql",
    [
        "",
        " A1",
        "A1 ",
        "A1:",
        ":A1",
        "A1:B2:C3",
        "1:D",
        "A:1",
        "A12345678:B2",
        "Orders!",
        "!A1",
        "'Orders!A1",
        "'Or'ders'!A1",
        "''!A1",
        "'''!A1",
        "Orders!Sales!A1",
        "Q1 sales!A1",
        "Orders!A1\n",
        "'a\tb'!A1",
        "'" + "a" * 101 + "'!A1",
        "a" * 101,
        "'" + "a" * 299 + "'",
        "DELETE FROM orders",
        "Orders!A1:D; x",
        '=IMPORTRANGE("x")',
        "Orders!A1:D,Odd!A1",
        "Orders!R1C1",
    ],
)
def test_a1_range_refuses_the_rest(sql: str) -> None:
    for sheet in (None, "Orders"):
        with pytest.raises(QueryRefusedError, match="^the query is not an A1 range$"):
            a1_range(sql, sheet)


async def test_params_are_refused_before_the_source(account: Account) -> None:
    sent: list[httpx2.Request] = []
    target = GsheetsTarget(spreadsheet_id=SPREADSHEET, service_account=account.json())  # pyright: ignore[reportArgumentType]
    connector = GsheetsConnector(target, transport=httpx2.MockTransport(sent.append))  # pyright: ignore[reportArgumentType]
    with pytest.raises(QueryRefusedError, match="^a Google Sheets read takes no parameters$"):
        await read(connector, ask("Orders", 1))
    assert sent == []


def _mock(
    account: Account, answer: Callable[[httpx2.Request], httpx2.Response]
) -> GsheetsConnector:
    target = GsheetsTarget(spreadsheet_id=SPREADSHEET, service_account=account.json())  # pyright: ignore[reportArgumentType]
    return GsheetsConnector(target, transport=httpx2.MockTransport(answer))


async def test_the_request_is_one_get_of_the_values_unformatted(account: Account) -> None:
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json={"values": [["a"], [1]]})

    _, rows = await read(_mock(account, answer), ask("'Q1 sales'!A1:B", tag="ssc:t"))
    assert rows == [[1]]
    (request,) = sent
    assert request.method == "GET"
    assert request.url.host == "sheets.googleapis.com"
    assert request.url.raw_path.decode() == (
        f"/v4/spreadsheets/{SPREADSHEET}/values/%27Q1%20sales%27%21A1%3AB"
        "?valueRenderOption=UNFORMATTED_VALUE&dateTimeRenderOption=SERIAL_NUMBER"
        "&majorDimension=ROWS"
    )
    assert request.headers["authorization"].startswith("Bearer ey")
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == "ssc-datagw"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'"values"',
        b'{"values": {"a": 1}}',
        b'{"values": [1, 2]}',
        b'{"values": [["a"], [{"k": 1}]]}',
        b'{"values": [["a"], [null]]}',
        b'{"values": [["a"], [[1]]]}',
        b'{"values": null}',
    ],
)
async def test_a_body_that_is_not_a_value_range_is_22p02(account: Account, body: bytes) -> None:
    connector = _mock(account, lambda _: httpx2.Response(200, content=body))
    with pytest.raises(QueryFailedError) as odd:
        await read(connector, ask("Orders"))
    assert odd.value.sqlstate == "22P02"


@pytest.mark.parametrize(
    ("status", "error", "sqlstate"),
    [
        (400, QueryFailedError, "42P01"),
        (401, QueryFailedError, "28000"),
        (403, QueryFailedError, "42501"),
        (404, QueryFailedError, "42P01"),
        (409, QueryFailedError, None),
        (429, UpstreamUnavailableError, None),
        (503, UpstreamUnavailableError, None),
        (302, QueryFailedError, None),
    ],
)
async def test_each_status_maps_to_its_error(
    account: Account, status: int, error: type[Exception], sqlstate: str | None
) -> None:
    connector = _mock(account, lambda _: httpx2.Response(status, json={"error": {}}))
    with pytest.raises(error) as failed:
        await read(connector, ask("Orders"))
    assert getattr(failed.value, "sqlstate", None) == sqlstate


def test_header_names() -> None:
    assert header_names([]) == []
    assert header_names(["a", "", "a", "a", "a_2", 1.5, False, "col2"]) == [
        "a",
        "col2",
        "a_2",
        "a_3",
        "a_2_2",
        "1.5",
        "false",
        "col2_2",
    ]


def test_columns_take_their_type_from_the_first_non_null_value() -> None:
    values: list[list[Any]] = [
        ["b", "i", "f", "s", "n"],
        ["", "", 1.5, "x"],
        [True, 3, 2, "", ""],
    ]
    columns, rows = table(values, 10)
    assert columns == [
        Column("b", "boolean", "boolean"),
        Column("i", "integer", "number"),
        Column("f", "float", "number"),
        Column("s", "string", "string"),
        Column("n", "string", "string"),
    ]
    assert rows == [[None, None, 1.5, "x", None], [True, 3, 2, None, None]]


def test_only_the_first_100_rows_decide_a_type() -> None:
    values: list[list[Any]] = [["x"]] + [[""]] * TYPE_SAMPLE + [[5]]
    assert table(values, 10)[0] == [Column("x", "string", "string")]
    assert table(values[:1] + values[2:], 10)[0] == [Column("x", "integer", "number")]


def test_the_type_sample_does_not_depend_on_the_cap() -> None:
    values: list[list[Any]] = [["x"], [""], [""], [7]]
    columns, rows = table(values, 1)
    assert columns == [Column("x", "integer", "number")]
    assert rows == [[None]]


def test_values_in_takes_google_leaving_values_out_as_none() -> None:
    assert values_in({"range": "'Empty'", "majorDimension": "ROWS"}) == []
    rows: Sequence[Any] = [["a", 1, 1.5, True]]
    assert values_in({"values": list(rows)}) == rows
