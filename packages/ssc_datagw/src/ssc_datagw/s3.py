"""The Amazon S3 connector: SigV4 by hand, lists and object reads under one prefix (GA-5 B7).

A connection names one bucket, its region and a prefix, and holds an IAM user's access key
(``s3_policy.json`` is the policy the customer attaches: list and read under the prefix, nothing
else). A query's ``sql`` is one of two requests (:func:`s3_request`): ``list <prefix>`` lists the
keys under a prefix, ``get <key>`` reads one ``.csv``, ``.json``, ``.jsonl`` or ``.ndjson``
object as records. Anything else, and any key outside the connection's prefix, is refused before
a byte leaves.

Each request is signed with AWS Signature Version 4 (:func:`sign`, ``hmac`` and ``hashlib``) and
sent through :func:`ssc_datagw.rest.get`: TLS checked by :func:`ssc_datagw.tls.tls_context`, no
redirect, the warm-up retry and the 32 MiB cap. A list follows ``continuation-token`` until it
holds ``max_rows`` plus one keys; the whole read, every page included, ends at ``timeout_ms``.
Without ``endpoint`` the bucket is addressed virtual-hosted on AWS (path-style when its name holds
a ``.``, which the wildcard certificate does not cover); with it, path-style on that S3-compatible
store.

A description (:meth:`S3Connector.describe`, GA-5.8) lists the first 500 objects under the
prefix and names each readable one a table, by its full key, without columns.

The secret key is a ``SecretStr``; it, the signing key and each ``Authorization`` value are never
in a repr, an error or a log line: every error names a status and a known S3 error code, never a
body, a header or the URL.
"""

import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import math
import re
import xml.etree.ElementTree as ET  # noqa: S405  (expat's own limits; DOCTYPE refused)
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Final, Literal, cast
from urllib.parse import quote, unquote, urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ssc_datagw.connectors import (
    DESCRIBE_TAG,
    MAX_TABLES,
    Column,
    JsonValue,
    Query,
    QueryFailedError,
    QueryRefusedError,
    Table,
    UpstreamUnavailableError,
)
from ssc_datagw.gsheets import Cell, table
from ssc_datagw.rest import TYPE_SAMPLE, USER_AGENT, columns_for, get, records_at, rows_for
from ssc_datagw.tls import tls_context
from ssc_datagw.warmup import CONNECT_SECONDS, Warmup

log = logging.getLogger(__name__)

BUCKET: Final = r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
REGION: Final = r"^[a-z]{2}(-[a-z]+)+-\d$"
PREFIX: Final = r"^[^\x00-\x1f]{0,512}$"
"""``ssc_contracts.connections.S3Address``'s three patterns."""
ACCESS_KEY_ID: Final = r"^[A-Z0-9]{16,128}$"
ENDPOINT: Final = r"^https://[A-Za-z0-9.-]{1,255}(:[0-9]{1,5})?$"
ALGORITHM: Final = "AWS4-HMAC-SHA256"
EMPTY_SHA256: Final = hashlib.sha256(b"").hexdigest()
ARGUMENT_BYTES: Final = 1024
PAGE_KEYS: Final = 1000
TAG_CHARS: Final = 128
INT64: Final = 2**63
EXTENSIONS: Final = frozenset({"csv", "json", "jsonl", "ndjson"})
NOT_A_READ: Final = "the query is not list <prefix> or get <key> under the connection's prefix"
NOT_READABLE: Final = "only .csv, .json, .jsonl and .ndjson objects are read"
OTHER_REGION: Final = "the bucket is in another region"
NOT_A_LIST: Final = "the body is not a ListObjectsV2 result"
KNOWN_CODES: Final = frozenset(
    {
        "AccessDenied",
        "AuthorizationHeaderMalformed",
        "ExpiredToken",
        "InternalError",
        "InvalidAccessKeyId",
        "InvalidArgument",
        "InvalidBucketName",
        "InvalidObjectState",
        "InvalidRequest",
        "InvalidToken",
        "NoSuchBucket",
        "NoSuchKey",
        "PermanentRedirect",
        "RequestTimeTooSkewed",
        "ServiceUnavailable",
        "SignatureDoesNotMatch",
        "SlowDown",
        "TemporaryRedirect",
    }
)
"""S3 error codes an error message may name; any other is left out (the body is the source's)."""
LIST_COLUMNS: Final = (
    Column("key", "string", "string"),
    Column("size", "integer", "integer"),
    Column("last_modified", "timestamp", "timestamp"),
    Column("etag", "string", "string"),
)

_REQUEST = re.compile(r"(list|get)(?: (.*))?", re.IGNORECASE | re.DOTALL)
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


class S3Target(BaseModel):
    """One bucket, its region, the prefix reads stay under, and the access key that reads it.
    ``endpoint`` is an S3-compatible store's ``https://host[:port]``, addressed path-style; left
    out, AWS. ``ca`` is as for Postgres: with it the chain must lead to it and the host name is
    not checked; without it the system trust store and the host name decide."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: Literal["s3"] = "s3"
    bucket: str = Field(pattern=BUCKET)
    region: str = Field(pattern=REGION)
    prefix: str = Field(default="", pattern=PREFIX)
    access_key_id: str = Field(pattern=ACCESS_KEY_ID)
    secret_access_key: SecretStr
    endpoint: str | None = Field(default=None, pattern=ENDPOINT)
    ca: str | None = Field(default=None, min_length=1)


def _encoded(text: str) -> str:
    """``text`` URI-encoded as SigV4 wants: every byte but ``A-Za-z0-9-._~`` as ``%XX``."""
    return quote(text, safe="")


def _canonical_path(path: str) -> str:
    """Each segment decoded, then encoded once (S3 style: never twice)."""
    return "/".join(_encoded(unquote(segment)) for segment in (path or "/").split("/"))


def _canonical_query(query: str) -> str:
    pairs = sorted(
        (_encoded(unquote(name)), _encoded(unquote(value)))
        for name, _, value in (part.partition("=") for part in query.split("&") if part)
    )
    return "&".join(f"{name}={value}" for name, value in pairs)


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def sign(  # noqa: PLR0913  (the request and who signs it)
    method: str,
    url: str,
    headers: Mapping[str, str],
    *,
    key_id: str,
    secret: str,
    region: str,
    now: datetime,
    service: str = "s3",
) -> dict[str, str]:
    """``headers`` with ``x-amz-date`` and an AWS Signature Version 4 ``Authorization`` added.
    Signed are ``host`` (the URL's), ``x-amz-date`` and every ``x-amz-*`` header given; the
    payload hash is the given ``x-amz-content-sha256``, else the empty body's. Pure: the
    signing key is derived here and kept nowhere."""
    parts = urlsplit(url)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    signed_headers = {**headers, "x-amz-date": stamp}
    lowered = {name.lower(): value for name, value in signed_headers.items()}
    covered = {"host": parts.netloc} | {
        name: value for name, value in lowered.items() if name.startswith("x-amz-")
    }
    names = sorted(covered)
    canonical_headers = "".join(f"{name}:{' '.join(covered[name].split())}\n" for name in names)
    signed_names = ";".join(names)
    canonical = "\n".join(
        (
            method,
            _canonical_path(parts.path),
            _canonical_query(parts.query),
            canonical_headers,
            signed_names,
            lowered.get("x-amz-content-sha256", EMPTY_SHA256),
        )
    )
    scope = f"{stamp[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join((ALGORITHM, stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()))
    key = _hmac(f"AWS4{secret}".encode(), stamp[:8])
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    signed_headers["Authorization"] = (
        f"{ALGORITHM} Credential={key_id}/{scope}, SignedHeaders={signed_names}, "
        f"Signature={signature}"
    )
    return signed_headers


def s3_request(sql: str, prefix: str) -> tuple[Literal["list", "get"], str]:
    """``sql`` as ``("list", prefix)`` or ``("get", key)``, or :class:`QueryRefusedError`. The
    keyword is any case and one space parts it from the argument (a bare ``list`` lists the
    empty prefix). The argument starts with the connection's ``prefix``, is at most 1024 bytes
    of UTF-8, does not start with ``/`` and has no control character and no ``.`` or ``..``
    segment; a key ends in a readable extension."""
    found = _REQUEST.fullmatch(sql)
    if found is None:
        raise QueryRefusedError(NOT_A_READ)
    op = cast("Literal['list', 'get']", found.group(1).lower())
    argument = found.group(2)
    if argument is None:
        if op != "list":
            raise QueryRefusedError(NOT_A_READ)
        argument = ""
    try:
        size = len(argument.encode())
    except UnicodeEncodeError:
        raise QueryRefusedError(NOT_A_READ) from None
    if (
        size > ARGUMENT_BYTES
        or argument.startswith("/")
        or any(c < " " or c == "\x7f" for c in argument)
        or any(segment in {".", ".."} for segment in argument.split("/"))
        or not argument.startswith(prefix)
        or (op == "get" and not argument)
    ):
        raise QueryRefusedError(NOT_A_READ)
    if op == "get" and _extension(argument) not in EXTENSIONS:
        raise QueryRefusedError(NOT_READABLE)
    return op, argument


def _extension(key: str) -> str:
    name = key.rpartition("/")[2]
    return name.rpartition(".")[2].lower() if "." in name else ""


def readable(key: str) -> bool:
    """Whether ``get`` reads the object: a ``.csv``, ``.json``, ``.jsonl`` or ``.ndjson``."""
    return _extension(key) in EXTENSIONS


def user_agent(tag: str) -> str:
    """``ssc-datagw (<tag>)``: the tag kept to printable ASCII without parentheses, at most
    :data:`TAG_CHARS` characters. CloudTrail's data events record it."""
    kept = "".join(c for c in tag if " " <= c <= "~" and c not in "()")[:TAG_CHARS]
    return f"{USER_AGENT} ({kept})"


def s3_failure(status: int, code: str | None) -> Exception | None:
    """The connector's error for an answer's status and S3 error ``code``; ``None`` for a 2xx."""
    if 200 <= status < 300:
        return None
    shown = f"{status} {code}" if code in KNOWN_CODES else str(status)
    if status == 301 or code in {"PermanentRedirect", "AuthorizationHeaderMalformed"}:
        return UpstreamUnavailableError(OTHER_REGION)
    if status == 403:
        return QueryFailedError(f"the source answered {shown}", sqlstate="42501")
    if status == 404:
        return QueryFailedError(f"the source answered {shown}", sqlstate="42P01")
    if status == 429 or status >= 500:
        return UpstreamUnavailableError(f"the source answered {shown}")
    return QueryFailedError(f"the source answered {shown}", sqlstate=None)


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _xml(body: bytes) -> ET.Element:
    """The parsed body; a ``DOCTYPE`` (S3 never sends one) or a body expat refuses is 22P02."""
    if b"<!DOCTYPE" in body:
        raise QueryFailedError("the body is not S3's XML", sqlstate="22P02")
    try:
        return ET.fromstring(body)  # noqa: S314  (expat's amplification limits; no DOCTYPE)
    except ET.ParseError:
        raise QueryFailedError("the body is not S3's XML", sqlstate="22P02") from None


def error_code(body: bytes) -> str | None:
    """The ``Code`` of an S3 error body, or ``None`` when it is not one."""
    try:
        root = _xml(body)
    except QueryFailedError:
        return None
    if _local(root.tag) != "Error":
        return None
    return next((child.text for child in root if _local(child.tag) == "Code"), None)


def _child(element: ET.Element, name: str) -> str | None:
    return next((c.text or "" for c in element if _local(c.tag) == name), None)


def _unquoted(etag: str | None) -> str | None:
    if etag is not None and len(etag) >= 2 and etag[0] == etag[-1] == '"':  # noqa: PLR2004
        return etag[1:-1]
    return etag


def list_page(body: bytes) -> tuple[list[list[object]], str | None]:
    """A ListObjectsV2 page's rows (key, size, last modified, etag) and its continuation token,
    ``None`` on the last page."""
    root = _xml(body)
    if _local(root.tag) != "ListBucketResult":
        raise QueryFailedError(NOT_A_LIST, sqlstate="22P02")
    rows: list[list[object]] = []
    for contents in (c for c in root if _local(c.tag) == "Contents"):
        key, size, modified = (_child(contents, n) for n in ("Key", "Size", "LastModified"))
        if key is None or size is None or modified is None or not _INTEGER.fullmatch(size):
            raise QueryFailedError(NOT_A_LIST, sqlstate="22P02")
        try:
            at = datetime.fromisoformat(modified)
        except ValueError:
            raise QueryFailedError(NOT_A_LIST, sqlstate="22P02") from None
        if at.tzinfo is None:
            raise QueryFailedError(NOT_A_LIST, sqlstate="22P02")
        rows.append([key, int(size), at.astimezone(UTC), _unquoted(_child(contents, "ETag"))])
    if _child(root, "IsTruncated") != "true":
        return rows, None
    token = _child(root, "NextContinuationToken")
    if not token:
        raise QueryFailedError(NOT_A_LIST, sqlstate="22P02")
    return rows, token


def csv_cell(text: str) -> Cell:
    """``true``/``false`` a boolean, a JSON-style integer within 64 bits an ``int``, a JSON-style
    number with a fraction or exponent a finite ``float``; anything else the text itself."""
    if text in {"true", "false"}:
        return text == "true"
    if _INTEGER.fullmatch(text):
        value = int(text) if len(text) <= 20 else INT64  # noqa: PLR2004  (int64's digits)
        return value if -INT64 <= value < INT64 else text
    if _NUMBER.fullmatch(text):
        number = float(text)
        return number if math.isfinite(number) else text
    return text


def _text(body: bytes) -> str:
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise QueryFailedError("the body is not UTF-8", sqlstate="22P02") from None


def csv_table(body: bytes, limit: int) -> tuple[list[Column], list[list[Cell | None]]]:
    """The first row names the columns (as for Google Sheets), each later row is a record; a
    blank line is no row, an empty field is ``None``."""
    reader = csv.reader(io.StringIO(_text(body), newline=""), strict=True)
    values: list[list[Cell]] = []
    try:
        for row in reader:
            if not row:
                continue
            values.append(list(row) if not values else [csv_cell(c) for c in row])
            if len(values) > max(limit, TYPE_SAMPLE):
                break
    except csv.Error:
        raise QueryFailedError("the body is not CSV", sqlstate="22P02") from None
    return table(values, limit)


def json_records(body: bytes, extension: str, limit: int) -> list[JsonValue]:
    """``.json``: an array is the records, an object one record. ``.jsonl``/``.ndjson``: one
    record per non-empty line."""
    try:
        if extension == "json":
            return records_at(cast("JsonValue", json.loads(body)), None)
        records: list[JsonValue] = []
        for line in _text(body).split("\n"):
            if line.strip():
                records.append(cast("JsonValue", json.loads(line)))
                if len(records) >= max(limit, TYPE_SAMPLE):
                    break
    except ValueError, RecursionError:
        raise QueryFailedError("the body is not JSON", sqlstate="22P02") from None
    return records


def object_table(key: str, body: bytes, limit: int) -> tuple[list[Column], list[list[object]]]:
    """The object's columns and at most ``limit`` rows, read by its extension."""
    extension = _extension(key)
    if extension == "csv":
        columns, cells = csv_table(body, limit)
        return columns, cast("list[list[object]]", cells)
    records = json_records(body, extension, limit)
    columns = columns_for(records)
    return columns, cast("list[list[object]]", rows_for(records, columns, limit))


def _read_on(_: int) -> None:
    """Every answer's body is read: an error's ``Code`` decides with its status."""
    return


class _Cursor:
    def __init__(self, columns: Sequence[Column], rows: list[list[object]]) -> None:
        self._columns = list(columns)
        self._rows = rows

    @property
    def columns(self) -> Sequence[Column]:
        return self._columns

    async def rows(self) -> AsyncIterator[Sequence[object]]:
        for row in self._rows:
            yield row


class S3Connector:
    """A :class:`ssc_datagw.connectors.Connector` for one bucket and prefix. ``transport``
    replaces the network and ``page_keys`` the list page size (tests)."""

    def __init__(  # noqa: PLR0913  (the target and its test seams)
        self,
        target: S3Target,
        *,
        connect_seconds: float = CONNECT_SECONDS,
        warmup: Warmup | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        page_keys: int = PAGE_KEYS,
    ) -> None:
        self._target = target
        self._tls = tls_context(target.ca)
        self._connect_seconds = connect_seconds
        self._warmup = warmup or Warmup()
        self._transport = transport
        self._page_keys = page_keys

    def _base(self) -> str:
        """The bucket's URL, without a trailing ``/``."""
        t = self._target
        if t.endpoint is not None:
            return f"{t.endpoint}/{t.bucket}"
        if "." in t.bucket:
            return f"https://s3.{t.region}.amazonaws.com/{t.bucket}"
        return f"https://{t.bucket}.s3.{t.region}.amazonaws.com"

    def object_url(self, key: str) -> str:
        return self._base() + "/" + "/".join(_encoded(s) for s in key.split("/"))

    def list_url(self, prefix: str, max_keys: int, token: str | None) -> str:
        base = self._base()
        root = base if urlsplit(base).path else base + "/"
        query = f"list-type=2&prefix={_encoded(prefix)}&max-keys={max_keys}"
        if token is not None:
            query += f"&continuation-token={_encoded(token)}"
        return f"{root}?{query}"

    async def _fetch(
        self, client: httpx2.AsyncClient, url: str, tag: str, seconds: float
    ) -> tuple[int, bytes]:
        t = self._target
        headers = sign(
            "GET",
            url,
            {"User-Agent": user_agent(tag), "x-amz-content-sha256": EMPTY_SHA256},
            key_id=t.access_key_id,
            secret=t.secret_access_key.get_secret_value(),
            region=t.region,
            now=datetime.now(UTC),
        )
        status, body = await get(
            client,
            httpx2.URL(url),
            headers,
            seconds=seconds,
            connect_seconds=self._connect_seconds,
            warmup=self._warmup,
            failure=_read_on,
        )
        refused = s3_failure(status, error_code(body) if status >= 300 else None)  # noqa: PLR2004
        if refused is not None:
            raise refused
        return status, body

    async def _list(
        self, client: httpx2.AsyncClient, prefix: str, query: Query, seconds: float
    ) -> list[list[object]]:
        limit = query.max_rows + 1
        rows: list[list[object]] = []
        token: str | None = None
        pages = size = 0
        status = 0
        while True:
            url = self.list_url(prefix, min(self._page_keys, limit - len(rows)), token)
            status, body = await self._fetch(client, url, query.tag, seconds)
            pages += 1
            size += len(body)
            page, token = list_page(body)
            rows += page[: limit - len(rows)]
            if len(rows) >= limit or token is None:
                break
        log.info("s3 read: op=list status=%s bytes=%s pages=%s", status, size, pages)
        return rows

    @asynccontextmanager
    async def open(self, query: Query) -> AsyncGenerator[_Cursor]:
        op, argument = s3_request(query.sql, self._target.prefix)
        if query.params:
            raise QueryRefusedError("an S3 read takes no parameters")
        seconds = max(1, query.timeout_ms) / 1000
        client = httpx2.AsyncClient(
            verify=self._tls,
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
        )
        try:
            async with asyncio.timeout(seconds):
                if op == "list":
                    rows = await self._list(client, argument, query, seconds)
                    columns: Sequence[Column] = LIST_COLUMNS
                else:
                    url = self.object_url(argument)
                    status, body = await self._fetch(client, url, query.tag, seconds)
                    log.info("s3 read: op=get status=%s bytes=%s pages=1", status, len(body))
                    columns, rows = object_table(argument, body, query.max_rows + 1)
        finally:
            await client.aclose()
        yield _Cursor(columns, rows)

    async def describe(self, *, schemas: Sequence[str] | None, timeout_ms: int) -> list[Table]:
        """The first :data:`~ssc_datagw.connectors.MAX_TABLES` objects under the prefix, each
        readable one a table without columns; ``schemas`` does not apply."""
        del schemas
        query = Query(
            sql="", params=(), max_rows=MAX_TABLES - 1, timeout_ms=timeout_ms, tag=DESCRIBE_TAG
        )
        seconds = max(1, timeout_ms) / 1000
        client = httpx2.AsyncClient(
            verify=self._tls,
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
        )
        try:
            async with asyncio.timeout(seconds):
                rows = await self._list(client, self._target.prefix, query, seconds)
        finally:
            await client.aclose()
        keys = [cast("str", row[0]) for row in rows]
        return [Table(key, []) for key in keys if readable(key)]
