"""The Airtable connector: a personal access token, paged record lists, formula filters (GA-5 B9).

A connection names one base and, optionally, the one table it may read, and holds a personal
access token the customer scoped to ``data.records:read`` on that base. A query's ``sql`` is one
list request (:func:`airtable_request`): ``<table>[ view <view>][ where <formula>]``, the
formula passed to Airtable as ``filterByFormula`` unchanged. Anything else, and any other table
than the connection's, is refused before a byte leaves.

Each page is one GET through :func:`ssc_datagw.rest.get`: TLS checked by
:func:`ssc_datagw.tls.tls_context`, no redirect, the warm-up retry and the 32 MiB cap. Pages
follow Airtable's ``offset`` until ``max_rows`` plus one records are read,
:data:`PAGE_PAUSE_SECONDS` apart (Airtable allows five requests a second per base); the whole
read ends at ``timeout_ms``.
A record is a row: its ``id``, its ``createdTime`` and one column per field, in the order fields
first appear (:func:`table_of`), typed as the REST connector types JSON.

A description (:meth:`AirtableConnector.describe`, GA-5.8) is one GET of the base's schema,
``/v0/meta/bases/{base}/tables``, which the token's ``schema.bases:read`` scope allows (without
it Airtable answers 403, 42501): one table per Airtable table (the connection's alone when it
names one), ``id`` and ``created_time`` then each field, typed by its Airtable type
(:data:`FIELD_TYPES`).

The token is a ``SecretStr`` and travels only in ``Authorization``: every error names a status
and Airtable's error type, never the body's message, a header or the URL.
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, cast
from urllib.parse import quote

import httpx2
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ssc_datagw.connectors import (
    DESCRIBE_TAG,
    MAX_COLUMNS,
    MAX_TABLES,
    Column,
    JsonValue,
    Query,
    QueryFailedError,
    QueryRefusedError,
    Table,
    UpstreamUnavailableError,
)
from ssc_datagw.gsheets import header_names
from ssc_datagw.rest import columns_for, get, rows_for
from ssc_datagw.s3 import user_agent
from ssc_datagw.tls import tls_context
from ssc_datagw.warmup import CONNECT_SECONDS, Warmup

log = logging.getLogger(__name__)

BASE_ID: Final = r"^app[A-Za-z0-9]{14}$"
TABLE: Final = r"^[^\x00-\x1f]{1,100}$"
"""``ssc_contracts.connections.AirtableAddress``'s two patterns."""
API_URL: Final = "https://api.airtable.com"
TOKEN_MIN: Final = 20
TOKEN_MAX: Final = 200
NAME_CHARS: Final = 100
FORMULA_CHARS: Final = 2000
PAGE_RECORDS: Final = 100
PAGE_PAUSE_SECONDS: Final = 0.2
"""The pause before each page after the first: Airtable allows 5 requests a second per base."""
VERBS: Final = frozenset(
    {
        "insert",
        "delete",
        "update",
        "select",
        "create",
        "drop",
        "alter",
        "replace",
        "upsert",
        "merge",
        "truncate",
    }
)
"""A table or view whose first word is one of these is refused: it reads as a statement. Such a
table is read by its ``tbl…`` id."""
SQLSTATES: Final = {401: "28000", 403: "42501", 404: "42P01", 422: "42601"}
ID: Final = "id"
CREATED_TIME: Final = "created_time"
NOT_A_READ: Final = "the query is not <table>[ view <view>][ where <formula>]"
OTHER_TABLE: Final = "the query's table is not the connection's table"
NOT_RECORDS: Final = "the body is not an Airtable record list"
NOT_TABLES: Final = "the body is not an Airtable table list"
FIELD_TYPES: Final = {
    **dict.fromkeys(
        (
            "singleLineText",
            "multilineText",
            "email",
            "url",
            "phoneNumber",
            "richText",
            "singleSelect",
            "barcode",
        ),
        "string",
    ),
    **dict.fromkeys(
        ("number", "currency", "percent", "duration", "rating", "count", "autoNumber"), "float"
    ),
    "checkbox": "boolean",
    "date": "date",
    **dict.fromkeys(("dateTime", "createdTime", "lastModifiedTime"), "timestamp"),
}
"""A field's portable type by its Airtable type; a ``number`` of precision 0 is ``integer``,
any other type ``json``."""

_REQUEST = re.compile(
    r"(?P<table>.+?)(?: view (?P<view>.+?))?(?: where (?P<formula>.+))?",
    re.IGNORECASE | re.DOTALL,
)
_ERROR_TYPE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class AirtableTarget(BaseModel):
    """One base, optionally the one table reads stay on, and the personal access token that
    reads it."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: Literal["airtable"] = "airtable"
    base_id: str = Field(pattern=BASE_ID)
    table: str | None = Field(default=None, pattern=TABLE)
    token: SecretStr

    @field_validator("token")
    @classmethod
    def _personal_access_token(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if (
            not raw.startswith("pat")
            or not TOKEN_MIN <= len(raw) <= TOKEN_MAX
            or not all("!" <= c <= "~" for c in raw)
        ):
            raise ValueError(
                f"the token must be a personal access token: {TOKEN_MIN} to {TOKEN_MAX} "
                "visible ASCII characters starting with pat"
            )
        return value


def _plain(text: str, chars: int) -> bool:
    """1 to ``chars`` characters of UTF-8 without a control character."""
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return 1 <= len(text) <= chars and not any(c < " " or c == "\x7f" for c in text)


def _name(text: str) -> bool:
    """A table or view: plain, no ``;``, no space at either end, no verb as its first word."""
    return (
        _plain(text, NAME_CHARS)
        and ";" not in text
        and text == text.strip(" ")
        and text.split(" ", 1)[0].lower() not in VERBS
    )


def airtable_request(sql: str, table: str | None) -> tuple[str, str | None, str | None]:
    """``sql`` as ``(table, view, formula)``, or :class:`QueryRefusedError`. The keywords are
    any case and single spaces part them; the first `` view `` and the first `` where `` split,
    and the formula is the rest, unchanged. With ``table`` the query's table must be it."""
    found = _REQUEST.fullmatch(sql)
    if found is None:
        raise QueryRefusedError(NOT_A_READ)
    name, view, formula = found.group("table", "view", "formula")
    if (
        not _name(name)
        or (view is not None and not _name(view))
        or (formula is not None and not _plain(formula, FORMULA_CHARS))
    ):
        raise QueryRefusedError(NOT_A_READ)
    if table is not None and name != table:
        raise QueryRefusedError(OTHER_TABLE)
    return name, view, formula


def error_type(body: bytes) -> str | None:
    """Airtable's ``error.type`` (or ``error`` when it is a string) when it is an upper-case
    word; never the message."""
    try:
        parsed = cast("JsonValue", json.loads(body))
    except ValueError, RecursionError:
        return None
    if not isinstance(parsed, dict):
        return None
    error = parsed.get("error")
    if isinstance(error, dict):
        error = error.get("type")
    return error if isinstance(error, str) and _ERROR_TYPE.fullmatch(error) else None


def airtable_failure(status: int, kind: str | None) -> Exception | None:
    """The connector's error for an answer's status and Airtable error type; ``None`` for a
    2xx."""
    if 200 <= status < 300:  # noqa: PLR2004
        return None
    message = f"the source answered {status} {kind}" if kind else f"the source answered {status}"
    if status in SQLSTATES:
        return QueryFailedError(message, sqlstate=SQLSTATES[status])
    if status == 429 or status >= 500:  # noqa: PLR2004
        return UpstreamUnavailableError(message)
    return QueryFailedError(message, sqlstate=None)


@dataclass(frozen=True, slots=True)
class Record:
    id: str
    created: datetime
    fields: dict[str, JsonValue]


def _bad() -> QueryFailedError:
    return QueryFailedError(NOT_RECORDS, sqlstate="22P02")


def _record(raw: JsonValue) -> Record:
    if not isinstance(raw, dict):
        raise _bad()
    rid, created, fields = raw.get("id"), raw.get("createdTime"), raw.get("fields", {})
    if not isinstance(rid, str) or not rid or not isinstance(created, str):
        raise _bad()
    if not isinstance(fields, dict):
        raise _bad()
    try:
        at = datetime.fromisoformat(created)
    except ValueError:
        raise _bad() from None
    if at.tzinfo is None:
        raise _bad()
    return Record(rid, at.astimezone(UTC), fields)


def records_page(body: bytes) -> tuple[list[Record], str | None]:
    """A list page's records and its ``offset``, ``None`` on the last page."""
    try:
        parsed = cast("JsonValue", json.loads(body))
    except ValueError, RecursionError:
        raise QueryFailedError("the body is not JSON", sqlstate="22P02") from None
    if not isinstance(parsed, dict):
        raise _bad()
    records, offset = parsed.get("records"), parsed.get("offset")
    if not isinstance(records, list) or not (
        offset is None or (isinstance(offset, str) and offset)
    ):
        raise _bad()
    return [_record(r) for r in records], offset


def table_of(records: Sequence[Record]) -> tuple[list[Column], list[list[object]]]:
    """``id`` and ``created_time``, then one column per field in the order fields first appear;
    a record without a field is ``None`` there. A field's type is the REST connector's (its first
    non-null value in the first 100 records); a name already taken gets ``_2``, ``_3``, ..."""
    keys = list(dict.fromkeys(key for r in records for key in r.fields))
    names = header_names([ID, CREATED_TIME, *keys])
    filled = [{key: r.fields.get(key) for key in keys} for r in records]
    typed = columns_for(filled)
    columns = [
        Column(names[0], "string", "string"),
        Column(names[1], "timestamp", "timestamp"),
        *(Column(name, c.type, c.db_type) for name, c in zip(names[2:], typed, strict=True)),
    ]
    cells = rows_for(filled, typed, len(filled))
    return columns, [[r.id, r.created, *row] for r, row in zip(records, cells, strict=True)]


def _field_type(field: dict[str, JsonValue]) -> str:
    kind = field.get("type")
    options = field.get("options")
    if kind == "number" and isinstance(options, dict) and options.get("precision") == 0:
        return "integer"
    return FIELD_TYPES.get(kind, "json") if isinstance(kind, str) else "json"


def tables_in(body: bytes, table: str | None) -> list[Table]:
    """The tables of a base schema answer, at most 500 of 500 columns; with ``table`` only the
    one so named or so identified."""
    try:
        parsed = cast("JsonValue", json.loads(body))
    except ValueError, RecursionError:
        raise QueryFailedError("the body is not JSON", sqlstate="22P02") from None
    found = parsed.get("tables") if isinstance(parsed, dict) else None
    if not isinstance(found, list):
        raise QueryFailedError(NOT_TABLES, sqlstate="22P02")
    tables: list[Table] = []
    for raw in found:
        name = raw.get("name") if isinstance(raw, dict) else None
        fields = raw.get("fields", []) if isinstance(raw, dict) else None
        if not isinstance(raw, dict) or not isinstance(name, str) or not isinstance(fields, list):
            raise QueryFailedError(NOT_TABLES, sqlstate="22P02")
        if table is not None and table not in (name, raw.get("id")):
            continue
        if not all(isinstance(f, dict) and isinstance(f.get("name"), str) for f in fields):
            raise QueryFailedError(NOT_TABLES, sqlstate="22P02")
        kept = cast("list[dict[str, JsonValue]]", fields)
        names = header_names([ID, CREATED_TIME, *(cast("str", f["name"]) for f in kept)])
        columns = [
            Column(names[0], "string", "string"),
            Column(names[1], "timestamp", "timestamp"),
            *(
                Column(n, _field_type(f), str(f.get("type", "")))
                for n, f in zip(names[2:], kept, strict=True)
            ),
        ]
        tables.append(Table(name, columns[:MAX_COLUMNS]))
        if len(tables) >= MAX_TABLES:
            break
    return tables


def _read_on(_: int) -> None:
    """Every answer's body is read: an error's type is named with its status."""
    return


class _Cursor:
    def __init__(self, columns: list[Column], rows: list[list[object]]) -> None:
        self._columns = columns
        self._rows = rows

    @property
    def columns(self) -> Sequence[Column]:
        return self._columns

    async def rows(self) -> AsyncIterator[Sequence[object]]:
        for row in self._rows:
            yield row


class AirtableConnector:
    """A :class:`ssc_datagw.connectors.Connector` for one Airtable base. ``base_url``, ``ca``,
    ``transport`` and ``sleep`` replace Airtable, its trust, the network and the pause between
    pages (tests); a customer sets none of them."""

    def __init__(  # noqa: PLR0913  (the target and its test seams)
        self,
        target: AirtableTarget,
        *,
        base_url: str = API_URL,
        ca: str | None = None,
        connect_seconds: float = CONNECT_SECONDS,
        warmup: Warmup | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._target = target
        self._base = base_url.rstrip("/")
        self._tls = tls_context(ca)
        self._connect_seconds = connect_seconds
        self._warmup = warmup or Warmup()
        self._transport = transport
        self._sleep = sleep

    def list_url(
        self, table: str, view: str | None, formula: str | None, size: int, offset: str | None
    ) -> httpx2.URL:
        params = [("pageSize", str(size))]
        params += [(n, v) for n, v in (("view", view), ("filterByFormula", formula)) if v]
        params += [("offset", offset)] if offset is not None else []
        query = "&".join(f"{name}={quote(value, safe='')}" for name, value in params)
        path = f"/v0/{self._target.base_id}/{quote(table, safe='')}"
        return httpx2.URL(f"{self._base}{path}?{query}")

    async def _list(  # noqa: PLR0913  (the request and how it is timed)
        self,
        client: httpx2.AsyncClient,
        request: tuple[str, str | None, str | None],
        headers: dict[str, str],
        limit: int,
        seconds: float,
    ) -> list[Record]:
        records: list[Record] = []
        offset: str | None = None
        pages = size = status = 0
        while True:
            if pages:
                await self._sleep(PAGE_PAUSE_SECONDS)
            url = self.list_url(*request, min(PAGE_RECORDS, limit - len(records)), offset)
            status, body = await get(
                client,
                url,
                headers,
                seconds=seconds,
                connect_seconds=self._connect_seconds,
                warmup=self._warmup,
                failure=_read_on,
            )
            pages += 1
            size += len(body)
            refused = airtable_failure(status, error_type(body) if status >= 300 else None)  # noqa: PLR2004
            if refused is not None:
                raise refused
            page, offset = records_page(body)
            records += page[: limit - len(records)]
            if len(records) >= limit or offset is None:
                break
        log.info("airtable read: status=%s bytes=%s pages=%s", status, size, pages)
        return records

    @asynccontextmanager
    async def open(self, query: Query) -> AsyncGenerator[_Cursor]:
        request = airtable_request(query.sql, self._target.table)
        if query.params:
            raise QueryRefusedError("an Airtable read takes no parameters")
        headers = {
            "Accept": "application/json",
            "User-Agent": user_agent(query.tag),
            "Authorization": f"Bearer {self._target.token.get_secret_value()}",
        }
        seconds = max(1, query.timeout_ms) / 1000
        client = httpx2.AsyncClient(
            verify=self._tls,
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
        )
        try:
            async with asyncio.timeout(seconds):
                records = await self._list(client, request, headers, query.max_rows + 1, seconds)
        finally:
            await client.aclose()
        columns, rows = table_of(records)
        yield _Cursor(columns, rows)

    async def describe(self, *, schemas: Sequence[str] | None, timeout_ms: int) -> list[Table]:
        """One GET of the base's schema; ``schemas`` does not apply."""
        del schemas
        headers = {
            "Accept": "application/json",
            "User-Agent": user_agent(DESCRIBE_TAG),
            "Authorization": f"Bearer {self._target.token.get_secret_value()}",
        }
        seconds = max(1, timeout_ms) / 1000
        url = httpx2.URL(f"{self._base}/v0/meta/bases/{self._target.base_id}/tables")
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
                    headers,
                    seconds=seconds,
                    connect_seconds=self._connect_seconds,
                    warmup=self._warmup,
                    failure=_read_on,
                )
        finally:
            await client.aclose()
        refused = airtable_failure(status, error_type(body) if status >= 300 else None)  # noqa: PLR2004
        if refused is not None:
            raise refused
        tables = tables_in(body, self._target.table)
        log.info("airtable describe: status=%s bytes=%s tables=%s", status, len(body), len(tables))
        return tables
