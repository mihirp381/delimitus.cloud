"""Check an audit export offline (SSC-012, decision 012, GA-3.5): no API, no token.

Each JSON-lines row carries ``prev_hash`` and ``hash`` in hex and the ``canonical`` bytes in
base64. For each row, in file order: its seq follows the previous row's (``missing``), its
``prev_hash`` is the previous row's hash (``prev_link``), ``sha256(prev_hash || canonical)`` is
its hash (``hash``), and the canonical bytes are ssc-audit-v1 JSON saying what the row says
(``fields``). The first row links to genesis (32 zero bytes) when it is seq 1; an export that
starts later, a filtered one, is checked from its first row, and the report says so.

The checks match the operator's ``python -m ssc_control.audit verify``, less the head and the
IP address, which the export leaves out. To check the whole chain, export with no filters.
"""

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

type BreakCause = Literal["unreadable", "missing", "prev_link", "hash", "fields"]

GENESIS_HASH: Final = bytes(32)
V1_KEYS: Final = frozenset(
    {"org_id", "seq", "at", "action", "actor", "target", "before", "after", "policy_decision_id"}
)
_ACTOR_KEYS: Final = frozenset({"kind", "id", "via_agent", "client_id", "ip"})
_TARGET_KEYS: Final = frozenset({"kind", "id"})


@dataclass(frozen=True, slots=True)
class ChainReport:
    ok: bool
    checked: int
    org_id: str | None
    first_seq: int | None
    last_seq: int | None
    last_hash: str | None
    from_genesis: bool
    broken_line: int | None = None
    broken_seq: int | None = None
    cause: BreakCause | None = None


def canonical_bytes(fields: dict[str, Any]) -> bytes:
    """Sorted keys, no whitespace, UTF-8: the form the control plane hashes."""
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _same(a: object, b: object) -> bool:
    """Equal and of the same JSON type (``true`` is not ``1``)."""
    return type(a) is type(b) and a == b


def _same_json(a: object, b: object) -> bool:
    return canonical_bytes({"v": a}) == canonical_bytes({"v": b})


def _same_at(a: object, b: object) -> bool:
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    try:
        left, right = datetime.fromisoformat(a), datetime.fromisoformat(b)
    except ValueError:
        return False
    return left.utcoffset() is not None and right.utcoffset() is not None and left == right


def _object(value: object, keys: frozenset[str]) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    obj = cast(dict[str, object], value)
    return obj if obj.keys() == keys else None


def _fields_match(raw: bytes, row: dict[str, Any]) -> str | None:
    """The canonical document's org when it is v1 JSON describing exactly ``row``, else None."""
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        return None
    doc = _object(parsed, V1_KEYS)
    if doc is None or canonical_bytes(doc) != raw:
        return None
    actor = _object(doc["actor"], _ACTOR_KEYS)
    target = _object(doc["target"], _TARGET_KEYS)
    shown_actor = row.get("actor")
    shown_target = row.get("target")
    if actor is None or target is None or not isinstance(doc["org_id"], str):
        return None
    if not isinstance(shown_actor, dict) or not isinstance(shown_target, dict):
        return None
    shown_actor = cast(dict[str, object], shown_actor)
    shown_target = cast(dict[str, object], shown_target)
    same = (
        _same(doc["seq"], row.get("seq"))
        and _same_at(doc["at"], row.get("at"))
        and _same(doc["action"], row.get("action"))
        and _same(actor["kind"], shown_actor.get("kind"))
        and _same(actor["id"], shown_actor.get("id"))
        and _same(actor["via_agent"], shown_actor.get("via_agent"))
        and _same(actor["client_id"], shown_actor.get("client_id"))
        and _same(target["kind"], shown_target.get("kind"))
        and _same(target["id"], shown_target.get("id"))
        and _same_json(doc["before"], row.get("before"))
        and _same_json(doc["after"], row.get("after"))
        and _same(doc["policy_decision_id"], row.get("policy_decision_id"))
    )
    return doc["org_id"] if same else None


def _row(line: str) -> tuple[dict[str, Any], bytes, bytes, bytes] | None:
    """The row, its prev_hash, hash and canonical bytes; None when the line is not one."""
    try:
        parsed: object = json.loads(line)
        if not isinstance(parsed, dict):
            return None
        row = cast(dict[str, Any], parsed)
        prev, digest = bytes.fromhex(row["prev_hash"]), bytes.fromhex(row["hash"])
        canonical = base64.b64decode(row["canonical"], validate=True)
    except ValueError, KeyError, TypeError, binascii.Error:
        return None
    if len(prev) != len(GENESIS_HASH) or len(digest) != len(GENESIS_HASH):
        return None
    if not isinstance(row.get("seq"), int) or isinstance(row.get("seq"), bool):
        return None
    return row, prev, digest, canonical


def check_lines(lines: Iterable[str]) -> ChainReport:
    """Walk the export's rows in order and stop at the first that fails a check."""
    org_id: str | None = None
    first_seq: int | None = None
    last_seq: int | None = None
    last_hash: bytes | None = None
    from_genesis = False
    checked = 0

    def report(broken: tuple[int, int | None, BreakCause] | None = None) -> ChainReport:
        return ChainReport(
            ok=broken is None,
            checked=checked,
            org_id=org_id,
            first_seq=first_seq,
            last_seq=last_seq,
            last_hash=None if last_hash is None else last_hash.hex(),
            from_genesis=from_genesis,
            broken_line=None if broken is None else broken[0],
            broken_seq=None if broken is None else broken[1],
            cause=None if broken is None else broken[2],
        )

    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        parsed = _row(line)
        if parsed is None:
            return report((number, None, "unreadable"))
        row, prev, digest, canonical = parsed
        seq: int = row["seq"]
        if last_seq is None:
            first_seq, from_genesis = seq, seq == 1
        cause = _link(seq, prev, last_seq, last_hash)
        if cause is None and hashlib.sha256(prev + canonical).digest() != digest:
            cause = "hash"
        row_org = None if cause is not None else _fields_match(canonical, row)
        if cause is None and (row_org is None or org_id not in {None, row_org}):
            cause = "fields"
        if cause is not None:
            expected = seq if last_seq is None or cause != "missing" else last_seq + 1
            return report((number, expected, cause))
        org_id, last_seq, last_hash, checked = row_org, seq, digest, checked + 1
    return report()


def _link(
    seq: int, prev: bytes, last_seq: int | None, last_hash: bytes | None
) -> BreakCause | None:
    """Whether the row follows the one before it, or genesis when it is the first and seq 1."""
    if last_seq is None:
        return "prev_link" if seq == 1 and prev != GENESIS_HASH else None
    if seq != last_seq + 1:
        return "missing"
    return "prev_link" if prev != last_hash else None
