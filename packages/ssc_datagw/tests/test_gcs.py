"""The GCS connector against a fake Cloud Storage JSON API over TLS in-process (GA-5 B6).

Each test that reads starts a FastAPI app under ``uvicorn`` on 127.0.0.1 with a server
certificate for ``localhost`` from the test PKI, so every read crosses a real TLS socket. The app
answers ``objects.list`` and ``objects.get?alt=media`` as Google does: it verifies the bearer JWT
against the test service account's public key (RS256, ``kid``, ``iss`` = ``sub`` = the email,
``aud`` exact, at most an hour) and answers 401 otherwise; it serves bucket ``corp-exports`` with
the S3 fake's objects, pages a list 20 objects at a time, lists any prefix (the binding's
bucket-level clause allows that), refuses an object read outside ``exports/`` with 403 (the
binding's condition), and answers 404 for a missing object or an unknown bucket, every error in
Google's JSON shape. The service account's key is made here, never read from disk. The connector
suite runs against it; the target, the error table and the list parsing are unit-tested without
it."""

import asyncio
import base64
import hashlib
import json
import logging
import socket
import time
import traceback
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx2
import jwt
import pytest
import uvicorn
from connector_suite import CHECKS, Subject, ask, conform, read
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pki import Pki, make_pki
from pydantic import ValidationError

from ssc_datagw.connectors import (
    Column,
    QueryFailedError,
    QueryRefusedError,
    UpstreamUnavailableError,
)
from ssc_datagw.gcs import (
    AUDIENCE,
    FIELDS,
    GcsConnector,
    GcsTarget,
    error_reason,
    gcs_failure,
    object_list,
)
from ssc_datagw.tls import tls_context

BUCKET = "corp-exports"
PREFIX = "exports/"
EMAIL = "reader@fake-project.iam.gserviceaccount.com"
FAKE_PAGE = 20
BODY_MARK = "do-not-echo-the-body"
"""In every error body the fake sends; no error may carry it."""
ODD_KEY = "exports/odd name +&=ü%41.csv"
ORDERS_CSV = (
    '﻿id,name,price,active,note\n1,widget,2.50,true,\n2,gadget,10,false,"a, b"\n3,gizmo,,true,x\n'
)
ORDERS_COLUMNS = [
    Column("id", "integer", "number"),
    Column("name", "string", "string"),
    Column("price", "float", "number"),
    Column("active", "boolean", "boolean"),
    Column("note", "string", "string"),
]
ORDERS_ROWS = [
    [1, "widget", 2.5, True, None],
    [2, "gadget", 10, False, "a, b"],
    [3, "gizmo", None, True, "x"],
]
OBJECTS: dict[str, bytes] = {
    "exports/orders.csv": ORDERS_CSV.encode(),
    "exports/orders.json": json.dumps(
        [{"id": 1, "name": "widget", "tags": ["a"]}, {"id": 2, "name": None}, {"id": 3}]
    ).encode(),
    "exports/one.json": b'{"id": 7, "name": "seven"}',
    "exports/events.jsonl": b'{"at": "2026-01-01", "n": 1}\r\n\n{"at": "2026-01-02", "n": 2.5}\n',
    "exports/events.ndjson": b'[1, 2]\n"x"\n',
    "exports/notes.txt": b"just text",
    "exports/bad.csv": b'a,b\n1,"x"y\n',
    "exports/bad.json": b'{"a": ',
    "exports/latin1.csv": b"a\n\xe9\n",
    ODD_KEY: b"k\nv\n",
    "other/x.csv": b"a\n1\n",
    **{f"exports/many/k{i:02}.csv": b"a\n1\n" for i in range(50)},
}
LIST_COLUMNS = [
    Column("key", "string", "string"),
    Column("size", "integer", "integer"),
    Column("last_modified", "timestamp", "timestamp"),
    Column("etag", "string", "string"),
]


def etag(key: str) -> str:
    return base64.b64encode(hashlib.md5(OBJECTS[key]).digest()).decode()  # noqa: S324


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
    """What the app saw: every request's raw path and query, every bearer token, the header and
    claims of each it accepted, and every user agent."""

    requests: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    bearers: list[str] = field(default_factory=list[str])
    headers: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    claims: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    agents: list[str] = field(default_factory=list[str])
    served: list[str] = field(default_factory=list[str])


def google_error(code: int, reason: str) -> JSONResponse:
    message = f"{reason} from the fake: {BODY_MARK}"
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "errors": [{"message": message, "domain": "global", "reason": reason}],
            }
        },
        status_code=code,
    )


def _token(index: int) -> str:
    """An opaque page token with the characters Google's carry."""
    return f"tok/{index}+=="


def _item(i: int, key: str) -> dict[str, str]:
    return {
        "name": key,
        "size": str(len(OBJECTS[key])),
        "updated": f"2026-10-07T12:00:{i % 60:02}.123Z",
        "etag": etag(key),
    }


def app(account: Account, seen: Seen) -> FastAPI:  # noqa: C901  (one fake, each answer)
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

    def listing(request: Request) -> Response:
        params = request.query_params
        if params.get("fields") != FIELDS:
            return google_error(400, "invalid")
        prefix = params.get("prefix", "")
        if prefix == "exports/notjson/":
            return Response(b"<html>not json</html>", media_type="text/html")
        keys = sorted(k for k in OBJECTS if k.startswith(prefix))
        token = params.get("pageToken")
        start = int(token.split("/")[1].split("+")[0]) if token else 0
        size = min(int(params.get("maxResults", "1000")), FAKE_PAGE)
        page = keys[start : start + size]
        answer: dict[str, Any] = {}
        if page:
            answer["items"] = [_item(start + i, k) for i, k in enumerate(page)]
        if start + size < len(keys):
            answer["nextPageToken"] = _token(start + size)
        return JSONResponse(answer)

    @api.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def anything(request: Request) -> Response:
        raw_path = request.scope["raw_path"].decode()
        seen.requests.append((raw_path, request.scope["query_string"].decode()))
        seen.agents.append(request.headers.get("user-agent", ""))
        if not verified(request):
            return google_error(401, "authError")
        parts = raw_path.split("/")
        if parts[:4] != ["", "storage", "v1", "b"] or len(parts) not in (6, 7) or parts[5] != "o":
            return google_error(404, "notFound")
        if request.method != "GET":
            return google_error(403, "forbidden")
        if parts[4] != BUCKET:
            return google_error(404, "notFound")
        if len(parts) == 6:
            return listing(request)
        if request.query_params.get("alt") != "media":
            return google_error(400, "invalid")
        key = unquote(parts[6])
        if not key.startswith(PREFIX):
            return google_error(403, "forbidden")
        if key == "exports/slow.csv":
            await asyncio.sleep(20)
            return Response(b"a\n1\n")
        if key not in OBJECTS:
            return google_error(404, "notFound")
        seen.served.append(key)
        return Response(OBJECTS[key], media_type="application/octet-stream")

    return api


@dataclass(frozen=True)
class Source:
    """The running app, its PKI, its service account and ways in."""

    pki: Pki
    base_url: str
    account: Account
    seen: Seen

    def target(self, **update: Any) -> GcsTarget:
        return GcsTarget.model_validate(
            {"bucket": BUCKET, "prefix": PREFIX, "service_account": self.account.json()} | update
        )

    def connector(self, base_url: str | None = None, **update: Any) -> GcsConnector:
        return GcsConnector(
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
    """The GCS connector as the connector suite sees it (GA-5)."""
    target = s.target()
    return Subject(
        connector=s.connector(),
        unreachable=s.connector(base_url="https://localhost:9"),
        credential="".join(s.account.body)[100:140],
        secrets=(target,),
        read="get exports/orders.csv",
        read_columns={c.name: c.type for c in ORDERS_COLUMNS},
        first_row=ORDERS_ROWS[0],
        writes=(
            "put exports/x.csv",
            "delete exports/x.csv",
            "get ../etc/passwd",
            "get other/x.csv",
            "list other/",
            "SELECT * FROM x",
        ),
        bad="get exports/missing.csv",
        slow="get exports/slow.csv",
        params=None,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_the_gcs_connector_conforms(
    source: Source, check: Callable[[Subject], Awaitable[None]]
) -> None:
    await conform(check, subject(source))


async def test_a_list_pages_to_max_rows_plus_one(source: Source) -> None:
    keys = sorted(k for k in OBJECTS if k.startswith("exports/many/"))
    columns, rows = await read(source.connector(), ask("list exports/many/", max_rows=10))
    assert columns == LIST_COLUMNS
    assert [r[0] for r in rows] == keys[:11]
    assert len(source.seen.requests) == 1
    _, rows = await read(source.connector(), ask("list exports/many/", max_rows=1000))
    assert [r[0] for r in rows] == keys
    pages = [httpx2.QueryParams(q) for _, q in source.seen.requests[1:]]
    assert len(pages) == 3, "50 objects at 20 a page"
    assert [p.get("pageToken") for p in pages] == [None, _token(20), _token(40)]
    assert [p["maxResults"] for p in pages] == ["1000", "981", "961"]
    assert all(p["prefix"] == "exports/many/" and p["fields"] == FIELDS for p in pages)
    assert {path for path, _ in source.seen.requests} == {f"/storage/v1/b/{BUCKET}/o"}
    key, size, modified, tag = rows[0]
    assert (key, size, tag) == (keys[0], len(OBJECTS[keys[0]]), etag(keys[0]))
    assert modified == datetime(2026, 10, 7, 12, 0, 0, 123000, tzinfo=UTC)
    _, rows = await read(source.connector(), ask("list exports/many/", max_rows=4))
    assert len(rows) == 5
    assert httpx2.QueryParams(source.seen.requests[-1][1])["maxResults"] == "5"


async def test_a_list_of_nothing_is_no_rows(source: Source) -> None:
    columns, rows = await read(source.connector(), ask("list exports/none/"))
    assert (columns, rows) == (LIST_COLUMNS, [])


async def test_a_csv_object_is_typed_from_its_text(source: Source) -> None:
    columns, rows = await read(source.connector(), ask("get exports/orders.csv"))
    assert columns == ORDERS_COLUMNS
    assert [list(r) for r in rows] == ORDERS_ROWS
    assert source.seen.requests == [(f"/storage/v1/b/{BUCKET}/o/exports%2Forders.csv", "alt=media")]


async def test_json_objects_are_records(source: Source) -> None:
    columns, rows = await read(source.connector(), ask("get exports/orders.json"))
    assert columns == [
        Column("id", "integer", "number"),
        Column("name", "string", "string"),
        Column("tags", "json", "array"),
    ]
    assert [list(r) for r in rows] == [[1, "widget", ["a"]], [2, None, None], [3, None, None]]
    _, rows = await read(source.connector(), ask("get exports/one.json"))
    assert [list(r) for r in rows] == [[7, "seven"]]


async def test_jsonl_and_ndjson_objects_are_one_record_per_line(source: Source) -> None:
    columns, rows = await read(source.connector(), ask("GET exports/events.jsonl"))
    assert columns == [Column("at", "string", "string"), Column("n", "integer", "number")]
    assert [list(r) for r in rows] == [["2026-01-01", 1], ["2026-01-02", 2.5]]
    columns, rows = await read(source.connector(), ask("get exports/events.ndjson"))
    assert columns == [Column("value", "json", "array")]
    assert [list(r) for r in rows] == [[[1, 2]], ["x"]]


async def test_an_odd_name_is_encoded_slashes_too_and_read(source: Source) -> None:
    _, rows = await read(source.connector(), ask(f"get {ODD_KEY}"))
    assert [list(r) for r in rows] == [["v"]]
    path, query = source.seen.requests[-1]
    assert path == f"/storage/v1/b/{BUCKET}/o/exports%2Fodd%20name%20%2B%26%3D%C3%BC%2541.csv"
    assert query == "alt=media"
    assert source.seen.served == [ODD_KEY]
    _, listed = await read(source.connector(), ask("list exports/odd name +&"))
    assert [r[0] for r in listed] == [ODD_KEY]


async def test_the_jwt_is_self_signed_for_the_storage_api(source: Source) -> None:
    before = int(time.time())
    for _ in range(2):
        await read(source.connector(), ask("list exports/many/", max_rows=1000))
    assert len(source.seen.claims) == 6, "every request carried a JWT the fake verified"
    assert len(set(source.seen.bearers[:3])) == 1, "one JWT for the pages of one read"
    header, claims = source.seen.headers[-1], source.seen.claims[-1]
    assert header["alg"] == "RS256"
    assert header["kid"] == source.account.kid
    assert claims["iss"] == claims["sub"] == EMAIL
    assert claims["aud"] == "https://storage.googleapis.com/"
    assert claims["exp"] - claims["iat"] == 3600
    assert before - 5 <= claims["iat"] <= int(time.time()) + 5
    assert set(claims) == {"iss", "sub", "aud", "iat", "exp"}


async def test_another_key_or_email_is_28000(source: Source) -> None:
    other_key = make_account(kid=source.account.kid)
    other_email = make_account(email="other@fake-project.iam.gserviceaccount.com")
    for account in (other_key, other_email):
        connector = source.connector(service_account=account.json())
        for sql in ("get exports/orders.csv", "list exports/"):
            with pytest.raises(QueryFailedError) as refused:
                await read(connector, ask(sql))
            assert refused.value.sqlstate == "28000"
            assert str(refused.value) == "the source refused the credential"
    assert source.seen.served == []


async def test_a_txt_object_and_params_are_refused_before_the_source(source: Source) -> None:
    with pytest.raises(QueryRefusedError, match=r"^only \.csv, \.json, \.jsonl and \.ndjson "):
        await read(source.connector(), ask("get exports/notes.txt"))
    with pytest.raises(QueryRefusedError, match="^a GCS read takes no parameters$"):
        await read(source.connector(), ask("get exports/orders.csv", 1))
    with pytest.raises(QueryRefusedError, match="^the query is not list <prefix> or get <key>"):
        await read(source.connector(), ask("get other/x.csv"))
    assert source.seen.requests == []


async def test_an_object_outside_the_binding_is_42501(source: Source) -> None:
    with pytest.raises(QueryFailedError) as denied:
        await read(source.connector(prefix=""), ask("get other/x.csv"))
    assert denied.value.sqlstate == "42501"
    assert str(denied.value) == "the source answered 403 forbidden"
    _, rows = await read(source.connector(prefix=""), ask("list other/"))
    assert [r[0] for r in rows] == ["other/x.csv"], "the bucket-level clause lists every name"


async def test_a_missing_object_or_bucket_is_42p01(source: Source) -> None:
    for connector, sql in (
        (source.connector(), "get exports/missing.csv"),
        (source.connector(bucket="no-such-bucket"), "get exports/orders.csv"),
        (source.connector(bucket="no-such-bucket"), "list exports/"),
    ):
        with pytest.raises(QueryFailedError) as missing:
            await read(connector, ask(sql))
        assert missing.value.sqlstate == "42P01"
        assert str(missing.value) == "the source answered 404 notFound"


@pytest.mark.parametrize("sql", ["get exports/bad.csv", "get exports/bad.json"])
async def test_a_body_that_does_not_parse_is_22p02(source: Source, sql: str) -> None:
    for query in (sql, "get exports/latin1.csv", "list exports/notjson/"):
        with pytest.raises(QueryFailedError) as corrupt:
            await read(source.connector(), ask(query))
        assert corrupt.value.sqlstate == "22P02"


async def test_another_ca_is_refused(source: Source) -> None:
    connector = GcsConnector(
        source.target(),
        base_url=source.base_url,
        transport=httpx2.AsyncHTTPTransport(verify=tls_context(source.pki.other_ca)),
    )
    with pytest.raises(UpstreamUnavailableError, match="^cannot connect: "):
        await read(connector, ask("get exports/orders.csv"))
    assert source.seen.bearers == [], "no request crossed a refused handshake"


async def test_the_tag_travels_in_the_user_agent(source: Source) -> None:
    await read(source.connector(), ask("get exports/orders.csv", tag="ssc:app_1:env_1:req-1"))
    assert source.seen.agents == ["ssc-datagw (ssc:app_1:env_1:req-1)"]


async def test_the_credential_is_absent_from_errors_and_logs(
    source: Source, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _, rows = await read(source.connector(), ask("list exports/many/", max_rows=100))
    assert len(rows) == 50, "three pages were accepted"
    stranger = make_account(kid=source.account.kid)
    failing = [
        (source.connector(), ask(sql))
        for sql in (
            "get exports/missing.csv",
            "get exports/bad.csv",
            "list exports/notjson/",
            "get exports/notes.txt",
            "SELECT 1",
        )
    ]
    failing += [
        (source.connector(prefix=""), ask("get other/x.csv")),
        (source.connector(bucket="no-such-bucket"), ask("list exports/")),
        (source.connector(service_account=stranger.json()), ask("get exports/orders.csv")),
        (source.connector(base_url="https://localhost:9"), ask("get exports/orders.csv")),
        (source.connector(), ask("get exports/slow.csv", timeout_ms=200)),
        (source.connector(), ask("get exports/orders.csv", 1)),
    ]
    for connector, query in failing:
        with pytest.raises(
            (QueryFailedError, QueryRefusedError, UpstreamUnavailableError, TimeoutError)
        ) as failed:
            await read(connector, query)
        shown = "".join(traceback.format_exception(failed.value)) + repr(failed.value)
        shown += repr(connector) + str(connector)
        for secret in (*source.secrets(), BODY_MARK):
            assert secret not in shown, query.sql
    assert len(source.seen.bearers) >= 8
    for secret in source.secrets():
        assert secret not in caplog.text
    assert "gcs read: op=list status=200 bytes=" in caplog.text
    assert "pages=3" in caplog.text


def test_a_target_hides_its_service_account(account: Account) -> None:
    target = GcsTarget(bucket=BUCKET, service_account=account.json())  # pyright: ignore[reportArgumentType]
    connector = GcsConnector(target)
    for shown in (repr(target), str(target), repr(connector), str(connector)):
        for secret in (account.json(), account.email, account.kid, *account.body):
            assert secret not in shown


def test_a_service_account_that_is_not_a_key_is_refused_without_quoting_it(
    account: Account,
) -> None:
    full = json.loads(account.json())
    bad = [
        "not json " + account.email,
        json.dumps({k: v for k, v in full.items() if k != "private_key"}),
        account.json(client_email=""),
        account.json(private_key=account.pem.replace(account.body[3], account.body[3][::-1])),
        account.json()[:-40],
    ]
    for service_account in bad:
        with pytest.raises(ValidationError) as refused:
            GcsTarget(bucket=BUCKET, service_account=service_account)  # pyright: ignore[reportArgumentType]
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
        {"bucket": "UPPER"},
        {"bucket": "ab"},
        {"bucket": "-bucket"},
        {"bucket": "bucket_"},
        {"bucket": "a/b"},
        {"bucket": "a" * 224},
        {"prefix": "a\nb"},
        {"prefix": "a" * 513},
        {"kind": "s3"},
        {"region": "eu-west-2"},
        {"ca": "x"},
    ],
)
def test_the_target_refuses_a_bad_address(account: Account, update: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as refused:
        GcsTarget.model_validate({"bucket": BUCKET, "service_account": account.json()} | update)
    for secret in (account.json(), account.email, *account.body):
        assert secret not in str(refused.value)
        assert secret not in repr(refused.value)


def test_the_target_takes_the_control_planes_address(account: Account) -> None:
    for bucket, prefix in (("my.bucket_1-x", ""), ("a" * 223, "exports/"), ("abc", "é ü/")):
        target = GcsTarget.model_validate(
            {"bucket": bucket, "prefix": prefix, "service_account": account.json()}
        )
        assert (target.kind, target.bucket, target.prefix) == ("gcs", bucket, prefix)
    bare = GcsTarget.model_validate({"bucket": BUCKET, "service_account": account.json()})
    assert bare.prefix == ""


def _mock(account: Account, answer: Callable[[httpx2.Request], httpx2.Response]) -> GcsConnector:
    target = GcsTarget(bucket=BUCKET, prefix=PREFIX, service_account=account.json())  # pyright: ignore[reportArgumentType]
    return GcsConnector(target, transport=httpx2.MockTransport(answer))


async def test_the_requests_go_to_the_json_api(account: Account) -> None:
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        if request.url.params.get("alt") == "media":
            return httpx2.Response(200, content=b"a\n1\n")
        return httpx2.Response(200, json={})

    await read(_mock(account, answer), ask("get exports/orders.csv", tag="a(b)\nc"))
    await read(_mock(account, answer), ask("list exports/x y", max_rows=100))
    get_request, list_request = sent
    assert get_request.method == list_request.method == "GET"
    assert str(get_request.url) == (
        f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/exports%2Forders.csv?alt=media"
    )
    assert list_request.url.host == "storage.googleapis.com"
    assert list_request.url.path == f"/storage/v1/b/{BUCKET}/o"
    assert dict(list_request.url.params) == {
        "prefix": "exports/x y",
        "maxResults": "101",
        "fields": FIELDS,
    }
    assert get_request.headers["user-agent"] == "ssc-datagw (abc)"
    assert list_request.headers["accept"] == "application/json"
    for request in sent:
        assert request.headers["authorization"].startswith("Bearer ey")


@pytest.mark.parametrize(
    ("status", "reason", "error", "sqlstate", "message"),
    [
        (302, None, QueryFailedError, None, "the source answered 302"),
        (400, "invalid", QueryFailedError, None, "the source answered 400 invalid"),
        (401, "authError", QueryFailedError, "28000", "the source refused the credential"),
        (401, None, QueryFailedError, "28000", "the source refused the credential"),
        (403, "forbidden", QueryFailedError, "42501", "the source answered 403 forbidden"),
        (403, "a b<c>", QueryFailedError, "42501", "the source answered 403"),
        (404, "notFound", QueryFailedError, "42P01", "the source answered 404 notFound"),
        (404, None, QueryFailedError, "42P01", "the source answered 404"),
        (409, "conflict", QueryFailedError, None, "the source answered 409 conflict"),
        (
            429,
            "rateLimitExceeded",
            UpstreamUnavailableError,
            None,
            "the source answered 429 rateLimitExceeded",
        ),
        (
            500,
            "backendError",
            UpstreamUnavailableError,
            None,
            "the source answered 500 backendError",
        ),
        (503, None, UpstreamUnavailableError, None, "the source answered 503"),
    ],
)
async def test_each_status_maps_to_its_error(  # noqa: PLR0913  (one row of the table)
    account: Account,
    status: int,
    reason: str | None,
    error: type[Exception],
    sqlstate: str | None,
    message: str,
) -> None:
    def answer(_: httpx2.Request) -> httpx2.Response:
        if reason is None:
            return httpx2.Response(status, content=f"No such object: {BODY_MARK}".encode())
        body = {"error": {"code": status, "message": BODY_MARK, "errors": [{"reason": reason}]}}
        return httpx2.Response(status, json=body)

    for sql in ("get exports/orders.csv", "list exports/"):
        with pytest.raises(error) as failed:
            await read(_mock(account, answer), ask(sql))
        assert str(failed.value) == message
        assert getattr(failed.value, "sqlstate", None) == sqlstate


def test_gcs_failure_reads_on_a_2xx_and_error_reason_reads_googles_shape() -> None:
    assert gcs_failure(200, None) is None
    assert gcs_failure(204, "x") is None
    shape = {"error": {"code": 404, "errors": [{"reason": "notFound"}, {"reason": "other"}]}}
    assert error_reason(json.dumps(shape).encode()) == "notFound"
    for body in (b"", b"not json", b"[]", b'{"error": "x"}', b'{"error": {"errors": []}}'):
        assert error_reason(body) is None


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"items": {}}',
        b'{"items": [1]}',
        b'{"items": [{"name": "a", "size": 1, "updated": "2026-01-01T00:00:00Z"}]}',
        b'{"items": [{"name": "a", "size": "x", "updated": "2026-01-01T00:00:00Z"}]}',
        b'{"items": [{"name": "a", "size": "-1", "updated": "2026-01-01T00:00:00Z"}]}',
        b'{"items": [{"name": "a", "size": "1", "updated": "2026-01-01T00:00:00"}]}',
        b'{"items": [{"name": "a", "size": "1", "updated": "yesterday"}]}',
        b'{"items": [{"size": "1", "updated": "2026-01-01T00:00:00Z"}]}',
        b'{"items": [{"name": "a", "size": "1", "updated": "2026-01-01T00:00:00Z", "etag": 5}]}',
        b'{"nextPageToken": 5}',
    ],
)
async def test_a_list_body_that_is_not_an_object_list_is_22p02(
    account: Account, body: bytes
) -> None:
    with pytest.raises(QueryFailedError) as corrupt:
        object_list(body)
    assert corrupt.value.sqlstate == "22P02"
    connector = _mock(account, lambda _: httpx2.Response(200, content=body))
    with pytest.raises(QueryFailedError) as listed:
        await read(connector, ask("list exports/"))
    assert listed.value.sqlstate == "22P02"


def test_object_list_reads_items_and_the_token() -> None:
    body = {
        "items": [
            {"name": "a.csv", "size": "12", "updated": "2026-10-07T12:00:00.5+02:00", "etag": "e"},
            {"name": "b.csv", "size": "0", "updated": "2026-10-07T12:00:00Z"},
        ],
        "nextPageToken": "t/1+=",
    }
    rows, token = object_list(json.dumps(body).encode())
    assert token == "t/1+="
    assert rows == [
        ["a.csv", 12, datetime(2026, 10, 7, 10, 0, 0, 500000, tzinfo=UTC), "e"],
        ["b.csv", 0, datetime(2026, 10, 7, 12, 0, tzinfo=UTC), None],
    ]
    assert object_list(b"{}") == ([], None)
    assert object_list(b'{"nextPageToken": ""}') == ([], None)
