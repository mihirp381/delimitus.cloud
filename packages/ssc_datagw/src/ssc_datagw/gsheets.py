"""The Google Sheets connector: a service-account JWT, one range read (GA-5 B3).

A connection names one spreadsheet by id and, optionally, the one tab apps read, and holds the
customer's service-account JSON key; the customer shares the spreadsheet with the service
account's email. A query's ``sql`` is an A1 range (:func:`a1_range`), refused before anything
is sent when it is anything else.

Each read signs its own JWT with the service account's key (Google's self-signed JWT for a
service account, no token endpoint) and sends one GET of the range's values, through
:func:`ssc_datagw.rest.get`: TLS against the system trust store, the warm-up retry, the 32 MiB
cap, and the whole read, connect included, ending at the query's ``timeout_ms``. The first row of
values names the columns; the rows after it are the records.

The key, the email, the JSON and each JWT are never in a repr, an error or a log line: every
error names a status or an exception class.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final, Literal, cast
from urllib.parse import quote

import httpx2
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ssc_datagw.connectors import (
    Column,
    JsonValue,
    Query,
    QueryFailedError,
    QueryRefusedError,
)
from ssc_datagw.rest import TYPE_SAMPLE, USER_AGENT, get, status_failure
from ssc_datagw.tls import tls_context
from ssc_datagw.warmup import CONNECT_SECONDS, Warmup

log = logging.getLogger(__name__)

type Cell = str | int | float | bool

SHEETS_URL: Final = "https://sheets.googleapis.com"
AUDIENCE: Final = "https://sheets.googleapis.com/"
"""The self-signed JWT's ``aud``: the API's own URL, in place of an OAuth scope."""
TOKEN_SECONDS: Final = 3600
SPREADSHEET_ID: Final = r"^[A-Za-z0-9_-]{20,128}$"
SHEET: Final = r"^[^\x00-\x1f'!:]{1,100}$"
"""``ssc_contracts.connections.SheetsAddress``'s two patterns."""
SHEET_CHARS: Final = 100
RANGE_CHARS: Final = 300
RENDER: Final = {
    "valueRenderOption": "UNFORMATTED_VALUE",
    "dateTimeRenderOption": "SERIAL_NUMBER",
    "majorDimension": "ROWS",
}
NOT_A_KEY: Final = (
    "the service account is not a JSON key with client_email, private_key and private_key_id"
)
NOT_A_RANGE: Final = "the query is not an A1 range"
OTHER_SHEET: Final = "the range names another sheet than the connection's"
NOT_VALUES: Final = "the body is not a Sheets value range"

_COL = r"[A-Za-z]{1,3}"
_ROW = r"[1-9][0-9]{0,6}"
_CELLS = rf"{_COL}{_ROW}(?::{_COL}(?:{_ROW})?)?|{_COL}:{_COL}|{_ROW}:{_ROW}"
_NAME = r"'(?:[^'\x00-\x1f]|'')+'|[A-Za-z0-9_]{1,100}"
RANGE: Final = re.compile(rf"(?:(?P<sheet>{_NAME})!)?(?P<cells>{_CELLS})")
"""``A1``, ``A1:D100``, ``A1:D``, ``A:D`` or ``1:100``, optionally after ``Sheet!`` or
``'Sheet name'!`` (``''`` is a quote in a quoted name)."""
TAB: Final = re.compile(rf"(?P<sheet>{_NAME})")
"""A sheet name alone: the whole tab."""
CELL_TYPES: Final[Sequence[tuple[type, str, str]]] = (
    (bool, "boolean", "boolean"),
    (int, "integer", "number"),
    (float, "float", "number"),
    (str, "string", "string"),
)
"""A cell's Python type, its portable type and its Sheets type. ``bool`` comes before ``int``,
which it is a subclass of."""


@dataclass(frozen=True, slots=True, repr=False)
class _Signer:
    """The three parts of the key a read needs. No repr: each part is the credential's."""

    email: str
    kid: str
    key: rsa.RSAPrivateKey

    def token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self.email,
            "sub": self.email,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + TOKEN_SECONDS,
        }
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})


def _parsed(raw: str) -> _Signer | None:
    try:
        account = json.loads(raw)
        if not isinstance(account, dict):
            return None
        fields = cast("dict[str, object]", account)
        email, pem, kid = (
            fields.get("client_email"),
            fields.get("private_key"),
            fields.get("private_key_id"),
        )
        if not (isinstance(email, str) and isinstance(pem, str) and isinstance(kid, str)):
            return None
        if not (email and pem and kid):
            return None
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except ValueError, TypeError, RecursionError, UnsupportedAlgorithm:
        return None
    if not isinstance(key, rsa.RSAPrivateKey):
        return None
    return _Signer(email, kid, key)


def _signer(service_account: SecretStr) -> _Signer:
    """The parsed key, or a ``ValueError`` that quotes nothing of it (raised outside the
    ``except`` so it carries no parser error, which holds the text)."""
    signer = _parsed(service_account.get_secret_value())
    if signer is None:
        raise ValueError(NOT_A_KEY)
    return signer


class GsheetsTarget(BaseModel):
    """One spreadsheet and the service account that reads it. ``sheet`` is the one tab apps
    read, every tab when left out. ``service_account`` is the key file's JSON text."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: Literal["gsheets"] = "gsheets"
    spreadsheet_id: str = Field(pattern=SPREADSHEET_ID)
    sheet: str | None = Field(default=None, pattern=SHEET)
    service_account: SecretStr

    @field_validator("service_account")
    @classmethod
    def _is_a_key(cls, value: SecretStr) -> SecretStr:
        _signer(value)
        return value


def _quoted(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def a1_range(sql: str, sheet: str | None) -> str:
    """``sql`` as the A1 range to read, or :class:`QueryRefusedError`. The tab is always
    quoted. With ``sheet`` a bare range reads that tab, a name must be it (any case), and it is
    sent as ``sheet`` spells it; without, a bare range reads the first tab."""
    found = None if len(sql) > RANGE_CHARS else RANGE.fullmatch(sql) or TAB.fullmatch(sql)
    if found is None:
        raise QueryRefusedError(NOT_A_RANGE)
    groups = found.groupdict()
    named, cells = groups.get("sheet"), groups.get("cells")
    if named is not None and named.startswith("'"):
        named = named[1:-1].replace("''", "'")
        if len(named) > SHEET_CHARS:
            raise QueryRefusedError(NOT_A_RANGE)
    if sheet is not None:
        if named is not None and named.casefold() != sheet.casefold():
            raise QueryRefusedError(OTHER_SHEET)
        named = sheet
    if named is None:
        return cast("str", cells)
    return _quoted(named) if cells is None else f"{_quoted(named)}!{cells}"


def sheets_failure(status: int) -> Exception | None:
    """:func:`ssc_datagw.rest.status_failure` but for two: after :func:`a1_range` a 400 means
    Google found no such tab or range, and a 403 that the spreadsheet is not shared with the
    service account."""
    if status == 400:
        return QueryFailedError("no such sheet or range", sqlstate="42P01")
    if status == 403:
        return QueryFailedError(
            "the service account may not read this spreadsheet", sqlstate="42501"
        )
    return status_failure(status)


def _is_cell(value: object) -> bool:
    return isinstance(value, (str, int, float))


def values_in(body: JsonValue) -> list[list[Cell]]:
    """The ``values`` of a value range, rows of cells; none when Google left it out."""
    if not isinstance(body, dict):
        raise QueryFailedError(NOT_VALUES, sqlstate="22P02")
    if "values" not in body:
        return []
    values = body["values"]
    if not isinstance(values, list) or not all(
        isinstance(row, list) and all(_is_cell(c) for c in row) for row in values
    ):
        raise QueryFailedError(NOT_VALUES, sqlstate="22P02")
    return cast("list[list[Cell]]", values)


def _header_name(cell: Cell | None, position: int) -> str:
    if cell is None or cell == "":
        return f"col{position}"
    return cell if isinstance(cell, str) else json.dumps(cell)


def header_names(header: Sequence[Cell]) -> list[str]:
    """A cell's text names its column (a number or boolean its JSON text), an empty one
    ``col<n>`` by its 1-based position; a name already taken gets ``_2``, ``_3``, ..."""
    names: list[str] = []
    for position, cell in enumerate(header, start=1):
        base = name = _header_name(cell, position)
        n = 1
        while name in names:
            n += 1
            name = f"{base}_{n}"
        names.append(name)
    return names


def _row(cells: Sequence[Cell], width: int) -> list[Cell | None]:
    """Cut or padded to ``width``; an empty cell (Google's ``""``) is ``None``."""
    kept: list[Cell | None] = [None if c == "" else c for c in cells[:width]]
    return kept + [None] * (width - len(kept))


def _column(name: str, values: Sequence[Cell | None]) -> Column:
    first = next((v for v in values if v is not None), None)
    for kind, portable, db_type in CELL_TYPES:
        if isinstance(first, kind):
            return Column(name, portable, db_type)
    return Column(name, "string", "string")


def table(
    values: Sequence[Sequence[Cell]], limit: int
) -> tuple[list[Column], list[list[Cell | None]]]:
    """The columns the first row names, each typed by its first non-null value in the first
    :data:`~ssc_datagw.rest.TYPE_SAMPLE` rows after it, and at most ``limit`` of those rows."""
    if not values:
        return [], []
    names = header_names(values[0])
    sample = [_row(r, len(names)) for r in values[1 : 1 + TYPE_SAMPLE]]
    columns = [_column(name, [r[i] for r in sample]) for i, name in enumerate(names)]
    return columns, [_row(r, len(names)) for r in values[1 : 1 + limit]]


class _Cursor:
    def __init__(self, columns: list[Column], rows: list[list[Cell | None]]) -> None:
        self._columns = columns
        self._rows = rows

    @property
    def columns(self) -> Sequence[Column]:
        return self._columns

    async def rows(self) -> AsyncIterator[Sequence[object]]:
        for row in self._rows:
            yield row


class GsheetsConnector:
    """A :class:`ssc_datagw.connectors.Connector` for one spreadsheet. ``transport`` and
    ``base_url`` replace the network and Google (tests)."""

    def __init__(
        self,
        target: GsheetsTarget,
        *,
        connect_seconds: float = CONNECT_SECONDS,
        warmup: Warmup | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        base_url: str = SHEETS_URL,
    ) -> None:
        self._target = target
        self._signer = _signer(target.service_account)
        self._base = base_url.rstrip("/")
        self._tls = tls_context(None)
        self._connect_seconds = connect_seconds
        self._warmup = warmup or Warmup()
        self._transport = transport

    def _url(self, cells: str) -> httpx2.URL:
        path = f"/v4/spreadsheets/{self._target.spreadsheet_id}/values/{quote(cells, safe='')}"
        return httpx2.URL(self._base + path, params=RENDER)

    @asynccontextmanager
    async def open(self, query: Query) -> AsyncGenerator[_Cursor]:
        cells = a1_range(query.sql, self._target.sheet)
        if query.params:
            raise QueryRefusedError("a Google Sheets read takes no parameters")
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self._signer.token()}",
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
                status, body = await get(
                    client,
                    self._url(cells),
                    headers,
                    seconds=seconds,
                    connect_seconds=self._connect_seconds,
                    warmup=self._warmup,
                    failure=sheets_failure,
                )
        finally:
            await client.aclose()
        log.info("gsheets read: status=%s bytes=%s", status, len(body))
        try:
            parsed = cast("JsonValue", json.loads(body))
        except ValueError, RecursionError:
            raise QueryFailedError("the body is not JSON", sqlstate="22P02") from None
        columns, rows = table(values_in(parsed), query.max_rows + 1)
        yield _Cursor(columns, rows)
