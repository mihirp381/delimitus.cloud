"""The cell's copy of its org's access snapshot (SSC-013, decision 019).

The control plane publishes ``snapshots/<org_id>/v<version>-<sha12>.json`` and then moves
``snapshots/<org_id>/latest.json`` (``{"version", "key", "digest"}``) onto it. A cell service
polls the pointer every ``POLL_SECONDS``; when it names a newer version, the feed reads that
object, checks its sha256 against the pointer and hands it to the ``ViewHolder``. Any failure,
a missing pointer, a torn read or an invalid document, keeps the last good view.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Final

from ssc_shared.access import SnapshotInvalidError, ViewHolder
from ssc_shared.blobstore import BlobError, BlobNotFoundError, BlobStore

log = logging.getLogger(__name__)

POLL_SECONDS: Final = 2.0
POINTER_MAX_BYTES: Final = 1024
SNAPSHOT_MAX_BYTES: Final = 32 * 1024 * 1024
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")


def snapshot_prefix(org_id: str) -> str:
    return f"snapshots/{org_id}/"


def object_key(org_id: str, version: int, sha256_hex: str) -> str:
    return f"{snapshot_prefix(org_id)}v{version}-{sha256_hex[:12]}.json"


def latest_key(org_id: str) -> str:
    return f"{snapshot_prefix(org_id)}latest.json"


@dataclass(frozen=True, slots=True)
class Pointer:
    version: int
    key: str
    sha256: str


def parse_pointer(raw: bytes, org_id: str) -> Pointer:
    """``latest.json`` for ``org_id``; ``SnapshotInvalidError`` for anything else."""
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise SnapshotInvalidError("latest.json is not JSON") from exc
    if not isinstance(doc, dict) or set(doc) != {"version", "key", "digest"}:  # pyright: ignore[reportUnknownArgumentType]
        raise SnapshotInvalidError("latest.json needs exactly version, key and digest")
    version, key, digest = doc["version"], doc["key"], doc["digest"]  # pyright: ignore[reportUnknownVariableType]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise SnapshotInvalidError("latest.json version must be a positive integer")
    match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    if match is None:
        raise SnapshotInvalidError("latest.json digest must be sha256:<64 hex>")
    if key != object_key(org_id, version, match.group(1)):
        raise SnapshotInvalidError("latest.json names an object outside this org's version")
    return Pointer(version, object_key(org_id, version, match.group(1)), match.group(1))


async def _read(store: BlobStore, key: str, limit: int) -> bytes:
    body = bytearray()
    chunks: AsyncIterator[bytes] = store.get(key)
    async for chunk in chunks:
        body.extend(chunk)
        if len(body) > limit:
            raise SnapshotInvalidError(f"{key} is larger than {limit} bytes")
    return bytes(body)


class SnapshotFeed:
    """Keeps ``holder`` on the newest published snapshot of its org."""

    def __init__(
        self,
        store: BlobStore,
        holder: ViewHolder,
        *,
        interval: float = POLL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._holder = holder
        self._interval = interval
        self._monotonic = monotonic
        self.last_error: str | None = None
        self.last_applied_at: float | None = None
        self.last_ok_at: float | None = None
        self.failures = 0

    @property
    def version(self) -> int | None:
        view = self._holder.view
        return None if view is None else view.version

    async def poll_once(self) -> bool:
        """One poll; True when a newer version was applied. Never raises for a bad snapshot.
        A good poll confirms the view as of the moment it asked for ``latest.json``."""
        org_id = self._holder.org_id
        asked_at = self._monotonic()
        try:
            raw = await _read(self._store, latest_key(org_id), POINTER_MAX_BYTES)
        except BlobNotFoundError:
            self._fail("no snapshot published")
            return False
        except (BlobError, OSError, SnapshotInvalidError) as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
            return False
        try:
            pointer = parse_pointer(raw, org_id)
            current = self.version
            if current is not None and pointer.version <= current:
                self._ok(asked_at)
                return False
            body = await _read(self._store, pointer.key, SNAPSHOT_MAX_BYTES)
            if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), pointer.sha256):
                raise SnapshotInvalidError(f"{pointer.key} does not match latest.json's digest")
            applied = self._holder.apply(body)
        except (BlobError, OSError, SnapshotInvalidError) as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
            return False
        self._ok(asked_at)
        if applied:
            self.last_applied_at = self._monotonic()
            log.info("snapshot applied", extra={"org_id": org_id, "version": pointer.version})
        return applied

    async def run(self, stop: asyncio.Event) -> None:
        """Poll every ``interval`` seconds until ``stop`` is set."""
        while not stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    def fresh(self, max_age: float) -> bool:
        """True when a poll confirmed the current view within ``max_age`` seconds."""
        ok = self.last_ok_at
        return ok is not None and self._monotonic() - ok <= max_age

    def _ok(self, asked_at: float) -> None:
        self.last_ok_at = asked_at
        if self.failures:
            log.info("snapshot feed recovered", extra={"after_failures": self.failures})
        self.failures = 0
        self.last_error = None

    def _fail(self, error: str) -> None:
        if error != self.last_error:
            log.warning("snapshot feed kept the last good view", extra={"error": error})
        self.failures += 1
        self.last_error = error
