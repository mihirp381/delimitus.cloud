"""The REST connector: one GET under a fixed https base, JSON records as rows (GA-5 B2).

A connection names a ``base_url`` (https, its host fixed) and, optionally, a token sent in one
header. A query's ``sql`` is the request: a path, with an optional query string, appended to
the base. :func:`request_path` refuses anything else before a byte leaves, so a read can neither
leave the host nor climb out of the base path.

Each read is one GET with its own client, over TLS checked by :func:`ssc_datagw.tls.tls_context`,
following no redirect. Sending it and waiting for the answer's headers runs under the shared
warm-up retry (:func:`ssc_datagw.warmup.connect_with_warmup`); the body is then read up to
:data:`BODY_BYTES` and parsed as JSON, and the whole read, connect included, ends at the query's
``timeout_ms``. :func:`get` is that GET, which the Google Sheets connector shares. The
records sit at the target's ``items`` path (the body itself without one): an array is one record
per element, an object one record. The first record's keys name the columns; a record that is
not an object is one column, ``value``.

A REST source has no schema to discover: :meth:`RestConnector.describe` answers no tables and
sends nothing (GA-5.8).

The token is a ``SecretStr`` and travels only in its header: every error names a status or an
exception class, never the URL's query, a header or the body.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Final, Literal, cast
from urllib.parse import unquote

import httpx2
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ssc_datagw.connectors import (
    Column,
    JsonValue,
    Query,
    QueryFailedError,
    QueryRefusedError,
    Table,
    UpstreamUnavailableError,
)
from ssc_datagw.tls import tls_context
from ssc_datagw.warmup import CONNECT_SECONDS, Warmup, connect_with_warmup

log = logging.getLogger(__name__)

BASE_URL: Final = r"^https://[A-Za-z0-9.-]{1,255}(:[0-9]{1,5})?(/[^\s?#]*)?$"
"""``ssc_contracts.connections.RestAddress``'s pattern with an optional port: the gateway target
may carry one, the control plane's address does not yet."""
URL_CHARS: Final = 2000
PATH_CHARS: Final = 2000
TOKEN_CHARS: Final = 4096
BODY_BYTES: Final = 32 * 2**20
TYPE_SAMPLE: Final = 100
"""How many records decide a column's type: the first non-null value among them."""
USER_AGENT: Final = "ssc-datagw"
VALUE: Final = "value"
"""The one column of records that are not objects."""
RESERVED_HEADERS: Final = frozenset(
    {
        "host",
        "accept",
        "user-agent",
        "x-ssc-query",
        "content-length",
        "transfer-encoding",
        "connection",
        "cookie",
    }
)
"""Headers the connector sets itself or that frame the request: never the credential's."""
NOT_A_GET: Final = "the request is not a GET path"
JSON_TYPES: Final[Sequence[tuple[type, str, str]]] = (
    (bool, "boolean", "boolean"),
    (int, "integer", "number"),
    (float, "float", "number"),
    (str, "string", "string"),
    (list, "json", "array"),
    (dict, "json", "object"),
)
"""A JSON value's Python type, its portable type and its JSON type name. ``bool`` comes before
``int``, which it is a subclass of."""


def _visible(text: str) -> bool:
    """Printable ASCII without the space: no whitespace, no control character, no non-ASCII."""
    return all("!" <= c <= "~" for c in text)


class RestTarget(BaseModel):
    """Where reads go and how they prove who asks. ``token`` is sent in ``header`` as
    ``<scheme> <token>``, or bare when ``scheme`` is empty. ``items`` is a dotted path of object
    keys to the records in the answer, the body itself when left out. ``ca`` is as for
    Postgres: with it the chain must lead to it and the host name is not checked; without it the
    system trust store and the host name decide."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: Literal["rest"] = "rest"
    base_url: str = Field(max_length=URL_CHARS, pattern=BASE_URL)
    token: SecretStr | None = None
    header: str = Field(default="Authorization", pattern=r"^[A-Za-z0-9-]{1,64}$")
    scheme: str = Field(default="Bearer", pattern=r"^[A-Za-z0-9-]{0,32}$")
    items: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,200}$")
    ca: str | None = Field(default=None, min_length=1)

    @field_validator("header")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value.lower() in RESERVED_HEADERS:
            raise ValueError("the header is one the connector sets itself")
        return value

    @field_validator("token")
    @classmethod
    def _token_is_visible_ascii(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if not 1 <= len(raw) <= TOKEN_CHARS or not _visible(raw):
            raise ValueError(f"the token must be 1 to {TOKEN_CHARS} visible ASCII characters")
        return value


def request_path(sql: str) -> str:
    """``sql`` as the path of a GET under the base, or :class:`QueryRefusedError`. It starts
    with one ``/``, is visible ASCII without a backslash or ``#``, and its path part has no
    ``..`` segment, even percent-encoded."""
    path = sql.partition("?")[0]
    if (
        not sql.startswith("/")
        or sql.startswith("//")
        or len(sql) > PATH_CHARS
        or not _visible(sql)
        or "\\" in sql
        or "#" in sql
        or ".." in unquote(path).split("/")
    ):
        raise QueryRefusedError(NOT_A_GET)
    return sql


def records_at(body: JsonValue, items: str | None) -> list[JsonValue]:
    """The records at the dotted ``items`` path of ``body``."""
    found = body
    for key in items.split(".") if items else ():
        if not isinstance(found, dict) or key not in found:
            raise QueryFailedError("items path not found", sqlstate="42P01")
        found = found[key]
    if isinstance(found, list):
        return found
    if isinstance(found, dict):
        return [found]
    raise QueryFailedError("the items are not an array or object", sqlstate="22P02")


def _by_object(records: Sequence[JsonValue]) -> bool:
    """Whether the records are read by key: the first one decides."""
    return bool(records) and isinstance(records[0], dict)


def _column(name: str, values: Sequence[JsonValue]) -> Column:
    first = next((v for v in values if v is not None), None)
    for kind, portable, db_type in JSON_TYPES:
        if isinstance(first, kind):
            return Column(name, portable, db_type)
    return Column(name, "string", "null")


def columns_for(records: Sequence[JsonValue]) -> list[Column]:
    """The first record's keys in order (``value`` for a record that is not an object), each
    typed by its first non-null value among the first :data:`TYPE_SAMPLE` records."""
    if not records:
        return []
    sample = records[:TYPE_SAMPLE]
    if not _by_object(records):
        return [_column(VALUE, sample)]
    first = cast("dict[str, JsonValue]", records[0])
    return [_column(name, [r.get(name) for r in sample if isinstance(r, dict)]) for name in first]


def rows_for(
    records: Sequence[JsonValue], columns: Sequence[Column], limit: int
) -> list[list[JsonValue]]:
    """At most ``limit`` rows in column order. A missing key is ``None`` and a key that is not a
    column is dropped; a record that is not an object, among objects, is a row of ``None``."""
    if not _by_object(records):
        return [[record] for record in records[:limit]]
    names = [c.name for c in columns]
    return [
        [r.get(n) for n in names] if isinstance(r, dict) else [None] * len(names)
        for r in records[:limit]
    ]


def status_failure(status: int) -> Exception | None:
    """The connector's error for an answer's status, or ``None`` for a 2xx."""
    if 200 <= status < 300:
        return None
    if status in (401, 403):
        return QueryFailedError("the source refused the credential", sqlstate="28000")
    if status == 404:
        return QueryFailedError("no such path", sqlstate="42P01")
    if status == 429:
        return UpstreamUnavailableError("the source is rate limiting")
    if status >= 500:
        return UpstreamUnavailableError(f"the source answered {status}")
    return QueryFailedError(f"the source answered {status}", sqlstate=None)


async def read_body(response: httpx2.Response) -> bytes:
    """The answer's body, decoded, at most :data:`BODY_BYTES`."""
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > BODY_BYTES:
                raise QueryFailedError("the body is larger than 32 MiB", sqlstate=None)
    except httpx2.TimeoutException:
        raise TimeoutError("the source stopped answering") from None
    except httpx2.DecodingError:
        raise QueryFailedError("the body could not be decoded", sqlstate="22P02") from None
    except httpx2.TransportError as exc:
        raise UpstreamUnavailableError(f"the read failed: {type(exc).__name__}") from None
    return bytes(body)


class _ReadTimedOutError(Exception):
    """The source took the request and did not answer in time: not a connect time-out, which
    the warm-up retry would try again."""


async def get(  # noqa: PLR0913  (the request and how it is timed)
    client: httpx2.AsyncClient,
    url: httpx2.URL,
    headers: Mapping[str, str],
    *,
    seconds: float,
    connect_seconds: float,
    warmup: Warmup,
    failure: Callable[[int], Exception | None] = status_failure,
    method: str = "GET",
    content: bytes | None = None,
) -> tuple[int, bytes]:
    """One GET of ``url``: the answer's status and body. Sending it and waiting for the
    headers runs under the warm-up retry; ``failure`` maps the status to the connector's error
    (``None`` reads on), and the body is read by :func:`read_body`. Every read waits at most
    ``seconds``; a connect or TLS failure is ``UpstreamUnavailableError``. ``method`` and
    ``content`` send another request the same way (the BigQuery connector's POSTs)."""

    async def connect_once(connect: float) -> httpx2.Response:
        timeout = httpx2.Timeout(seconds, connect=connect)
        request = client.build_request(
            method, url, headers=headers, content=content, timeout=timeout
        )
        try:
            return await client.send(request, stream=True)
        except httpx2.ConnectTimeout:
            raise TimeoutError("connect") from None
        except httpx2.TimeoutException:
            raise _ReadTimedOutError from None
        except httpx2.TransportError as exc:
            raise UpstreamUnavailableError(f"cannot connect: {type(exc).__name__}") from None

    try:
        response = await connect_with_warmup(
            connect_once, connect_seconds=connect_seconds, warmup=warmup
        )
    except _ReadTimedOutError:
        raise TimeoutError("the source did not answer in time") from None
    try:
        refused = failure(response.status_code)
        if refused is not None:
            raise refused
        body = await read_body(response)
    finally:
        await response.aclose()
    return response.status_code, body


class _Cursor:
    def __init__(self, columns: list[Column], rows: list[list[JsonValue]]) -> None:
        self._columns = columns
        self._rows = rows

    @property
    def columns(self) -> Sequence[Column]:
        return self._columns

    async def rows(self) -> AsyncIterator[Sequence[object]]:
        for row in self._rows:
            yield row


class RestConnector:
    """A :class:`ssc_datagw.connectors.Connector` for one REST source. ``transport`` replaces
    the network (tests)."""

    def __init__(
        self,
        target: RestTarget,
        *,
        connect_seconds: float = CONNECT_SECONDS,
        warmup: Warmup | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._target = target
        self._base = target.base_url.rstrip("/")
        self._tls = tls_context(target.ca)
        self._connect_seconds = connect_seconds
        self._warmup = warmup or Warmup()
        self._transport = transport

    def _headers(self, tag: str) -> dict[str, str]:
        t = self._target
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT, "X-SSC-Query": tag}
        if t.token is not None:
            token = t.token.get_secret_value()
            headers[t.header] = f"{t.scheme} {token}" if t.scheme else token
        return headers

    @asynccontextmanager
    async def open(self, query: Query) -> AsyncGenerator[_Cursor]:
        path = request_path(query.sql)
        if query.params:
            raise QueryRefusedError("a REST read takes no parameters")
        try:
            url = httpx2.URL(self._base + path)
        except httpx2.InvalidURL:
            raise QueryRefusedError(NOT_A_GET) from None
        seconds = max(1, query.timeout_ms) / 1000
        client = httpx2.AsyncClient(
            verify=self._tls,
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
        )
        try:
            async with asyncio.timeout(seconds):
                status, body = await get(
                    client,
                    url,
                    self._headers(query.tag),
                    seconds=seconds,
                    connect_seconds=self._connect_seconds,
                    warmup=self._warmup,
                )
        finally:
            await client.aclose()
        log.info("rest read: status=%s bytes=%s", status, len(body))
        try:
            parsed = cast("JsonValue", json.loads(body))
        except ValueError, RecursionError:
            raise QueryFailedError("the body is not JSON", sqlstate="22P02") from None
        records = records_at(parsed, self._target.items)
        columns = columns_for(records)
        yield _Cursor(columns, rows_for(records, columns, query.max_rows + 1))

    async def describe(self, *, schemas: Sequence[str] | None, timeout_ms: int) -> list[Table]:
        """No tables: a REST source names none."""
        del schemas, timeout_ms
        return []
