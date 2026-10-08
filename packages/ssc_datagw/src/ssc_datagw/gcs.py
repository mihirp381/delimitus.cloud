"""The Google Cloud Storage connector: a service-account JWT, lists and object reads under one
prefix (GA-5 B6).

A connection names one bucket and a prefix, and holds the customer's service-account JSON key;
the customer grants the service account Storage Object Viewer on the bucket under a condition
that keeps reads under the prefix (``docs/contracts/data-gateway.md``, "The GCS connector"). A
query's ``sql`` is one of the S3 connector's two requests (:func:`ssc_datagw.s3.s3_request`):
``list <prefix>`` lists the objects under a prefix, ``get <key>`` reads one ``.csv``, ``.json``,
``.jsonl`` or ``.ndjson`` object as records, by the S3 connector's rules
(:func:`ssc_datagw.s3.object_table`). Anything else, and any name outside the connection's
prefix, is refused before a byte leaves.

Each read signs one JWT with the service account's key (:mod:`ssc_datagw.google`) and sends it
on each request of that read, through :func:`ssc_datagw.rest.get`: TLS against the system trust
store, no redirect, the warm-up retry and the 32 MiB cap. A list follows ``nextPageToken`` until
it holds ``max_rows`` plus one objects; the whole read, every page included, ends at
``timeout_ms``.

The key, the email, the JSON and each JWT are never in a repr, an error or a log line: every
error names a status and Google's reason, never the body, a header or the URL.
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Final, Literal, cast
from urllib.parse import quote

import httpx2
from pydantic import BaseModel, ConfigDict, Field

from ssc_datagw.bigquery import REASON
from ssc_datagw.connectors import (
    Column,
    Query,
    QueryFailedError,
    QueryRefusedError,
    UpstreamUnavailableError,
)
from ssc_datagw.google import ServiceAccount, signer
from ssc_datagw.rest import get
from ssc_datagw.s3 import LIST_COLUMNS, object_table, s3_request, user_agent
from ssc_datagw.tls import tls_context
from ssc_datagw.warmup import CONNECT_SECONDS, Warmup

log = logging.getLogger(__name__)

GCS_URL: Final = "https://storage.googleapis.com"
AUDIENCE: Final = "https://storage.googleapis.com/"
"""The self-signed JWT's ``aud``: the API's own URL, in place of an OAuth scope."""
BUCKET: Final = r"^[a-z0-9][a-z0-9._-]{1,221}[a-z0-9]$"
PREFIX: Final = r"^[^\x00-\x1f]{0,512}$"
"""``ssc_contracts.connections.GcsAddress``'s two patterns."""
PAGE_OBJECTS: Final = 1000
FIELDS: Final = "items(name,size,updated,etag),nextPageToken"
NOT_A_LIST: Final = "the body is not a GCS object list"

_SIZE = re.compile(r"[0-9]+")


class GcsTarget(BaseModel):
    """One bucket, the prefix reads stay under, and the service account that reads it.
    ``service_account`` is the key file's JSON text."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: Literal["gcs"] = "gcs"
    bucket: str = Field(pattern=BUCKET)
    prefix: str = Field(default="", pattern=PREFIX)
    service_account: ServiceAccount


def error_reason(body: bytes) -> str | None:
    """``error.errors[0].reason`` of a Google error body, when it is a plain word."""
    try:
        parsed = cast("object", json.loads(body))
    except ValueError, RecursionError:
        return None
    error = cast("dict[str, object]", parsed).get("error") if isinstance(parsed, dict) else None
    errors = cast("dict[str, object]", error).get("errors") if isinstance(error, dict) else None
    if not isinstance(errors, list) or not errors:
        return None
    first = cast("list[object]", errors)[0]
    reason = cast("dict[str, object]", first).get("reason") if isinstance(first, dict) else None
    return reason if isinstance(reason, str) and REASON.fullmatch(reason) else None


def gcs_failure(status: int, reason: str | None) -> Exception | None:
    """The connector's error for an answer's status and Google's ``reason``; ``None`` for a
    2xx. The message names the status and the reason, never the body."""
    if 200 <= status < 300:
        return None
    shown = f"{status} {reason}" if reason else str(status)
    if status == 401:
        return QueryFailedError("the source refused the credential", sqlstate="28000")
    if status == 403:
        return QueryFailedError(f"the source answered {shown}", sqlstate="42501")
    if status == 404:
        return QueryFailedError(f"the source answered {shown}", sqlstate="42P01")
    if status == 429 or status >= 500:
        return UpstreamUnavailableError(f"the source answered {shown}")
    return QueryFailedError(f"the source answered {shown}", sqlstate=None)


def _bad() -> QueryFailedError:
    return QueryFailedError(NOT_A_LIST, sqlstate="22P02")


def _item(raw: object) -> list[object]:
    if not isinstance(raw, dict):
        raise _bad()
    item = cast("dict[str, object]", raw)
    name, size, updated, etag = (item.get(k) for k in ("name", "size", "updated", "etag"))
    if not (isinstance(name, str) and isinstance(size, str) and isinstance(updated, str)):
        raise _bad()
    if not _SIZE.fullmatch(size) or not (etag is None or isinstance(etag, str)):
        raise _bad()
    try:
        at = datetime.fromisoformat(updated)
    except ValueError:
        raise _bad() from None
    if at.tzinfo is None:
        raise _bad()
    return [name, int(size), at.astimezone(UTC), etag]


def object_list(body: bytes) -> tuple[list[list[object]], str | None]:
    """An ``objects.list`` page's rows (name, size, updated, etag) and its ``nextPageToken``,
    ``None`` on the last page. No ``items`` is no rows."""
    try:
        parsed = cast("object", json.loads(body))
    except ValueError, RecursionError:
        raise _bad() from None
    if not isinstance(parsed, dict):
        raise _bad()
    found = cast("dict[str, object]", parsed)
    items = found.get("items", [])
    token = found.get("nextPageToken")
    if not isinstance(items, list) or not (token is None or isinstance(token, str)):
        raise _bad()
    return [_item(i) for i in cast("list[object]", items)], token or None


def _read_on(_: int) -> None:
    """Every answer's body is read: an error's reason is in it."""
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


class GcsConnector:
    """A :class:`ssc_datagw.connectors.Connector` for one bucket and prefix. ``transport`` and
    ``base_url`` replace the network and Google (tests)."""

    def __init__(
        self,
        target: GcsTarget,
        *,
        connect_seconds: float = CONNECT_SECONDS,
        warmup: Warmup | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        base_url: str = GCS_URL,
    ) -> None:
        self._target = target
        self._signer = signer(target.service_account)
        self._base = base_url.rstrip("/") + f"/storage/v1/b/{target.bucket}/o"
        self._tls = tls_context(None)
        self._connect_seconds = connect_seconds
        self._warmup = warmup or Warmup()
        self._transport = transport

    def object_url(self, name: str) -> httpx2.URL:
        return httpx2.URL(f"{self._base}/{quote(name, safe='')}?alt=media")

    def list_url(self, prefix: str, max_results: int, token: str | None) -> httpx2.URL:
        params: dict[str, str | int] = {"prefix": prefix, "maxResults": max_results}
        if token is not None:
            params["pageToken"] = token
        params["fields"] = FIELDS
        return httpx2.URL(self._base, params=params)

    async def _fetch(
        self,
        client: httpx2.AsyncClient,
        url: httpx2.URL,
        headers: dict[str, str],
        seconds: float,
    ) -> tuple[int, bytes]:
        status, body = await get(
            client,
            url,
            headers,
            seconds=seconds,
            connect_seconds=self._connect_seconds,
            warmup=self._warmup,
            failure=_read_on,
        )
        refused = gcs_failure(status, error_reason(body) if status >= 300 else None)  # noqa: PLR2004
        if refused is not None:
            raise refused
        return status, body

    async def _list(
        self,
        client: httpx2.AsyncClient,
        headers: dict[str, str],
        prefix: str,
        query: Query,
        seconds: float,
    ) -> list[list[object]]:
        limit = query.max_rows + 1
        rows: list[list[object]] = []
        token: str | None = None
        pages = size = status = 0
        list_headers = headers | {"Accept": "application/json"}
        while True:
            url = self.list_url(prefix, min(PAGE_OBJECTS, limit - len(rows)), token)
            status, body = await self._fetch(client, url, list_headers, seconds)
            pages += 1
            size += len(body)
            page, token = object_list(body)
            rows += page[: limit - len(rows)]
            if len(rows) >= limit or token is None:
                break
        log.info("gcs read: op=list status=%s bytes=%s pages=%s", status, size, pages)
        return rows

    @asynccontextmanager
    async def open(self, query: Query) -> AsyncGenerator[_Cursor]:
        op, argument = s3_request(query.sql, self._target.prefix)
        if query.params:
            raise QueryRefusedError("a GCS read takes no parameters")
        headers = {
            "User-Agent": user_agent(query.tag),
            "Authorization": f"Bearer {self._signer.token(AUDIENCE)}",
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
                if op == "list":
                    rows = await self._list(client, headers, argument, query, seconds)
                    columns: Sequence[Column] = LIST_COLUMNS
                else:
                    url = self.object_url(argument)
                    status, body = await self._fetch(client, url, headers, seconds)
                    log.info("gcs read: op=get status=%s bytes=%s pages=1", status, len(body))
                    columns, rows = object_table(argument, body, query.max_rows + 1)
        finally:
            await client.aclose()
        yield _Cursor(columns, rows)
