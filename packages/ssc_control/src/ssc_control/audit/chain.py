"""Append one audit event inside the caller's transaction.

This is the writer the API's unit of work uses so that the change and its audit row commit or
roll back together. Appends are serialised per org by locking ``audit_head`` ``FOR UPDATE``;
the chain is ``hash = sha256(prev_hash || canonical)`` over deterministic JSON, starting from
:data:`GENESIS_HASH`. The constraints in ``audit_event`` (unique ``prev_hash`` and ``hash`` per
org) make a fork or a gap unstorable.

The canonical form is frozen as ssc-audit-v1 (decision 012): the nine keys below, sorted, no
whitespace, UTF-8 without ASCII escaping, ``at`` in ISO 8601 with its offset. It carries no
version key; a v2 would add ``"format"``. :mod:`ssc_control.audit.verify` reproduces it.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_control.audit.views import check_view

HASH_LENGTH: Final = 32
GENESIS_HASH: Final = bytes(HASH_LENGTH)


@dataclass(frozen=True, slots=True)
class Actor:
    kind: ActorKind
    id: str
    via_agent: bool = False
    client_id: str | None = None
    ip: str | None = None


@dataclass(frozen=True, slots=True)
class AppendedEvent:
    seq: int
    hash: bytes
    at: datetime


def canonical_bytes(fields: dict[str, Any]) -> bytes:
    """Sorted keys, no whitespace, UTF-8. Timestamps are ISO 8601 with offset."""
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


_LOCK_HEAD = text("select seq, hash from ssc.audit_head where org_id = :org for update")
_INSERT_EVENT = text(
    "insert into ssc.audit_event (org_id, seq, at, action, actor_kind, actor_id, actor_via_agent, "
    "actor_client_id, actor_ip, target_kind, target_id, before, after, policy_decision_id, "
    "canonical, prev_hash, hash) values (:org, :seq, :at, :action, :actor_kind, :actor_id, "
    ":via_agent, :client_id, cast(:ip as inet), :target_kind, :target_id, "
    "cast(:before as jsonb), cast(:after as jsonb), :policy, :canonical, :prev, :hash)"
)
_UPDATE_HEAD = text("update ssc.audit_head set seq = :seq, hash = :hash where org_id = :org")


@dataclass(frozen=True, slots=True, kw_only=True)
class NewEvent:
    org_id: str
    action: AuditAction
    actor: Actor
    target_kind: str
    target_id: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    policy_decision_id: str | None = None
    at: datetime | None = None


async def append_event(conn: AsyncConnection, event: NewEvent) -> AppendedEvent:
    """Append inside ``conn``'s open, org-bound transaction. Raises if the head is missing.

    ``before`` and ``after`` must fit the target kind's view (:func:`check_view`), and an
    explicit ``at`` must carry a UTC offset.
    """
    org_id, actor = event.org_id, event.actor
    check_view(event.target_kind, event.before)
    check_view(event.target_kind, event.after)
    if event.at is not None and event.at.utcoffset() is None:
        raise ValueError("audit timestamps must carry a UTC offset")
    head = (await conn.execute(_LOCK_HEAD, {"org": org_id})).first()
    if head is None:
        raise RuntimeError(f"audit_head missing for {org_id}; orgs are created with create_org()")
    prev_seq, prev_hash = int(head[0]), bytes(head[1])
    when = event.at or datetime.now(UTC)
    seq = prev_seq + 1
    canonical = canonical_bytes(
        {
            "org_id": org_id,
            "seq": seq,
            "at": when.isoformat(),
            "action": event.action.value,
            "actor": {
                "kind": actor.kind.value,
                "id": actor.id,
                "via_agent": actor.via_agent,
                "client_id": actor.client_id,
                "ip": actor.ip,
            },
            "target": {"kind": event.target_kind, "id": event.target_id},
            "before": event.before,
            "after": event.after,
            "policy_decision_id": event.policy_decision_id,
        }
    )
    digest = hashlib.sha256(prev_hash + canonical).digest()
    await conn.execute(
        _INSERT_EVENT,
        {
            "org": org_id,
            "seq": seq,
            "at": when,
            "action": event.action.value,
            "actor_kind": actor.kind.value,
            "actor_id": actor.id,
            "via_agent": actor.via_agent,
            "client_id": actor.client_id,
            "ip": actor.ip,
            "target_kind": event.target_kind,
            "target_id": event.target_id,
            "before": json.dumps(event.before) if event.before is not None else None,
            "after": json.dumps(event.after) if event.after is not None else None,
            "policy": event.policy_decision_id,
            "canonical": canonical,
            "prev": prev_hash,
            "hash": digest,
        },
    )
    await conn.execute(_UPDATE_HEAD, {"org": org_id, "seq": seq, "hash": digest})
    return AppendedEvent(seq=seq, hash=digest, at=when)
