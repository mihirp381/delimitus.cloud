"""The S3 connector on a contract fake that verifies SigV4 and on S3Mock 4.9.1 (GA-5 B7).

Two subjects run the connector suite. The fake is a FastAPI app under ``uvicorn`` on 127.0.0.1
over TLS from the test PKI, as for REST: it recomputes each request's AWS Signature Version 4
with the same secret (its own code, not the connector's), answers in S3's XML shape, pages a list
20 keys at a time, refuses what the IAM policy would (anything outside ``exports/``) with
``AccessDenied``, and serves a slow object for the time-out and cancel checks. S3Mock (in a
container, HTTPS from a PKCS12 keystore made from the same PKI, seeded with signed PUTs) ignores
credentials, so it proves the wire protocol, the paths, the XML and the encoding, not the
signature; the time-out and cancel checks run on the fake only. The grammar, the target, the
signing and the record rules are unit-tested without either."""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import socket
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx2
import pytest
import uvicorn
from connector_suite import CHECKS, Subject, ask, conform, read
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from fastapi import FastAPI, Request, Response
from pki import Pki, make_pki
from pydantic import ValidationError
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from ssc_datagw.connectors import (
    Column,
    QueryFailedError,
    QueryRefusedError,
    UpstreamUnavailableError,
)
from ssc_datagw.s3 import (
    EMPTY_SHA256,
    S3Connector,
    S3Target,
    csv_cell,
    list_page,
    s3_failure,
    s3_request,
    sign,
    user_agent,
)
from ssc_datagw.tls import tls_context

KEY_ID = "FAKEKEYID0000001"
SECRET = "fake/" + "s3-secret+" + "Key0123456789"
BUCKET = "ssc-test-bucket"
REGION = "eu-west-2"
PREFIX = "exports/"
S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"
FAKE_PAGE = 20
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
STATUSES: dict[str, tuple[int, str]] = {
    "exports/status/moved.csv": (301, "PermanentRedirect"),
    "exports/status/region.csv": (400, "AuthorizationHeaderMalformed"),
    "exports/status/invalid.csv": (400, "InvalidArgument"),
    "exports/status/throttled.csv": (429, "SlowDown"),
    "exports/status/slowdown.csv": (503, "SlowDown"),
    "exports/status/boom.csv": (500, "InternalError"),
}
"""Keys the fake answers with an S3 error."""


def error_xml(code: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>{code}</Code>'
        f"<Message>{code} from the fake</Message><RequestId>R1</RequestId></Error>"
    ).encode()


def _enc(text: str) -> str:
    return quote(text, safe="")


def expected_signature(request: Request, body: bytes) -> str | None:
    """The fake's own SigV4 check (written apart from ``ssc_datagw.s3.sign``): the S3 error code
    to answer, or ``None`` when the signature is good."""
    found = re.fullmatch(
        r"AWS4-HMAC-SHA256 Credential=([A-Z0-9]+)/(\d{8})/([a-z0-9-]+)/s3/aws4_request, "
        r"SignedHeaders=([a-z0-9;-]+), Signature=([0-9a-f]{64})",
        request.headers.get("authorization", ""),
    )
    if found is None:
        return "AccessDenied"
    key_id, day, region, signed, signature = found.groups()
    if key_id != KEY_ID:
        return "InvalidAccessKeyId"
    names = signed.split(";")
    stamp = request.headers.get("x-amz-date", "")
    if (
        names != sorted(names)
        or not {"host", "x-amz-date", "x-amz-content-sha256"} <= set(names)
        or region != REGION
        or not stamp.startswith(day)
    ):
        return "SignatureDoesNotMatch"
    when = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    if abs(datetime.now(UTC) - when) > timedelta(minutes=15):
        return "RequestTimeTooSkewed"
    payload = request.headers.get("x-amz-content-sha256", "")
    if payload != hashlib.sha256(body).hexdigest():
        return "SignatureDoesNotMatch"
    raw_path = request.scope["raw_path"].decode()
    path = "/".join(_enc(unquote(s)) for s in raw_path.split("/"))
    pairs = sorted(
        (_enc(unquote(n)), _enc(unquote(v)))
        for n, _, v in (p.partition("=") for p in request.scope["query_string"].decode().split("&"))
        if n
    )
    query = "&".join(f"{n}={v}" for n, v in pairs)
    headers = "".join(f"{n}:{' '.join(request.headers[n].split())}\n" for n in names)
    canonical = f"{request.method}\n{path}\n{query}\n{headers}\n{signed}\n{payload}"
    scope = f"{day}/{region}/s3/aws4_request"
    text = f"AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    key = f"AWS4{SECRET}".encode()
    for part in (day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    good = hmac.new(key, text.encode(), hashlib.sha256).hexdigest()
    return None if hmac.compare_digest(good, signature) else "SignatureDoesNotMatch"


def list_xml(keys: list[str], *, prefix: str, token: str | None, next_token: str | None) -> bytes:
    contents = "".join(
        f"<Contents><Key>{k.replace('&', '&amp;')}</Key>"
        f"<LastModified>2026-10-07T12:00:{i % 60:02}.000Z</LastModified>"
        f'<ETag>"{hashlib.md5(OBJECTS[k]).hexdigest()}"</ETag>'  # noqa: S324  (S3's ETag)
        f"<Size>{len(OBJECTS[k])}</Size><StorageClass>STANDARD</StorageClass></Contents>"
        for i, k in enumerate(keys)
    )
    tokens = f"<ContinuationToken>{token}</ContinuationToken>" if token else ""
    tokens += f"<NextContinuationToken>{next_token}</NextContinuationToken>" if next_token else ""
    truncated = "true" if next_token else "false"
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<ListBucketResult xmlns="{S3_NS}">'
        f"<Name>{BUCKET}</Name><Prefix>{prefix.replace('&', '&amp;')}</Prefix>"
        f"<KeyCount>{len(keys)}</KeyCount><MaxKeys>1000</MaxKeys>"
        f"<IsTruncated>{truncated}</IsTruncated>{tokens}{contents}</ListBucketResult>"
    ).encode()


def _token(index: int) -> str:
    """An opaque token with the characters S3's carry (``/``, ``+``, ``=``)."""
    return f"tok/{index}+=="


@dataclass
class Seen:
    """What the fake saw: each request's path and query, and every ``Authorization`` value."""

    requests: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    authorizations: list[str] = field(default_factory=list[str])
    agents: list[str] = field(default_factory=list[str])


def xml(body: bytes, status: int = 200) -> Response:
    return Response(body, status_code=status, media_type="application/xml")


def fake_s3(seen: Seen) -> FastAPI:  # noqa: C901  (one fake, each answer)
    api = FastAPI()

    def listing(request: Request) -> Response:
        params = request.query_params
        prefix = params.get("prefix", "")
        if params.get("list-type") != "2":
            return xml(error_xml("InvalidArgument"), 400)
        if not prefix.startswith(PREFIX):
            return xml(error_xml("AccessDenied"), 403)
        if prefix == "exports/brokenxml/":
            return xml(b"<ListBucketResult><Contents>")
        if prefix == "exports/doctype/":
            return xml(b'<!DOCTYPE x [<!ENTITY a "aaaa">]><ListBucketResult/>')
        keys = sorted(k for k in OBJECTS if k.startswith(prefix))
        token = params.get("continuation-token")
        start = int(token.split("/")[1].split("+")[0]) if token else 0
        size = min(int(params.get("max-keys", "1000")), FAKE_PAGE)
        page = keys[start : start + size]
        more = start + size < len(keys)
        return xml(
            list_xml(
                page, prefix=prefix, token=token, next_token=_token(start + size) if more else None
            )
        )

    @api.api_route("/{rest:path}", methods=["GET", "PUT", "DELETE"])
    async def anything(request: Request) -> Response:
        body = await request.body()
        seen.requests.append(
            (request.scope["raw_path"].decode(), request.scope["query_string"].decode())
        )
        seen.authorizations.append(request.headers.get("authorization", ""))
        seen.agents.append(request.headers.get("user-agent", ""))
        refused = expected_signature(request, body)
        if refused is not None:
            return xml(error_xml(refused), 403)
        if request.method != "GET":
            return xml(error_xml("AccessDenied"), 403)
        bucket, _, key = unquote(request.scope["raw_path"].decode()).lstrip("/").partition("/")
        if bucket != BUCKET:
            return xml(error_xml("NoSuchBucket"), 404)
        if not key:
            return listing(request)
        if not key.startswith(PREFIX):
            return xml(error_xml("AccessDenied"), 403)
        if key == "exports/slow.csv":
            await asyncio.sleep(20)
            return Response(b"a\n1\n")
        if key in STATUSES:
            status, code = STATUSES[key]
            return xml(error_xml(code), status)
        if key not in OBJECTS:
            return xml(error_xml("NoSuchKey"), 404)
        return Response(OBJECTS[key], media_type="application/octet-stream")

    return api


@dataclass(frozen=True)
class Source:
    """A running S3 (the fake or S3Mock), its PKI and ways in."""

    pki: Pki
    endpoint: str
    seen: Seen

    def target(self, **update: Any) -> S3Target:
        return S3Target.model_validate(
            {
                "bucket": BUCKET,
                "region": REGION,
                "prefix": PREFIX,
                "access_key_id": KEY_ID,
                "secret_access_key": SECRET,
                "endpoint": self.endpoint,
                "ca": self.pki.ca,
            }
            | update
        )

    def connector(self, page_keys: int = 1000, **update: Any) -> S3Connector:
        return S3Connector(self.target(**update), page_keys=page_keys)


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
        fake_s3(seen),
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


KEYSTORE_PASSWORD = "password"
"""S3Mock's own default key password, which its key-password setting keeps for the key; a test
keystore's, never a credential."""


STARTED = re.compile("Started S3MockApplication")


def keystore(pki: Pki) -> bytes:
    """The test PKI's server key and certificate as PKCS12, under S3Mock's default alias."""
    key = serialization.load_pem_private_key(pki.key.encode(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    cert = x509.load_pem_x509_certificate(pki.cert.encode())
    return pkcs12.serialize_key_and_certificates(
        b"selfsigned",
        key,
        cert,
        None,
        serialization.BestAvailableEncryption(KEYSTORE_PASSWORD.encode()),
    )


def seed(endpoint: str, ca: str) -> None:
    """Every object in :data:`OBJECTS` put with a signed PUT, as a customer's upload would be."""
    with httpx2.Client(verify=tls_context(ca), trust_env=False) as client:
        for key, body in OBJECTS.items():
            url = f"{endpoint}/{BUCKET}/" + "/".join(_enc(s) for s in key.split("/"))
            headers = sign(
                "PUT",
                url,
                {"x-amz-content-sha256": hashlib.sha256(body).hexdigest()},
                key_id=KEY_ID,
                secret=SECRET,
                region=REGION,
                now=datetime.now(UTC),
            )
            answer = client.put(url, content=body, headers=headers)
            assert answer.status_code == 200, (key, answer.status_code)


@pytest.fixture(scope="module")
def s3mock(pki: Pki, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Source]:
    certs = tmp_path_factory.mktemp("s3mock-certs")
    (certs / "server.p12").write_bytes(keystore(pki))
    (certs / "server.p12").chmod(0o644)
    certs.chmod(0o755)
    container = (
        DockerContainer("adobe/s3mock:4.9.1")
        .with_exposed_ports(9191)
        .with_volume_mapping(str(certs), "/certs", "ro")
        .with_env("SERVER_SSL_KEY_STORE", "/certs/server.p12")
        .with_env("SERVER_SSL_KEY_STORE_PASSWORD", KEYSTORE_PASSWORD)
        .with_env("SERVER_SSL_KEY_STORE_TYPE", "PKCS12")
        .with_env("initialBuckets", BUCKET)
        .waiting_for(LogMessageWaitStrategy(STARTED).with_startup_timeout(120))
    )
    with container as mock:
        endpoint = f"https://localhost:{mock.get_exposed_port(9191)}"
        seed(endpoint, pki.ca)
        yield Source(pki, endpoint, Seen())


def subject(s: Source, *, slow: str = "get exports/slow.csv") -> Subject:
    """The S3 connector as the connector suite sees it (GA-5)."""
    target = s.target()
    nowhere = target.model_copy(update={"endpoint": "https://localhost:9"})
    return Subject(
        connector=S3Connector(target),
        unreachable=S3Connector(nowhere, connect_seconds=2),
        credential=SECRET,
        secrets=(target, nowhere),
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
        slow=slow,
        params=None,
    )


ON_S3MOCK = [
    c
    for c in CHECKS
    if c.__name__ not in {"check_a_read_past_its_timeout_times_out", "check_a_cancelled_read_stops"}
]


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_the_s3_connector_conforms_on_the_fake(
    fake: Source, check: Callable[[Subject], Awaitable[None]]
) -> None:
    await conform(check, subject(fake))


@pytest.mark.parametrize("check", ON_S3MOCK, ids=lambda c: c.__name__.removeprefix("check_"))
async def test_the_s3_connector_conforms_on_s3mock(
    s3mock: Source, check: Callable[[Subject], Awaitable[None]]
) -> None:
    await conform(check, subject(s3mock, slow="get exports/orders.csv"))


@pytest.fixture(params=["fake", "s3mock"])
def source(request: pytest.FixtureRequest) -> Source:
    return request.getfixturevalue(request.param)


async def test_a_list_pages_to_max_rows_plus_one(source: Source) -> None:
    keys = sorted(k for k in OBJECTS if k.startswith("exports/many/"))
    for page_keys in (1000, 7):
        connector = source.connector(page_keys=page_keys)
        columns, rows = await read(connector, ask("list exports/many/", max_rows=10))
        assert columns == [
            Column("key", "string", "string"),
            Column("size", "integer", "integer"),
            Column("last_modified", "timestamp", "timestamp"),
            Column("etag", "string", "string"),
        ]
        assert [r[0] for r in rows] == keys[:11], page_keys
        _, rows = await read(connector, ask("list exports/many/", max_rows=1000))
        assert [r[0] for r in rows] == keys, page_keys
    key, size, modified, etag = rows[0]
    assert size == len(OBJECTS[keys[0]])
    assert isinstance(modified, datetime)
    assert modified.tzinfo is not None
    assert etag == hashlib.md5(OBJECTS[keys[0]]).hexdigest()  # noqa: S324  (S3's ETag)
    assert key == keys[0]


async def test_the_fake_list_follows_continuation_tokens(fake: Source) -> None:
    _, rows = await read(fake.connector(), ask("list exports/many/", max_rows=1000))
    assert len(rows) == 50
    queries = [q for _, q in fake.seen.requests]
    assert len(queries) == 3, "50 keys at 20 a page"
    assert "continuation-token" not in queries[0]
    assert "continuation-token=tok%2F20%2B%3D%3D" in queries[1]
    assert "max-keys=1000" in queries[0]
    _, rows = await read(fake.connector(), ask("list exports/many/", max_rows=4))
    assert len(rows) == 5
    assert "max-keys=5" in fake.seen.requests[-1][1]


async def test_a_bare_list_lists_the_empty_prefix(s3mock: Source) -> None:
    _, rows = await read(s3mock.connector(prefix=""), ask("list", max_rows=1000))
    assert {r[0] for r in rows} >= {"exports/orders.csv", "exports/many/k00.csv"}


async def test_a_csv_object_is_typed_from_its_text(source: Source) -> None:
    columns, rows = await read(source.connector(), ask("get exports/orders.csv"))
    assert columns == ORDERS_COLUMNS
    assert [list(r) for r in rows] == ORDERS_ROWS


async def test_json_objects_are_records(source: Source) -> None:
    columns, rows = await read(source.connector(), ask("get exports/orders.json"))
    assert columns == [
        Column("id", "integer", "number"),
        Column("name", "string", "string"),
        Column("tags", "json", "array"),
    ]
    assert [list(r) for r in rows] == [[1, "widget", ["a"]], [2, None, None], [3, None, None]]
    columns, rows = await read(source.connector(), ask("get exports/one.json"))
    assert [list(r) for r in rows] == [[7, "seven"]]


async def test_jsonl_and_ndjson_objects_are_one_record_per_line(source: Source) -> None:
    columns, rows = await read(source.connector(), ask("GET exports/events.jsonl"))
    assert columns == [Column("at", "string", "string"), Column("n", "integer", "number")]
    assert [list(r) for r in rows] == [["2026-01-01", 1], ["2026-01-02", 2.5]]
    columns, rows = await read(source.connector(), ask("get exports/events.ndjson"))
    assert columns == [Column("value", "json", "array")]
    assert [list(r) for r in rows] == [[[1, 2]], ["x"]]


async def test_an_odd_key_is_encoded_once_and_read(source: Source) -> None:
    _, rows = await read(source.connector(), ask(f"get {ODD_KEY}"))
    assert [list(r) for r in rows] == [["v"]]
    _, listed = await read(source.connector(), ask("list exports/odd name +&"))
    assert [r[0] for r in listed] == [ODD_KEY]


@pytest.mark.parametrize("key", ["exports/bad.csv", "exports/bad.json", "exports/latin1.csv"])
async def test_a_body_that_does_not_parse_is_22p02(source: Source, key: str) -> None:
    with pytest.raises(QueryFailedError) as corrupt:
        await read(source.connector(), ask(f"get {key}"))
    assert corrupt.value.sqlstate == "22P02"


async def test_a_missing_key_is_42p01(source: Source) -> None:
    with pytest.raises(QueryFailedError) as missing:
        await read(source.connector(), ask("get exports/missing.csv"))
    assert missing.value.sqlstate == "42P01"
    assert str(missing.value) == "the source answered 404 NoSuchKey"


async def test_another_ca_or_the_system_store_is_refused(source: Source) -> None:
    for connector in (source.connector(ca=source.pki.other_ca), source.connector(ca=None)):
        with pytest.raises(UpstreamUnavailableError, match="^cannot connect: "):
            await read(connector, ask("get exports/orders.csv"))


async def test_a_txt_object_is_refused_before_the_source(fake: Source) -> None:
    with pytest.raises(QueryRefusedError, match=r"^only \.csv, \.json, \.jsonl and \.ndjson "):
        await read(fake.connector(), ask("get exports/notes.txt"))
    assert fake.seen.requests == []


async def test_a_key_outside_the_policy_is_access_denied_42501(fake: Source) -> None:
    with pytest.raises(QueryFailedError) as denied:
        await read(fake.connector(prefix=""), ask("get other/x.csv"))
    assert denied.value.sqlstate == "42501"
    assert str(denied.value) == "the source answered 403 AccessDenied"
    for sql in ("list other/", "list"):
        with pytest.raises(QueryFailedError) as listed:
            await read(fake.connector(prefix=""), ask(sql))
        assert listed.value.sqlstate == "42501"


async def test_a_tampered_secret_is_signature_does_not_match_42501(fake: Source) -> None:
    updates: list[tuple[dict[str, Any], str]] = [
        ({"secret_access_key": SECRET + "x"}, "SignatureDoesNotMatch"),
        ({"access_key_id": "FAKEKEYID0000002"}, "InvalidAccessKeyId"),
        ({"region": "us-east-1"}, "SignatureDoesNotMatch"),
    ]
    for update, code in updates:
        with pytest.raises(QueryFailedError) as refused:
            await read(fake.connector(**update), ask("get exports/orders.csv"))
        assert refused.value.sqlstate == "42501"
        assert str(refused.value) == f"the source answered 403 {code}"


@pytest.mark.parametrize(
    ("key", "error", "message", "sqlstate"),
    [
        ("moved", UpstreamUnavailableError, "the bucket is in another region", None),
        ("region", UpstreamUnavailableError, "the bucket is in another region", None),
        ("invalid", QueryFailedError, "the source answered 400 InvalidArgument", None),
        ("throttled", UpstreamUnavailableError, "the source answered 429 SlowDown", None),
        ("slowdown", UpstreamUnavailableError, "the source answered 503 SlowDown", None),
        ("boom", UpstreamUnavailableError, "the source answered 500 InternalError", None),
    ],
)
async def test_each_s3_error_maps(
    fake: Source, key: str, error: type[Exception], message: str, sqlstate: str | None
) -> None:
    with pytest.raises(error) as failed:
        await read(fake.connector(), ask(f"get exports/status/{key}.csv"))
    assert str(failed.value) == message
    assert getattr(failed.value, "sqlstate", None) == sqlstate


@pytest.mark.parametrize("prefix", ["exports/brokenxml/", "exports/doctype/"])
async def test_a_list_body_that_is_not_s3_xml_is_22p02(fake: Source, prefix: str) -> None:
    with pytest.raises(QueryFailedError) as corrupt:
        await read(fake.connector(), ask(f"list {prefix}"))
    assert corrupt.value.sqlstate == "22P02"


async def test_the_tag_travels_in_the_user_agent(fake: Source) -> None:
    await read(fake.connector(), ask("get exports/orders.csv", tag="ssc:app_1:env_1:req-1"))
    assert fake.seen.agents == ["ssc-datagw (ssc:app_1:env_1:req-1)"]


async def test_the_secret_and_every_signature_are_hidden(
    fake: Source, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _, rows = await read(fake.connector(), ask("list exports/many/", max_rows=100))
    assert len(rows) == 50, "three signed pages were accepted"
    nowhere = S3Connector(fake.target(endpoint="https://localhost:9"), connect_seconds=2)
    failing = [(fake.connector(), ask(f"get exports/status/{k.split('/')[-1]}")) for k in STATUSES]
    failing += [
        (fake.connector(), ask("get exports/missing.csv")),
        (fake.connector(), ask("get exports/bad.csv")),
        (fake.connector(), ask("list exports/doctype/")),
        (fake.connector(secret_access_key=SECRET + "x"), ask("get exports/orders.csv")),
        (fake.connector(ca=fake.pki.other_ca), ask("get exports/orders.csv")),
        (nowhere, ask("get exports/orders.csv")),
        (fake.connector(), ask("get exports/slow.csv", timeout_ms=200)),
    ]
    shown: list[str] = []
    for connector, query in failing:
        with pytest.raises((QueryFailedError, UpstreamUnavailableError, TimeoutError)) as failed:
            await read(connector, query)
        shown.append("".join(traceback.format_exception(failed.value)) + repr(failed.value))
        shown.append(repr(connector))
    shown += [repr(fake.target()), str(fake.target()), caplog.text]
    hidden = [SECRET, *(a.rpartition("Signature=")[2] for a in fake.seen.authorizations)]
    assert len(fake.seen.authorizations) >= 10
    for text in shown:
        for secret in hidden:
            assert secret not in text
    assert "s3 read: op=list status=200 bytes=" in caplog.text
    assert "pages=3" in caplog.text


LISTED = "?list-type=2&prefix=exports%2Fx%20y&max-keys=101"


@pytest.mark.parametrize(
    ("bucket", "base", "listed"),
    [
        (
            BUCKET,
            "https://ssc-test-bucket.s3.eu-west-2.amazonaws.com/",
            "https://ssc-test-bucket.s3.eu-west-2.amazonaws.com/" + LISTED,
        ),
        (
            "ssc.test.bucket",
            "https://s3.eu-west-2.amazonaws.com/ssc.test.bucket/",
            "https://s3.eu-west-2.amazonaws.com/ssc.test.bucket" + LISTED,
        ),
    ],
)
async def test_aws_is_addressed_virtual_hosted_or_path_style_for_a_dotted_bucket(
    bucket: str, base: str, listed: str
) -> None:
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        if request.url.path.endswith(".csv"):
            return httpx2.Response(200, content=b"a\n1\n")
        return httpx2.Response(200, content=list_xml([], prefix="", token=None, next_token=None))

    target = S3Target.model_validate(
        {
            "bucket": BUCKET,
            "region": REGION,
            "prefix": PREFIX,
            "access_key_id": KEY_ID,
            "secret_access_key": SECRET,
        }
        | {"bucket": bucket}
    )
    connector = S3Connector(target, transport=httpx2.MockTransport(answer))
    await read(connector, ask("get exports/orders.csv", tag="a(b)\nc"))
    await read(connector, ask("list exports/x y", max_rows=100))
    get_request, list_request = sent
    assert str(get_request.url) == base + "exports/orders.csv"
    assert str(list_request.url) == listed
    assert get_request.headers["user-agent"] == "ssc-datagw (abc)"
    assert get_request.headers["x-amz-content-sha256"] == EMPTY_SHA256
    assert (
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date,"
        in get_request.headers["authorization"]
    )


AWS_SUITE = {
    "get-vanilla": (
        "https://example.amazonaws.com/",
        "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
    ),
    "get-vanilla-query-order-key-case": (
        "https://example.amazonaws.com/?Param2=value2&Param1=value1",
        "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500",
    ),
}
"""Two vectors of the AWS Signature Version 4 test suite, as botocore's copy of it holds them
(``tests/unit/auth/aws4_testsuite``): key ``AKIDEXAMPLE``, secret
``wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY``, 2015-08-30T12:36:00Z, ``us-east-1``, service
``service``, Host ``example.amazonaws.com``, SignedHeaders ``host;x-amz-date``."""


@pytest.mark.parametrize("case", sorted(AWS_SUITE))
def test_sign_reproduces_the_aws_test_suite(case: str) -> None:
    url, signature = AWS_SUITE[case]
    headers = sign(
        "GET",
        url,
        {},
        key_id="AKIDEXAMPLE",
        secret="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        now=datetime(2015, 8, 30, 12, 36, tzinfo=UTC),
        service="service",
    )
    assert headers["x-amz-date"] == "20150830T123600Z"
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        f"SignedHeaders=host;x-amz-date, Signature={signature}"
    )


def test_sign_encodes_each_path_segment_once() -> None:
    now = datetime(2015, 8, 30, 12, 36, tzinfo=UTC)
    common: dict[str, Any] = {"key_id": "K", "secret": "s", "region": "us-east-1", "now": now}
    raw = sign("GET", "https://h/a/b%20c/%C3%BC", {}, **common)
    again = sign("GET", "https://h/a/b c/ü", {}, **common)
    assert raw["Authorization"] == again["Authorization"]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("list exports/", ("list", "exports/")),
        ("LIST exports/many/", ("list", "exports/many/")),
        ("List exports/a b", ("list", "exports/a b")),
        ("get exports/orders.csv", ("get", "exports/orders.csv")),
        ("Get exports/x/ORDERS.CSV", ("get", "exports/x/ORDERS.CSV")),
        ("get exports/e.ndjson", ("get", "exports/e.ndjson")),
        ("get exports/e.jsonl", ("get", "exports/e.jsonl")),
        ("get exports/a..b.json", ("get", "exports/a..b.json")),
        ("get exports/" + "a" * 1012 + ".csv", ("get", "exports/" + "a" * 1012 + ".csv")),
    ],
)
def test_the_grammar_admits(sql: str, expected: tuple[str, str]) -> None:
    assert s3_request(sql, PREFIX) == expected


def test_a_bare_or_empty_list_is_the_empty_prefix() -> None:
    assert s3_request("list", "") == ("list", "")
    assert s3_request("list ", "") == ("list", "")
    assert s3_request("LIST", "") == ("list", "")
    with pytest.raises(QueryRefusedError):
        s3_request("list", PREFIX)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "get",
        "get ",
        "SELECT * FROM x",
        "put exports/x.csv",
        "delete exports/x.csv",
        "head exports/x.csv",
        "listexports/",
        "list\texports/",
        "get ../etc/passwd",
        "get exports/../other/x.csv",
        "get exports/./orders.csv",
        "list exports/..",
        "get /exports/orders.csv",
        "get other/x.csv",
        "list other/",
        "list export",
        "get exports/a\nb.csv",
        "get exports/a\x00.csv",
        "get exports/a\x7f.csv",
        "get exports/\ud800.csv",
        "get exports/" + "a" * 1013 + ".csv",
        "get exports/" + "é" * 507 + ".csv",
    ],
)
def test_the_grammar_refuses(sql: str) -> None:
    with pytest.raises(QueryRefusedError, match="^the query is not list <prefix> or get <key>"):
        s3_request(sql, PREFIX)


@pytest.mark.parametrize("key", ["exports/a.txt", "exports/a", "exports/", "exports/a.csv.gz"])
def test_only_four_extensions_are_read(key: str) -> None:
    with pytest.raises(QueryRefusedError, match=r"^only \.csv, \.json, \.jsonl and \.ndjson"):
        s3_request(f"get {key}", PREFIX)


async def test_params_are_refused_before_the_source() -> None:
    target = S3Target.model_validate(
        {"bucket": BUCKET, "region": REGION, "access_key_id": KEY_ID, "secret_access_key": SECRET}
    )
    with pytest.raises(QueryRefusedError, match="^an S3 read takes no parameters$"):
        await read(S3Connector(target), ask("get exports/orders.csv", 1))


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("true", True),
        ("false", False),
        ("0", 0),
        ("-12", -12),
        (str(2**63 - 1), 2**63 - 1),
        (str(2**63), str(2**63)),
        ("1" * 5000, "1" * 5000),
        ("2.50", 2.5),
        ("-1e3", -1000.0),
        ("1E-2", 0.01),
        ("1e999", "1e999"),
        ("007", "007"),
        ("+1", "+1"),
        (" 1", " 1"),
        ("1_000", "1_000"),
        ("NaN", "NaN"),
        ("inf", "inf"),
        ("True", "True"),
        (".5", ".5"),
        ("1.", "1."),
        ("١٢", "١٢"),
        ("x", "x"),
        ("", ""),
    ],
)
def test_a_csv_cell_is_inferred_strictly(text: str, value: object) -> None:
    got = csv_cell(text)
    assert got == value
    assert type(got) is type(value)


def test_list_page_reads_s3_xml_and_its_token() -> None:
    body = list_xml(
        ["exports/many/k00.csv"], prefix="exports/many/", token=None, next_token="t/1+="
    )
    rows, token = list_page(body)
    assert token == "t/1+="
    assert rows[0][0] == "exports/many/k00.csv"
    assert rows[0][2] == datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    for bad in (
        b"<Error/>",
        b"<ListBucketResult><IsTruncated>true</IsTruncated></ListBucketResult>",
        b"<ListBucketResult><Contents><Key>k</Key><Size>x</Size>"
        b"<LastModified>2026-01-01T00:00:00Z</LastModified></Contents></ListBucketResult>",
        b"<ListBucketResult><Contents><Key>k</Key><Size>1</Size>"
        b"<LastModified>2026-01-01T00:00:00</LastModified></Contents></ListBucketResult>",
    ):
        with pytest.raises(QueryFailedError) as corrupt:
            list_page(bad)
        assert corrupt.value.sqlstate == "22P02"


def test_an_unknown_error_code_is_not_named() -> None:
    failed = s3_failure(403, "Secretive<Code>")
    assert isinstance(failed, QueryFailedError)
    assert str(failed) == "the source answered 403"
    assert s3_failure(204, None) is None
    other = s3_failure(302, None)
    assert isinstance(other, QueryFailedError)
    assert other.sqlstate is None


def test_the_user_agent_keeps_the_tag_printable_and_short() -> None:
    assert user_agent("ssc:a:b:c") == "ssc-datagw (ssc:a:b:c)"
    assert user_agent("x(y)\r\nz\x00é") == "ssc-datagw (xyz)"
    assert user_agent("t" * 200) == f"ssc-datagw ({'t' * 128})"


def _target(**update: Any) -> S3Target:
    return S3Target.model_validate(
        {
            "bucket": BUCKET,
            "region": REGION,
            "access_key_id": KEY_ID,
            "secret_access_key": SECRET,
        }
        | update
    )


@pytest.mark.parametrize(
    "update",
    [
        {"bucket": "UPPER"},
        {"bucket": "ab"},
        {"bucket": "-bucket"},
        {"bucket": "a" * 64},
        {"region": "eu-west"},
        {"region": "EU-WEST-2"},
        {"prefix": "a\nb"},
        {"prefix": "a" * 513},
        {"access_key_id": "short"},
        {"access_key_id": "fakekeyid0000001"},
        {"endpoint": "http://localhost:9000"},
        {"endpoint": "https://localhost:9000/path"},
        {"endpoint": "https://user@localhost"},
        {"ca": ""},
        {"kind": "gcs"},
        {"token": "x"},
    ],
)
def test_the_target_refuses_and_hides_the_secret(update: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as refused:
        _target(**update)
    assert SECRET not in str(refused.value)
    assert SECRET not in repr(refused.value)


def test_the_target_admits_the_control_planes_address() -> None:
    target = _target(bucket="my.bucket-1", region="ap-southeast-2", prefix="")
    assert (target.kind, target.prefix, target.endpoint) == ("s3", "", None)
    assert _target(endpoint="https://minio.internal:9000").endpoint == "https://minio.internal:9000"
    assert SECRET not in repr(target)
    assert SECRET not in str(target)


def test_the_policy_is_list_and_read_under_the_prefix() -> None:
    policy = json.loads(files("ssc_datagw").joinpath("s3_policy.json").read_text())
    statements = {s["Action"]: s for s in policy["Statement"]}
    assert set(statements) == {"s3:ListBucket", "s3:GetObject"}
    assert statements["s3:ListBucket"]["Resource"] == "arn:aws:s3:::<bucket>"
    assert statements["s3:ListBucket"]["Condition"] == {"StringLike": {"s3:prefix": "<prefix>*"}}
    assert statements["s3:GetObject"]["Resource"] == "arn:aws:s3:::<bucket>/<prefix>*"
    assert all(s["Effect"] == "Allow" for s in policy["Statement"])
