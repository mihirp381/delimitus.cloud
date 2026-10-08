"""The BigQuery connector: a service-account JWT, one query job, positional parameters (GA-5 B5).

A connection names one project, the dataset unqualified tables are read from, and the job's
location, and holds the customer's service-account JSON key; the customer grants the service
account BigQuery Job User on the project and BigQuery Data Viewer on the dataset.

Each read runs in this order; the first step that refuses answers:

1. :func:`ssc_datagw.classify.bigquery_refusal` refuses anything that is not one plain read,
   and any table or function another project qualifies;
2. the ``?`` placeholders sqlglot's BigQuery tokenizer finds must match the parameters, each
   typed by its value (:func:`query_parameters`);
3. one JWT the service account signs itself (:mod:`ssc_datagw.google`), then one
   ``jobs.query`` POST: positional parameters, the connection's dataset as the default, its
   location, ``maximumBytesBilled`` (the cost guard: BigQuery refuses a job that would bill
   more), ``jobTimeoutMs`` and the query's tag as the label ``ssc-tag``;
4. while the job is not complete, ``getQueryResults`` polls it; more pages follow their
   ``pageToken`` until ``max_rows`` plus one rows.

Every request goes through :func:`ssc_datagw.rest.get`: TLS against the system trust store, the
warm-up retry, the 32 MiB cap. The whole read ends at the query's ``timeout_ms``; past it, and
when the gateway cancels the read, the job is cancelled (5 s at most, from a client of its own)
and BigQuery's own ``jobTimeoutMs`` stops a job whose id never came back.

The key, the email, the JSON and each JWT are never in a repr, an error or a log line: every
error names a status or BigQuery's reason, never the body.
"""

import asyncio
import base64
import json
import logging
import math
import re
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final, Literal, cast

import httpx2
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import tokenize
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from ssc_datagw.classify import bigquery_refusal
from ssc_datagw.connectors import (
    Column,
    JsonValue,
    Query,
    QueryFailedError,
    QueryRefusedError,
    Scalar,
    UpstreamUnavailableError,
)
from ssc_datagw.google import ServiceAccount, signer
from ssc_datagw.rest import USER_AGENT, get
from ssc_datagw.tls import tls_context
from ssc_datagw.warmup import CONNECT_SECONDS, Warmup

log = logging.getLogger(__name__)

BIGQUERY_URL: Final = "https://bigquery.googleapis.com"
AUDIENCE: Final = "https://bigquery.googleapis.com/"
"""The self-signed JWT's ``aud``: the API's own URL, in place of an OAuth scope."""
PROJECT: Final = r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
DATASET: Final = r"^[A-Za-z0-9_]{1,1024}$"
LOCATION: Final = r"^[A-Za-z0-9-]{1,32}$"
"""``ssc_contracts.connections.BigQueryAddress``'s three patterns."""
MIN_BYTES_BILLED: Final = 2**20
MAX_BYTES_BILLED: Final = 2**40
BYTES_BILLED: Final = 2**30
WAIT_MS: Final = 60_000
"""The longest one ``jobs.query`` or ``getQueryResults`` call waits for the job."""
CANCEL_SECONDS: Final = 5.0
LABEL: Final = "ssc-tag"
LABEL_CHARS: Final = 63
INT64: Final = (-(2**63), 2**63 - 1)
JOB_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,1024}")
JOB_LOCATION: Final = re.compile(r"[A-Za-z0-9-]{1,64}")
REASON: Final = re.compile(r"[A-Za-z]{1,64}")
NOT_A_RESULT: Final = "the body is not a BigQuery result"
LEGACY: Final = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "RECORD": "STRUCT"}
"""The API's legacy type names and the GoogleSQL names ``db_type`` gives instead."""
PORTABLE: Final = {
    "INT64": "integer",
    "FLOAT64": "float",
    "NUMERIC": "decimal",
    "BIGNUMERIC": "decimal",
    "BOOL": "boolean",
    "STRING": "string",
    "BYTES": "bytes",
    "DATE": "date",
    "TIME": "time",
    "DATETIME": "timestamp",
    "TIMESTAMP": "timestamp",
    "GEOGRAPHY": "string",
    "JSON": "json",
    "INTERVAL": "interval",
}
"""Portable column types; a ``REPEATED`` column or a ``STRUCT`` is ``json``, any other type
``string``."""
FAILED_REASONS: Final = {
    "invalidQuery": "42601",
    "notFound": "42P01",
    "accessDenied": "42501",
    "billingNotEnabled": "42501",
    "billingTierLimitExceeded": "42501",
    "bytesBilledLimitExceeded": "53400",
    "responseTooLarge": "54000",
    "stopped": "57014",
    "invalid": "22023",
}
UNAVAILABLE_REASONS: Final = frozenset(
    {"rateLimitExceeded", "quotaExceeded", "backendError", "jobRateLimitExceeded", "internalError"}
)
INTERVAL: Final = re.compile(
    r"(?P<ym>-?)(?P<y>\d+)-(?P<mo>\d+) (?P<d>-?\d+) (?P<hms>-?)(?P<h>\d+):(?P<mi>\d+):"
    r"(?P<s>\d+(?:\.\d{1,9})?)"
)
"""BigQuery's canonical ``INTERVAL`` text, ``[-]Y-M [-]D [-]H:M:S[.F]``."""
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class BigQueryTarget(BaseModel):
    """One project and dataset and the service account that reads them. ``service_account``
    is the key file's JSON text; ``max_bytes_billed`` caps what one read may bill."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: Literal["bigquery"] = "bigquery"
    project: str = Field(pattern=PROJECT)
    dataset: str = Field(pattern=DATASET)
    location: str = Field(default="US", pattern=LOCATION)
    service_account: ServiceAccount
    max_bytes_billed: int = Field(default=BYTES_BILLED, ge=MIN_BYTES_BILLED, le=MAX_BYTES_BILLED)


def placeholders(sql: str) -> int:
    """How many ``?`` placeholders BigQuery reads in ``sql``: one in a string or a comment is
    not one."""
    try:
        tokens = tokenize(sql, read="bigquery")
    except SqlglotError:
        return 0
    return sum(1 for t in tokens if t.token_type == TokenType.PLACEHOLDER)


def _float(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return repr(value)


def _parameter(value: Scalar) -> dict[str, JsonValue]:
    kind: str
    text: str | None
    if isinstance(value, bool):
        kind, text = "BOOL", "true" if value else "false"
    elif isinstance(value, int):
        if not INT64[0] <= value <= INT64[1]:
            raise QueryFailedError("an integer parameter is outside INT64", sqlstate="22003")
        kind, text = "INT64", str(value)
    elif isinstance(value, float):
        kind, text = "FLOAT64", _float(value)
    else:
        kind, text = "STRING", value
    return {"parameterType": {"type": kind}, "parameterValue": {"value": text}}


def query_parameters(params: Sequence[Scalar]) -> list[JsonValue]:
    """``params`` as positional query parameters: a boolean ``BOOL``, an integer ``INT64``, a
    float ``FLOAT64``, a string ``STRING``, ``None`` a ``STRING`` null."""
    return [_parameter(v) for v in params]


def label(tag: str) -> str:
    """``tag`` as a label value: lower case, each character outside ``[a-z0-9_-]`` an ``_``,
    at most 63 characters."""
    return re.sub(r"[^a-z0-9_-]", "_", tag.lower())[:LABEL_CHARS]


def _reason(body: object) -> str | None:
    """``error.errors[0].reason`` of an error body, or a completed job's ``errors[0].reason``,
    when it is a plain word."""
    if not isinstance(body, dict):
        return None
    found = cast("dict[str, object]", body)
    error = found.get("error")
    errors = cast("dict[str, object]", error).get("errors") if isinstance(error, dict) else None
    if errors is None:
        errors = found.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    first = cast("list[object]", errors)[0]
    reason = cast("dict[str, object]", first).get("reason") if isinstance(first, dict) else None
    return reason if isinstance(reason, str) and REASON.fullmatch(reason) else None


def failure(status: int, reason: str | None) -> Exception:  # noqa: PLR0911  (one return per rule)
    """The connector's error for an answer BigQuery refused: by its reason first (BigQuery
    answers 403 for a quota), then by its status. The message names the reason only."""
    named = reason or "unknown"
    if reason == "bytesBilledLimitExceeded":
        return QueryFailedError("the read would bill more than max_bytes_billed", sqlstate="53400")
    if reason in FAILED_REASONS:
        return QueryFailedError(
            f"bigquery refused the read: {named}", sqlstate=FAILED_REASONS[reason]
        )
    if reason in UNAVAILABLE_REASONS:
        return UpstreamUnavailableError(f"bigquery is unavailable: {named}")
    if reason == "timeout":
        return TimeoutError("the job passed jobTimeoutMs")
    if status == 401:
        return QueryFailedError("the source refused the credential", sqlstate="28000")
    if status == 403:
        return QueryFailedError(f"bigquery refused the read: {named}", sqlstate="42501")
    if status == 429 or status >= 500:
        return UpstreamUnavailableError(f"bigquery answered {status}: {named}")
    return QueryFailedError(f"bigquery answered {status}: {named}", sqlstate=None)


def _bad() -> QueryFailedError:
    return QueryFailedError(NOT_A_RESULT, sqlstate="22P02")


@dataclass(frozen=True, slots=True)
class SchemaField:
    """One column, or one member of a ``STRUCT``, as ``schema.fields`` names it. ``type`` is
    the GoogleSQL name."""

    name: str
    type: str
    mode: str = "NULLABLE"
    fields: tuple[SchemaField, ...] = ()

    @property
    def repeated(self) -> bool:
        return self.mode == "REPEATED"

    @property
    def db_type(self) -> str:
        return f"ARRAY<{self.type}>" if self.repeated else self.type

    @property
    def portable(self) -> str:
        if self.repeated or self.type == "STRUCT":
            return "json"
        return PORTABLE.get(self.type, "string")


def schema_fields(raw: object) -> tuple[SchemaField, ...]:
    """``schema.fields`` as :class:`SchemaField`, or 22P02."""
    if not isinstance(raw, list):
        raise _bad()
    found: list[SchemaField] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            raise _bad()
        field = cast("dict[str, object]", item)
        name, kind, mode = field.get("name"), field.get("type"), field.get("mode", "NULLABLE")
        if not (isinstance(name, str) and isinstance(kind, str) and isinstance(mode, str)):
            raise _bad()
        kind = LEGACY.get(kind.upper(), kind.upper())
        members = schema_fields(field.get("fields")) if kind == "STRUCT" else ()
        found.append(SchemaField(name, kind, mode.upper(), members))
    return tuple(found)


def _timestamp(text: str) -> datetime:
    """Microseconds since the epoch (``useInt64Timestamp``), or seconds as a decimal string."""
    if re.fullmatch(r"-?\d+", text):
        micros = int(text)
    else:
        seconds = Decimal(text)
        if not seconds.is_finite():
            raise ValueError(text)
        micros = int((seconds * 10**6).to_integral_value())
    return _EPOCH + timedelta(microseconds=micros)


def interval(text: str) -> str:
    """BigQuery's ``INTERVAL`` text as an ISO 8601 duration, each part with its own sign:
    ``1-2 3 4:5:6.5`` is ``P1Y2M3DT4H5M6.5S``. A ``timedelta`` cannot hold months."""
    found = INTERVAL.fullmatch(text)
    if found is None:
        raise ValueError(text)
    g = found.groupdict()
    seconds = Decimal(g["s"]).normalize()
    parts = [
        (int(g["y"]) * (-1 if g["ym"] else 1), "Y"),
        (int(g["mo"]) * (-1 if g["ym"] else 1), "M"),
        (int(g["d"]), "D"),
    ]
    times = [
        (int(g["h"]) * (-1 if g["hms"] else 1), "H"),
        (int(g["mi"]) * (-1 if g["hms"] else 1), "M"),
        (seconds * (-1 if g["hms"] else 1), "S"),
    ]
    day = "".join(f"{n}{unit}" for n, unit in parts if n)
    clock = "".join(f"{n:f}{unit}" if unit == "S" else f"{n}{unit}" for n, unit in times if n)
    if not day and not clock:
        return "PT0S"
    return f"P{day}" + (f"T{clock}" if clock else "")


def _decimal(text: str) -> Decimal:
    value = Decimal(text)
    if not value.is_finite():
        raise ValueError(text)
    return value


def _bool(text: str) -> bool:
    if text not in ("true", "false"):
        raise ValueError(text)
    return text == "true"


def scalar(kind: str, text: str) -> object:  # noqa: PLR0911  (one return per type)
    """One value's text as its column's type."""
    match kind:
        case "INT64":
            return int(text)
        case "FLOAT64":
            return float(text)
        case "NUMERIC" | "BIGNUMERIC":
            return _decimal(text)
        case "BOOL":
            return _bool(text)
        case "BYTES":
            return base64.b64decode(text, validate=True)
        case "DATE":
            return date.fromisoformat(text)
        case "TIME":
            return time.fromisoformat(text)
        case "DATETIME":
            return datetime.fromisoformat(text)
        case "TIMESTAMP":
            return _timestamp(text)
        case "JSON":
            return cast("object", json.loads(text))
        case "INTERVAL":
            return interval(text)
        case _:
            return text


def _members(raw: object) -> list[object] | None:
    """The ``f`` list of a ``{"f": [...]}`` row or ``STRUCT``."""
    found = cast("dict[str, object]", raw).get("f") if isinstance(raw, dict) else None
    return cast("list[object]", found) if isinstance(found, list) else None


def _cell(raw: object) -> object:
    """The ``v`` of a ``{"v": ...}`` cell."""
    if not isinstance(raw, dict) or "v" not in raw:
        raise _bad()
    return cast("dict[str, object]", raw)["v"]


def _one(field: SchemaField, raw: object) -> object:
    if raw is None:
        return None
    if field.type == "STRUCT":
        cells = _members(raw)
        if cells is None or len(cells) != len(field.fields):
            raise _bad()
        return {f.name: value(f, _cell(c)) for f, c in zip(field.fields, cells, strict=True)}
    if not isinstance(raw, str):
        raise _bad()
    return scalar(field.type, raw)


def value(field: SchemaField, raw: object) -> object:
    """A cell's ``v`` as plain values: a ``REPEATED`` one a list, a ``STRUCT`` a dict by member
    name, each member by its own type."""
    if raw is None:
        return None
    if field.repeated:
        if not isinstance(raw, list):
            raise _bad()
        return [_one(field, _cell(item)) for item in cast("list[object]", raw)]
    return _one(field, raw)


def row(fields: Sequence[SchemaField], raw: object) -> list[object]:
    """One of ``rows``, ``{"f": [{"v": ...}, ...]}``, in column order."""
    cells = _members(raw)
    if cells is None or len(cells) != len(fields):
        raise _bad()
    try:
        return [value(f, _cell(c)) for f, c in zip(fields, cells, strict=True)]
    except ValueError, InvalidOperation, OverflowError, RecursionError:
        raise _bad() from None


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    location: str


@dataclass(frozen=True, slots=True)
class Page:
    """One ``jobs.query`` or ``getQueryResults`` answer."""

    complete: bool
    job: Job | None
    fields: tuple[SchemaField, ...] | None
    rows: list[object]
    token: str | None


def _job(raw: object, location: str) -> Job | None:
    if raw is None:
        return None
    reference = cast("dict[str, object]", raw) if isinstance(raw, dict) else {}
    job_id, where = reference.get("jobId"), reference.get("location", location)
    if not (isinstance(job_id, str) and JOB_ID.fullmatch(job_id)):
        raise _bad()
    if not (isinstance(where, str) and JOB_LOCATION.fullmatch(where)):
        raise _bad()
    return Job(job_id, where)


def page(body: object, location: str) -> Page:
    """A 2xx answer as a :class:`Page`, or 22P02. A complete job that reports ``errors`` and no
    ``schema`` failed: :func:`failure` by its reason. ``errors`` beside a schema are
    warnings."""
    if not isinstance(body, dict):
        raise _bad()
    found = cast("dict[str, object]", body)
    complete = found.get("jobComplete")
    if not isinstance(complete, bool):
        raise _bad()
    job = _job(found.get("jobReference"), location)
    if not complete:
        if job is None:
            raise _bad()
        return Page(complete=False, job=job, fields=None, rows=[], token=None)
    if found.get("errors") and "schema" not in found:
        raise failure(200, _reason(found))
    schema = found.get("schema")
    if not isinstance(schema, dict):
        raise _bad()
    fields = schema_fields(cast("dict[str, object]", schema).get("fields", []))
    rows = found.get("rows", [])
    token = found.get("pageToken")
    if not isinstance(rows, list) or not (token is None or isinstance(token, str)):
        raise _bad()
    return Page(True, job, fields, cast("list[object]", rows), token or None)


class _Cursor:
    def __init__(self, columns: list[Column], rows: list[list[object]]) -> None:
        self._columns = columns
        self._rows = rows

    @property
    def columns(self) -> Sequence[Column]:
        return self._columns

    async def rows(self) -> AsyncIterator[Sequence[object]]:
        for found in self._rows:
            yield found


def _read_on(_: int) -> None:
    """Every status reads the body: BigQuery's reason is in it."""


@dataclass
class _Read:
    """What one read did, for its log line, and the job to cancel."""

    job: Job | None = None
    pages: int = 0
    polls: int = 0
    bytes: int = 0


class BigQueryConnector:
    """A :class:`ssc_datagw.connectors.Connector` for one BigQuery dataset. ``transport`` and
    ``base_url`` replace the network and Google (tests)."""

    def __init__(
        self,
        target: BigQueryTarget,
        *,
        connect_seconds: float = CONNECT_SECONDS,
        warmup: Warmup | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        base_url: str = BIGQUERY_URL,
    ) -> None:
        self._target = target
        self._signer = signer(target.service_account)
        self._base = base_url.rstrip("/") + f"/bigquery/v2/projects/{target.project}"
        self._tls = tls_context(None)
        self._connect_seconds = connect_seconds
        self._warmup = warmup or Warmup()
        self._transport = transport
        self._ending: set[asyncio.Task[None]] = set()

    def _client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            verify=self._tls, transport=self._transport, follow_redirects=False, trust_env=False
        )

    def _request(
        self, query: Query, parameters: list[JsonValue], want: int
    ) -> dict[str, JsonValue]:
        t = self._target
        timeout_ms = max(1, query.timeout_ms)
        return {
            "query": query.sql,
            "useLegacySql": False,
            "parameterMode": "POSITIONAL",
            "queryParameters": parameters,
            "defaultDataset": {"projectId": t.project, "datasetId": t.dataset},
            "location": t.location,
            "maximumBytesBilled": str(t.max_bytes_billed),
            "timeoutMs": min(timeout_ms, WAIT_MS),
            "jobTimeoutMs": str(timeout_ms),
            "maxResults": want,
            "formatOptions": {"useInt64Timestamp": True},
            "labels": {LABEL: label(query.tag)},
        }

    async def _call(  # noqa: PLR0913  (the request and how it is timed)
        self,
        client: httpx2.AsyncClient,
        url: httpx2.URL,
        headers: dict[str, str],
        read: _Read,
        *,
        seconds: float,
        content: bytes | None = None,
    ) -> Page:
        status, body = await get(
            client,
            url,
            headers,
            seconds=seconds,
            connect_seconds=self._connect_seconds,
            warmup=self._warmup,
            failure=_read_on,
            method="GET" if content is None else "POST",
            content=content,
        )
        read.bytes += len(body)
        try:
            parsed = cast("object", json.loads(body))
        except ValueError, RecursionError:
            parsed = None
        if not 200 <= status < 300:
            raise failure(status, _reason(parsed))
        return page(parsed, self._target.location)

    def _results(self, job: Job, wait_ms: int, want: int, token: str | None) -> httpx2.URL:
        params: dict[str, str | int] = {
            "location": job.location,
            "timeoutMs": wait_ms,
            "maxResults": want,
            "formatOptions.useInt64Timestamp": "true",
        }
        if token is not None:
            params["pageToken"] = token
        return httpx2.URL(f"{self._base}/queries/{job.job_id}", params=params)

    async def _read(
        self,
        client: httpx2.AsyncClient,
        headers: dict[str, str],
        query: Query,
        read: _Read,
    ) -> tuple[list[Column], list[list[object]]]:
        want = query.max_rows + 1
        seconds = max(1, query.timeout_ms) / 1000
        wait_ms = min(max(1, query.timeout_ms), WAIT_MS)
        parameters = query_parameters(query.params)
        content = json.dumps(self._request(query, parameters, want)).encode()
        found = await self._call(
            client,
            httpx2.URL(f"{self._base}/queries"),
            headers | {"Content-Type": "application/json"},
            read,
            seconds=seconds,
            content=content,
        )
        read.job = found.job
        while not found.complete and read.job is not None:
            read.polls += 1
            url = self._results(read.job, wait_ms, want, None)
            found = await self._call(client, url, headers, read, seconds=seconds)
        fields = found.fields or ()
        raw = found.rows[:want]
        read.pages = 1
        while len(raw) < want and found.token is not None and read.job is not None:
            url = self._results(read.job, wait_ms, want - len(raw), found.token)
            found = await self._call(client, url, headers, read, seconds=seconds)
            read.pages += 1
            raw += found.rows[: want - len(raw)]
        columns = [Column(f.name, f.portable, f.db_type) for f in fields]
        return columns, [row(fields, r) for r in raw]

    async def _cancel(self, job: Job, headers: dict[str, str]) -> None:
        """Cancel ``job``, best effort, from a client of its own."""
        url = httpx2.URL(
            f"{self._base}/jobs/{job.job_id}/cancel", params={"location": job.location}
        )
        try:
            async with asyncio.timeout(CANCEL_SECONDS):
                client = self._client()
                try:
                    await get(
                        client,
                        url,
                        headers,
                        seconds=CANCEL_SECONDS,
                        connect_seconds=min(self._connect_seconds, CANCEL_SECONDS),
                        warmup=self._warmup,
                        method="POST",
                        content=b"",
                    )
                finally:
                    await client.aclose()
        except (QueryFailedError, UpstreamUnavailableError, TimeoutError) as exc:
            log.warning("could not cancel the job: %s", type(exc).__name__)

    async def _end(self, job: Job, headers: dict[str, str]) -> None:
        """Run :meth:`_cancel` to the end even if the caller is cancelled again."""
        task = asyncio.create_task(self._cancel(job, headers))
        self._ending.add(task)
        task.add_done_callback(self._ending.discard)
        await asyncio.shield(task)

    @asynccontextmanager
    async def open(self, query: Query) -> AsyncGenerator[_Cursor]:
        reason = bigquery_refusal(query.sql, self._target.project)
        if reason is not None:
            raise QueryRefusedError(reason)
        count = placeholders(query.sql)
        if count != len(query.params):
            raise QueryFailedError(
                f"{len(query.params)} parameters for {count} placeholders", sqlstate="07001"
            )
        query_parameters(query.params)  # a parameter BigQuery cannot take fails before a request
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self._signer.token(AUDIENCE)}",
        }
        read = _Read()
        client = self._client()
        try:
            async with asyncio.timeout(max(1, query.timeout_ms) / 1000):
                columns, rows = await self._read(client, headers, query, read)
        except TimeoutError:
            if read.job is not None:
                await self._end(read.job, headers)
            raise TimeoutError("the read passed timeout_ms") from None
        except asyncio.CancelledError:
            if read.job is not None:
                await self._end(read.job, headers)
            raise
        finally:
            await client.aclose()
        log.info("bigquery read: pages=%s polls=%s bytes=%s", read.pages, read.polls, read.bytes)
        yield _Cursor(columns, rows)
