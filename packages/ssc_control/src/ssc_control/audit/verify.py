"""Walk one org's chain from genesis and name the first broken link.

For each row in ``seq`` order: the seq follows the previous one (``missing``), ``prev_hash`` is the
previous row's hash (``prev_link``), ``sha256(prev_hash || canonical)`` is the stored hash
(``hash``), and the canonical bytes are v1 canonical JSON that say exactly what the row's columns
say (``fields``). Then ``audit_head`` must point at the last row: a head beyond it means the tail
is ``missing``, any other difference is ``head`` at the last row's seq.

Run it inside a REPEATABLE READ transaction when appends may be running, so the head and the rows
come from one snapshot.
"""

import hashlib
import ipaddress
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_control.audit.chain import GENESIS_HASH, canonical_bytes

type BreakCause = Literal["missing", "prev_link", "hash", "fields", "head"]

BATCH: Final = 1000
V1_KEYS: Final = frozenset(
    {"org_id", "seq", "at", "action", "actor", "target", "before", "after", "policy_decision_id"}
)
_ACTOR_KEYS: Final = frozenset({"kind", "id", "via_agent", "client_id", "ip"})
_TARGET_KEYS: Final = frozenset({"kind", "id"})

_SELECT_HEAD = text("select seq, hash from ssc.audit_head where org_id = :org")
_SELECT_ROWS = text(
    "select seq, at, action, actor_kind, actor_id, actor_via_agent, actor_client_id, "
    "actor_ip::text as actor_ip, target_kind, target_id, before, after, policy_decision_id, "
    "canonical, prev_hash, hash from ssc.audit_event where org_id = :org order by seq"
)


@dataclass(frozen=True, slots=True)
class BrokenLink:
    seq: int
    cause: BreakCause


@dataclass(frozen=True, slots=True)
class VerifyReport:
    ok: bool
    checked: int
    head_seq: int | None
    first_broken: BrokenLink | None


def _same(a: object, b: object) -> bool:
    """Equal and of the same JSON type (``true`` is not ``1``)."""
    return type(a) is type(b) and a == b


def _same_json(a: object, b: object) -> bool:
    return canonical_bytes({"v": a}) == canonical_bytes({"v": b})


def _same_ip(canonical: object, column: str | None) -> bool:
    if canonical is None or column is None:
        return canonical is None and column is None
    if not isinstance(canonical, str):
        return False
    try:
        return ipaddress.ip_interface(canonical) == ipaddress.ip_interface(column)
    except ValueError:
        return False


def _same_at(canonical: object, column: datetime) -> bool:
    if not isinstance(canonical, str):
        return False
    try:
        at = datetime.fromisoformat(canonical)
    except ValueError:
        return False
    return at.utcoffset() is not None and at == column


def _object(value: object, keys: frozenset[str]) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    obj = cast(dict[str, object], value)
    return obj if obj.keys() == keys else None


def fields_match(org_id: str, row: Mapping[Any, Any]) -> bool:
    """True when ``row["canonical"]`` is v1 canonical JSON describing exactly this row."""
    raw = bytes(row["canonical"])
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        return False
    doc = _object(parsed, V1_KEYS)
    if doc is None or canonical_bytes(doc) != raw:
        return False
    actor = _object(doc["actor"], _ACTOR_KEYS)
    target = _object(doc["target"], _TARGET_KEYS)
    return (
        actor is not None
        and target is not None
        and _same(doc["org_id"], org_id)
        and _same(doc["seq"], row["seq"])
        and _same_at(doc["at"], row["at"])
        and _same(doc["action"], row["action"])
        and _same(actor["kind"], row["actor_kind"])
        and _same(actor["id"], row["actor_id"])
        and _same(actor["via_agent"], row["actor_via_agent"])
        and _same(actor["client_id"], row["actor_client_id"])
        and _same_ip(actor["ip"], row["actor_ip"])
        and _same(target["kind"], row["target_kind"])
        and _same(target["id"], row["target_id"])
        and _same_json(doc["before"], row["before"])
        and _same_json(doc["after"], row["after"])
        and _same(doc["policy_decision_id"], row["policy_decision_id"])
    )


def check_row(
    org_id: str, row: Mapping[Any, Any], expected_seq: int, prev_hash: bytes
) -> BreakCause | None:
    """The first check ``row`` fails as the successor of ``prev_hash`` at ``expected_seq``."""
    if row["seq"] != expected_seq:
        return "missing"
    stored_prev = bytes(row["prev_hash"])
    if stored_prev != prev_hash:
        return "prev_link"
    if hashlib.sha256(stored_prev + bytes(row["canonical"])).digest() != bytes(row["hash"]):
        return "hash"
    if not fields_match(org_id, row):
        return "fields"
    return None


async def verify(conn: AsyncConnection, org_id: str) -> VerifyReport:
    """Verify ``org_id``'s chain and head inside ``conn``'s open, org-bound transaction."""
    head = (await conn.execute(_SELECT_HEAD, {"org": org_id})).first()
    head_seq = None if head is None else int(head[0])
    last_seq, last_hash, checked = 0, GENESIS_HASH, 0

    def broken(seq: int, cause: BreakCause) -> VerifyReport:
        return VerifyReport(False, checked, head_seq, BrokenLink(seq, cause))

    options = {"yield_per": BATCH}
    async with conn.stream(_SELECT_ROWS, {"org": org_id}, execution_options=options) as result:
        async for row in result.mappings():
            cause = check_row(org_id, row, last_seq + 1, last_hash)
            if cause is not None:
                return broken(last_seq + 1, cause)
            last_seq, last_hash, checked = int(row["seq"]), bytes(row["hash"]), checked + 1
    if head is None or head_seq is None:
        return broken(last_seq, "head")
    if head_seq > last_seq:
        return broken(last_seq + 1, "missing")
    if head_seq != last_seq or bytes(head[1]) != last_hash:
        return broken(last_seq, "head")
    return VerifyReport(True, checked, head_seq, None)
