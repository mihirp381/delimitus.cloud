"""Audit queries: filters, newest-first keyset pages, and the row shape search and export share.

Pages are keyed on ``seq``, which is unique per org and never reused, so a page boundary cannot
skip or repeat a row while appends continue. ``since`` is inclusive and ``until`` exclusive.
``actor_ip`` is never selected.
"""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import Select, column, select, table

from ssc_contracts.audit import ActorKind, AuditAction

_EVENT: Final = table(
    "audit_event",
    column("org_id"),
    column("seq"),
    column("at"),
    column("action"),
    column("actor_kind"),
    column("actor_id"),
    column("actor_via_agent"),
    column("actor_client_id"),
    column("target_kind"),
    column("target_id"),
    column("before"),
    column("after"),
    column("policy_decision_id"),
    column("canonical"),
    column("prev_hash"),
    column("hash"),
    schema="ssc",
)
_EQUALITY: Final = ("action", "actor_kind", "actor_id", "target_kind", "target_id")

type Row = Mapping[Any, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditFilters:
    since: datetime | None = None
    until: datetime | None = None
    action: AuditAction | None = None
    actor_kind: ActorKind | None = None
    actor_id: str | None = None
    target_kind: str | None = None
    target_id: str | None = None

    def as_view(self) -> dict[str, str]:
        """The filters that are set, as strings: the ``filters`` of an ``audit.exported`` row."""
        out: dict[str, str] = {}
        for f in fields(self):
            value: object = getattr(self, f.name)
            if isinstance(value, datetime):
                out[f.name] = value.isoformat()
            elif value is not None:
                out[f.name] = str(value)
        return out


def select_events(
    org_id: str,
    filters: AuditFilters,
    *,
    before_seq: int | None = None,
    limit: int | None = None,
    newest_first: bool = True,
) -> Select[Any]:
    """Rows of ``org_id`` matching ``filters``, ordered by ``seq``, below ``before_seq`` if set."""
    c = _EVENT.c
    query = select(*[col for col in c if col.name != "org_id"]).where(c.org_id == org_id)
    if filters.since is not None:
        query = query.where(c.at >= filters.since)
    if filters.until is not None:
        query = query.where(c.at < filters.until)
    for name in _EQUALITY:
        value: object = getattr(filters, name)
        if value is not None:
            query = query.where(c[name] == str(value))
    if before_seq is not None:
        query = query.where(c.seq < before_seq)
    query = query.order_by(c.seq.desc() if newest_first else c.seq.asc())
    return query if limit is None else query.limit(limit)


def utc_iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


def event_record(row: Row) -> dict[str, Any]:
    """One row as search and JSON-lines export show it: hashes in hex, ``at`` in UTC."""
    return {
        "seq": row["seq"],
        "at": utc_iso(row["at"]),
        "action": row["action"],
        "actor": {
            "kind": row["actor_kind"],
            "id": row["actor_id"],
            "via_agent": row["actor_via_agent"],
            "client_id": row["actor_client_id"],
        },
        "target": {"kind": row["target_kind"], "id": row["target_id"]},
        "before": row["before"],
        "after": row["after"],
        "policy_decision_id": row["policy_decision_id"],
        "prev_hash": bytes(row["prev_hash"]).hex(),
        "hash": bytes(row["hash"]).hex(),
    }
