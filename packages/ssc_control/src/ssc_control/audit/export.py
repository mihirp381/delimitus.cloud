"""Audit export as CSV or JSON Lines, oldest first, streamed from one consistent snapshot.

``actor_ip`` is never exported (``db/PII.md``: redact on export). CSV cells that a spreadsheet
would run as a formula are prefixed with ``'``. JSON-lines rows add ``canonical`` (base64), so a
customer can recompute every hash offline: ``sha256(prev_hash || canonical) == hash``.
"""

import base64
import csv
import io
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncEngine

from ssc_control.audit.chain import canonical_bytes
from ssc_control.audit.search import AuditFilters, Row, event_record, select_events, utc_iso
from ssc_control.db.bind import bound_org

type ExportFormat = Literal["csv", "jsonl"]

MEDIA_TYPES: Final[Mapping[ExportFormat, str]] = {
    "csv": "text/csv",
    "jsonl": "application/x-ndjson",
}
CSV_COLUMNS: Final = (
    "seq",
    "at",
    "action",
    "actor_kind",
    "actor_id",
    "actor_via_agent",
    "actor_client_id",
    "target_kind",
    "target_id",
    "before",
    "after",
    "policy_decision_id",
    "prev_hash",
    "hash",
)
BATCH: Final = 1000
_FLUSH_AT: Final = 64 * 1024
_FORMULA_START: Final = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        text = canonical_bytes(value).decode()  # pyright: ignore[reportUnknownArgumentType]
    else:
        text = str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def csv_row(row: Row) -> list[str]:
    values: dict[str, object] = {
        **row,
        "at": utc_iso(row["at"]),
        "prev_hash": bytes(row["prev_hash"]).hex(),
        "hash": bytes(row["hash"]).hex(),
    }
    return [_cell(values[name]) for name in CSV_COLUMNS]


def jsonl_line(row: Row) -> bytes:
    record = event_record(row)
    record["canonical"] = base64.b64encode(bytes(row["canonical"])).decode()
    return canonical_bytes(record) + b"\n"


async def render_csv(rows: AsyncIterable[Row]) -> AsyncIterator[bytes]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    async for row in rows:
        writer.writerow(csv_row(row))
        if buffer.tell() >= _FLUSH_AT:
            yield buffer.getvalue().encode()
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue().encode()


async def render_jsonl(rows: AsyncIterable[Row]) -> AsyncIterator[bytes]:
    chunk = bytearray()
    async for row in rows:
        chunk += jsonl_line(row)
        if len(chunk) >= _FLUSH_AT:
            yield bytes(chunk)
            chunk.clear()
    if chunk:
        yield bytes(chunk)


async def stream_export(
    engine: AsyncEngine, org_id: str, filters: AuditFilters, fmt: ExportFormat
) -> AsyncIterator[bytes]:
    """The export body, read in its own REPEATABLE READ, read-only transaction."""
    snapshot = engine.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
    query = select_events(org_id, filters, newest_first=False)
    render = render_csv if fmt == "csv" else render_jsonl
    async with bound_org(snapshot, org_id) as conn:
        options = {"yield_per": BATCH}
        async with conn.stream(query, execution_options=options) as result:
            async for chunk in render(result.mappings()):
                yield chunk
