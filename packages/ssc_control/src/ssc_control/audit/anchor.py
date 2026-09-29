"""Anchor an org's audit chain in the blob store (SSC-012, decision 012).

An anchor is the org's head ``(seq, hash)`` at one moment, stored twice: as a canonical
``ssc-audit-anchor/v1`` object under ``audit-anchors/<org>/`` and as a row in
``ssc.audit_anchor``. The object is the witness: whoever rewrites the log and recomputes every
hash cannot make an earlier object agree with the new chain.

``write_anchor`` holds the org's anchor lock (advisory class 12), reads the head together with
the database clock, and writes the object before the row, so a committed row always has its
object and ``anchored_at`` is never earlier than the head it names. An object is never replaced:
one already under the key (a commit that failed, a timeline a restore discarded) is adopted when
it agrees with the chain and refused when it does not.

``check_anchors`` compares the chain with every row and every object. A restore anchor, written
by ``reanchor`` after a point-in-time restore to ``restored_to``, supersedes the anchors written
between ``restored_to`` and itself: they witnessed events the restore lost. ``verify_anchors``
lists the bucket before it opens its read snapshot, so every listed object's head is in that
snapshot, then fetches the objects of rows committed after the listing.
"""

import asyncio
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Literal, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_control.audit.chain import GENESIS_HASH, Actor, NewEvent, append_event, canonical_bytes
from ssc_control.audit.verify import VerifyReport, verify
from ssc_control.db.bind import bound_org
from ssc_shared.blobstore import BlobNotFoundError, BlobStore

type Reason = Literal["daily", "restore"]
type AnchorCause = Literal["mismatch", "ahead", "missing", "differs", "malformed", "stale"]

FORMAT: Final = "ssc-audit-anchor/v1"
PREFIX: Final = "audit-anchors"
LOCK_CLASS: Final = 12
"""First key of the org's anchor advisory lock; the second is ``hashtext(org_id)``."""
CONTENT_TYPE: Final = "application/json"
PUT_TIMEOUT_SECONDS: Final = 20.0
MAX_OBJECT_BYTES: Final = 4096
STALE_AFTER: Final = timedelta(hours=36)
"""A day's anchor is due at 00:05 UTC; none for 36 hours means the job is failing."""
REANCHOR_ACTOR: Final = Actor(ActorKind.OPERATOR, "system:reanchor")
_DOC_KEYS: Final = frozenset(
    {"format", "org_id", "seq", "hash", "anchored_at", "reason", "restored_to"}
)
_HEX_HASH: Final = re.compile(r"[0-9a-f]{64}")
_REF: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:#/-]{0,127}")
_NEVER: Final = datetime.max.replace(tzinfo=UTC)

_LOCK_TIMEOUT = text("select set_config('lock_timeout', '30s', true)")
_LOCK = text("select pg_advisory_xact_lock(:cls, hashtext(:org))")
_HEAD_NOW = text("select seq, hash, clock_timestamp() from ssc.audit_head where org_id = :org")
_HASHES = text("select seq, hash from ssc.audit_event where org_id = :org and seq = any(:seqs)")
_ROWS = text(
    "select org_id, seq, hash, anchored_at, reason, restored_to, object_key "
    "from ssc.audit_anchor where org_id = :org order by anchored_at"
)
_ROW_AT = text(
    "select org_id, seq, hash, anchored_at, reason, restored_to, object_key "
    "from ssc.audit_anchor where org_id = :org and object_key = :key"
)
_RESTORES = text(
    "select org_id, seq, hash, anchored_at, reason, restored_to, object_key "
    "from ssc.audit_anchor where org_id = :org and reason = 'restore' order by anchored_at"
)
_INSERT = text(
    "insert into ssc.audit_anchor "
    "(org_id, seq, hash, anchored_at, reason, restored_to, object_key) "
    "values (:org_id, :seq, :hash, :anchored_at, :reason, :restored_to, :object_key)"
)
_CREATED = text("select created_at from ssc.org where id = :org")


class AnchorError(RuntimeError):
    """An anchor that cannot be written."""


class AnchorFormatError(AnchorError, ValueError):
    """An object under ``audit-anchors/<org>/`` that is not a canonical anchor of that org."""


class AnchorConflictError(AnchorError):
    """An object already under the key disagrees with the chain; it is never replaced."""


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True, kw_only=True)
class Anchor:
    org_id: str
    seq: int
    hash: bytes
    anchored_at: datetime
    reason: Reason
    restored_to: datetime | None
    object_key: str

    def document(self) -> bytes:
        """The object's bytes: canonical JSON, as for audit rows (decision 012)."""
        return canonical_bytes(
            {
                "format": FORMAT,
                "org_id": self.org_id,
                "seq": self.seq,
                "hash": self.hash.hex(),
                "anchored_at": _iso(self.anchored_at),
                "reason": self.reason,
                "restored_to": None if self.restored_to is None else _iso(self.restored_to),
            }
        )

    def params(self) -> dict[str, object]:
        return {
            "org_id": self.org_id,
            "seq": self.seq,
            "hash": self.hash,
            "anchored_at": self.anchored_at,
            "reason": self.reason,
            "restored_to": self.restored_to,
            "object_key": self.object_key,
        }


@dataclass(frozen=True, slots=True)
class AnchorProblem:
    cause: AnchorCause
    key: str | None
    seq: int | None


@dataclass(frozen=True, slots=True)
class AnchorReport:
    checked: int
    superseded: int
    newest: Anchor | None
    problems: tuple[AnchorProblem, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


def org_prefix(org_id: str) -> str:
    return f"{PREFIX}/{org_id}/"


def daily_key(org_id: str, day: date) -> str:
    return f"{org_prefix(org_id)}{day.isoformat()}.json"


def restore_key(org_id: str, anchored_at: datetime) -> str:
    return f"{org_prefix(org_id)}restore-{anchored_at.astimezone(UTC):%Y%m%d-%H%M%S-%f}.json"


def _time(value: object, key: str) -> datetime:
    if not isinstance(value, str):
        raise AnchorFormatError(f"{key}: a time must be a string")
    try:
        at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AnchorFormatError(f"{key}: bad time {value!r}") from exc
    if at.utcoffset() is None:
        raise AnchorFormatError(f"{key}: a time must carry a UTC offset")
    return at


def _reason(value: object, key: str) -> Reason:
    if value == "daily":
        return "daily"
    if value == "restore":
        return "restore"
    raise AnchorFormatError(f"{key}: unknown reason {value!r}")


def parse_anchor(key: str, raw: bytes, *, org_id: str) -> Anchor:
    """The anchor in ``raw``, stored under ``key``; strict: canonical bytes of this org only."""
    try:
        parsed: object = json.loads(raw)
    except ValueError as exc:
        raise AnchorFormatError(f"{key}: not JSON") from exc
    if not isinstance(parsed, dict) or cast(dict[str, object], parsed).keys() != _DOC_KEYS:
        raise AnchorFormatError(f"{key}: not an {FORMAT} document")
    doc = cast(dict[str, object], parsed)
    seq, digest, restored_to = doc["seq"], doc["hash"], doc["restored_to"]
    if doc["format"] != FORMAT or doc["org_id"] != org_id:
        raise AnchorFormatError(f"{key}: not an {FORMAT} anchor of {org_id}")
    if type(seq) is not int or seq < 0:
        raise AnchorFormatError(f"{key}: seq must be a non-negative integer")
    if not isinstance(digest, str) or not _HEX_HASH.fullmatch(digest):
        raise AnchorFormatError(f"{key}: hash must be 64 lowercase hex characters")
    anchor = Anchor(
        org_id=org_id,
        seq=seq,
        hash=bytes.fromhex(digest),
        anchored_at=_time(doc["anchored_at"], key),
        reason=_reason(doc["reason"], key),
        restored_to=None if restored_to is None else _time(restored_to, key),
        object_key=key,
    )
    if (anchor.reason == "restore") != (anchor.restored_to is not None):
        raise AnchorFormatError(f"{key}: restored_to belongs to restore anchors only")
    if anchor.document() != raw:
        raise AnchorFormatError(f"{key}: not in canonical form")
    return anchor


def _from_row(row: Mapping[Any, Any]) -> Anchor:
    return Anchor(
        org_id=str(row["org_id"]),
        seq=int(row["seq"]),
        hash=bytes(row["hash"]),
        anchored_at=row["anchored_at"],
        reason="restore" if row["reason"] == "restore" else "daily",
        restored_to=row["restored_to"],
        object_key=str(row["object_key"]),
    )


async def _read(blob: BlobStore, key: str) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in blob.get(key):
        size += len(chunk)
        if size > MAX_OBJECT_BYTES:
            raise AnchorFormatError(f"{key}: larger than {MAX_OBJECT_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def read_anchor(blob: BlobStore, org_id: str, key: str) -> Anchor | None:
    """The anchor stored under ``key``; None when there is no object."""
    try:
        raw = await _read(blob, key)
    except BlobNotFoundError:
        return None
    return parse_anchor(key, raw, org_id=org_id)


async def read_anchors(
    blob: BlobStore, org_id: str, keys: Iterable[str] | None = None
) -> dict[str, Anchor | None]:
    """Each object under the org's prefix (or each of ``keys`` that exists), mapped to its
    anchor, or to None when it is malformed."""
    if keys is None:
        keys = [info.key async for info in blob.list(org_prefix(org_id))]
    found: dict[str, Anchor | None] = {}
    for key in keys:
        try:
            anchor = await read_anchor(blob, org_id, key)
        except AnchorFormatError:
            found[key] = None
            continue
        if anchor is not None:
            found[key] = anchor
    return found


async def chain_hashes(conn: AsyncConnection, org_id: str, seqs: Iterable[int]) -> dict[int, bytes]:
    """The stored hash at each of ``seqs`` that exists; seq 0 is the genesis hash."""
    wanted = sorted({seq for seq in seqs if seq > 0})
    hashes = {0: GENESIS_HASH}
    if wanted:
        result = await conn.execute(_HASHES, {"org": org_id, "seqs": wanted})
        hashes.update((int(seq), bytes(digest)) for seq, digest in result)
    return hashes


def _supersedes(restore: Anchor, anchor: Anchor) -> bool:
    start = restore.restored_to
    return start is not None and start < anchor.anchored_at < restore.anchored_at


async def _put(blob: BlobStore, anchor: Anchor) -> None:
    body = anchor.document()
    sha = hashlib.sha256(body).hexdigest()
    async with asyncio.timeout(PUT_TIMEOUT_SECONDS):
        await blob.put(
            anchor.object_key, body, content_type=CONTENT_TYPE, size=len(body), sha256=sha
        )


async def _adopt_or_cover(conn: AsyncConnection, found: Anchor, head_seq: int) -> Anchor | None:
    """None to adopt ``found``; the restore anchor that covers it; or refuse."""
    hashes = await chain_hashes(conn, found.org_id, [found.seq])
    if found.seq <= head_seq and hashes.get(found.seq) == found.hash:
        return None
    restores = (await conn.execute(_RESTORES, {"org": found.org_id})).mappings()
    for restore in map(_from_row, restores):
        if _supersedes(restore, found):
            return restore
    raise AnchorConflictError(
        f"{found.object_key} (seq {found.seq}) disagrees with the chain: a restore that was not "
        "re-anchored, or tampering; see decision 012"
    )


async def write_anchor(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    org_id: str,
    blob: BlobStore,
    reason: Reason,
    *,
    day: date | None = None,
    restored_to: datetime | None = None,
) -> Anchor:
    """Anchor the org's head in ``conn``'s open, org-bound transaction and return the anchor.

    A daily anchor is keyed by ``day`` (today in UTC by default) and written once: a later call
    returns the first. A restore anchor needs ``restored_to`` and gets a key of its own. An
    object under the key that the chain contradicts raises :class:`AnchorConflictError`, unless
    a later restore anchor covers it; that restore anchor is returned instead."""
    if (reason == "restore") != (restored_to is not None):
        raise ValueError("a restore anchor, and only a restore anchor, has restored_to")
    if restored_to is not None and restored_to.utcoffset() is None:
        raise ValueError("restored_to must carry a UTC offset")
    await conn.execute(_LOCK_TIMEOUT)
    await conn.execute(_LOCK, {"cls": LOCK_CLASS, "org": org_id})
    head = (await conn.execute(_HEAD_NOW, {"org": org_id})).first()
    if head is None:
        raise AnchorError(f"org {org_id} has no audit head")
    head_seq, anchored_at = int(head[0]), cast(datetime, head[2])
    if reason == "daily":
        key = daily_key(org_id, day or anchored_at.astimezone(UTC).date())
    else:
        key = restore_key(org_id, anchored_at)
    done = (await conn.execute(_ROW_AT, {"org": org_id, "key": key})).mappings().first()
    if done is not None:
        return _from_row(done)
    anchor = Anchor(
        org_id=org_id,
        seq=head_seq,
        hash=bytes(head[1]),
        anchored_at=anchored_at,
        reason=reason,
        restored_to=restored_to,
        object_key=key,
    )
    found = await read_anchor(blob, org_id, key)
    if found is None:
        await _put(blob, anchor)
    elif found.reason != reason:
        raise AnchorConflictError(f"{key} holds a {found.reason} anchor")
    else:
        covering = await _adopt_or_cover(conn, found, head_seq)
        if covering is not None:
            return covering
        anchor = found
    await conn.execute(_INSERT, anchor.params())
    return anchor


def check_anchors(  # noqa: PLR0913  (keyword-only)
    *,
    head_seq: int | None,
    hashes: Mapping[int, bytes],
    rows: Sequence[Anchor],
    objects: Mapping[str, Anchor | None],
    since: datetime,
    now: datetime,
    restoring_to: datetime | None = None,
) -> AnchorReport:
    """Compare rows with objects, and both with the chain (``hashes`` by seq, up to ``head_seq``).

    ``objects`` maps each key found in the bucket to its anchor, or None when it is malformed.
    ``since`` is when the org began, for an org with no anchor yet. ``restoring_to`` supersedes
    the anchors written after it, as the restore anchor about to be written will."""
    rows_by_key = {row.object_key: row for row in rows}
    witnesses = [anchor for anchor in objects.values() if anchor is not None]
    windows = [(a.restored_to, a.anchored_at) for a in witnesses if a.restored_to is not None]
    if restoring_to is not None:
        windows.append((restoring_to, _NEVER))
    problems: set[AnchorProblem] = set()
    anchors: set[Anchor] = set()
    for key in rows_by_key.keys() | objects.keys():
        row, found = rows_by_key.get(key), objects.get(key)
        if key in objects and found is None:
            problems.add(AnchorProblem("malformed", key, None if row is None else row.seq))
        elif key not in objects:
            problems.add(AnchorProblem("missing", key, None if row is None else row.seq))
        elif row is not None and row != found:
            problems.add(AnchorProblem("differs", key, row.seq))
        anchors.update(anchor for anchor in (row, found) if anchor is not None)
    checked = superseded = 0
    for anchor in anchors:
        if any(start < anchor.anchored_at < end for start, end in windows):
            superseded += 1
            continue
        checked += 1
        if head_seq is None or anchor.seq > head_seq:
            problems.add(AnchorProblem("ahead", anchor.object_key, anchor.seq))
        elif hashes.get(anchor.seq) != anchor.hash:
            problems.add(AnchorProblem("mismatch", anchor.object_key, anchor.seq))
    newest = max(witnesses, key=lambda anchor: anchor.anchored_at, default=None)
    if now - (since if newest is None else newest.anchored_at) > STALE_AFTER:
        problems.add(
            AnchorProblem(
                "stale",
                None if newest is None else newest.object_key,
                None if newest is None else newest.seq,
            )
        )
    ordered = sorted(problems, key=lambda p: (p.key or "", p.cause, -1 if p.seq is None else p.seq))
    return AnchorReport(checked, superseded, newest, tuple(ordered))


async def verify_anchors(
    engine: AsyncEngine,
    org_id: str,
    blob: BlobStore,
    *,
    now: datetime,
    restoring_to: datetime | None = None,
) -> tuple[VerifyReport, AnchorReport]:
    """The chain and its anchors, from one REPEATABLE READ snapshot taken after the listing."""
    objects = await read_anchors(blob, org_id)
    snapshot = engine.execution_options(isolation_level="REPEATABLE READ")
    async with bound_org(snapshot, org_id) as conn:
        chain = await verify(conn, org_id)
        rows = [_from_row(row) for row in (await conn.execute(_ROWS, {"org": org_id})).mappings()]
        late = [row.object_key for row in rows if row.object_key not in objects]
        objects |= await read_anchors(blob, org_id, late)
        seqs = [row.seq for row in rows] + [a.seq for a in objects.values() if a is not None]
        hashes = await chain_hashes(conn, org_id, seqs)
        since = cast(datetime, (await conn.execute(_CREATED, {"org": org_id})).scalar_one())
    report = check_anchors(
        head_seq=chain.head_seq,
        hashes=hashes,
        rows=rows,
        objects=objects,
        since=since,
        now=now,
        restoring_to=restoring_to,
    )
    return chain, report


class ReanchorRefusedError(AnchorError):
    """The restored chain fails, or an anchor written before the restore point disagrees with it:
    tampering, or the wrong restore point. Nothing was written."""

    def __init__(self, chain: VerifyReport, anchors: AnchorReport) -> None:
        super().__init__("refusing to re-anchor: the restored chain does not hold")
        self.chain = chain
        self.anchors = anchors


async def reanchor(  # noqa: PLR0913  (keyword-only)
    engine: AsyncEngine,
    org_id: str,
    blob: BlobStore,
    *,
    restored_to: datetime,
    ref: str,
    now: datetime,
) -> Anchor:
    """Step 4 of decision 012's restore procedure, for a database restored to ``restored_to``.

    Refuses unless the chain verifies and every anchor written before ``restored_to`` agrees with
    it (anchors written after it witnessed events the restore lost). Then, in one transaction,
    anchors the head as a restore anchor and appends ``audit.reanchored`` naming the ticket
    ``ref``. Run it before the API and the worker serve again."""
    if restored_to.utcoffset() is None or restored_to > now:
        raise ValueError("restored_to must carry a UTC offset and be in the past")
    if not _REF.fullmatch(ref):
        raise ValueError("ref must be a ticket reference: letters, digits and ._:#/- only")
    chain, anchors = await verify_anchors(engine, org_id, blob, now=now, restoring_to=restored_to)
    if not chain.ok or any(problem.cause != "stale" for problem in anchors.problems):
        raise ReanchorRefusedError(chain, anchors)
    prior = anchors.newest
    async with bound_org(engine, org_id) as conn:
        anchor = await write_anchor(conn, org_id, blob, "restore", restored_to=restored_to)
        await append_event(
            conn,
            NewEvent(
                org_id=org_id,
                action=AuditAction.AUDIT_REANCHORED,
                actor=REANCHOR_ACTOR,
                target_kind="audit_anchor",
                target_id=anchor.object_key,
                after={
                    "restored_to": _iso(restored_to),
                    "prior_anchor_seq": None if prior is None else prior.seq,
                    "head_seq": anchor.seq,
                    "ref": ref,
                },
            ),
        )
    return anchor
