"""Collect bundle objects nobody can use (SSC-014, decision 015).

``collect_org`` walks ``bundles/{org}/`` and deletes an object older than ``GRACE`` whose bundle
row is still ``pending`` and older than ``GRACE`` too (the row stays: asking again answers a
fresh upload URL), or that no row names (an orphan). The object of a ``stored`` bundle is never
deleted, nor any object whose digest a release of that app carries, whatever its row says, nor
a key that is not a bundle key.

Each decision and its delete share one transaction holding the key's advisory lock
``(15, hashtext(key))``, which ``create_bundle`` takes before it records a digest, and the row
``FOR UPDATE``, which ``complete`` takes before it reads the object. The collector never waits:
a key or row another transaction holds is left for the next run.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_control.db.bind import bound_org
from ssc_control.db.orgs import all_org_ids
from ssc_control.deploy.bundles import DIGEST_PREFIX
from ssc_shared.blobstore import BlobError, BlobStore

log = logging.getLogger(__name__)

GRACE: Final = timedelta(hours=24)
LOCK_CLASS: Final = 15
"""First key of a bundle key's advisory lock; the second is ``hashtext(key)``."""

_BUNDLE_KEY = re.compile(r"bundles/([^/]+)/([^/]+)/sha256/([0-9a-f]{64})\.tar\.gz")
_LOCK_KEY = text("select pg_advisory_xact_lock(:cls, hashtext(:key))")
_TRY_LOCK_KEY = text("select pg_try_advisory_xact_lock(:cls, hashtext(:key))")
_RELEASED = text(
    "select exists (select 1 from ssc.release "
    "where org_id = :org and app_id = :app and source_digest = :digest)"
)
_ROW = text("select id from ssc.bundle where org_id = :org and app_id = :app and digest = :digest")
_LOCK_ROW = text(
    "select state, created_at from ssc.bundle where org_id = :org and id = :id "
    "for update skip locked"
)

type _Verdict = Literal["delete", "keep", "busy"]


class BundleGcError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Collected:
    deleted: int = 0
    kept: int = 0
    busy: int = 0

    def __add__(self, other: Collected) -> Collected:
        return Collected(
            self.deleted + other.deleted, self.kept + other.kept, self.busy + other.busy
        )


async def lock_bundle_key(conn: AsyncConnection, key: str) -> None:
    """Wait for the key's advisory lock, held to the end of ``conn``'s transaction."""
    await conn.execute(_LOCK_KEY, {"cls": LOCK_CLASS, "key": key})


async def collect_all(engine: AsyncEngine, store: BlobStore, *, now: datetime) -> Collected:
    """``collect_org`` for every org in ``org_index``. One org's failure is logged and the
    others are collected; then ``BundleGcError`` is raised."""
    total, failed = Collected(), 0
    for org_id in await all_org_ids(engine):
        try:
            total += await collect_org(engine, store, org_id, now=now)
        except DBAPIError, BlobError, OSError:
            log.exception("bundle collection failed for one org", extra={"org_id": org_id})
            failed += 1
    log.info("bundles collected", extra={"deleted": total.deleted, "busy": total.busy})
    if failed:
        raise BundleGcError(f"bundle collection failed for {failed} orgs")
    return total


async def collect_org(
    engine: AsyncEngine, store: BlobStore, org_id: str, *, now: datetime
) -> Collected:
    cutoff = now - GRACE
    counts = {"delete": 0, "keep": 0, "busy": 0}
    async for info in store.list(f"bundles/{org_id}/"):
        verdict: _Verdict = "keep"
        if info.created_at < cutoff:
            async with bound_org(engine, org_id) as conn:
                verdict = await _verdict(conn, org_id, info.key, cutoff)
                if verdict == "delete":
                    verdict = await _delete_if_stale(store, info.key, cutoff)
        counts[verdict] += 1
    return Collected(counts["delete"], counts["keep"], counts["busy"])


async def _verdict(conn: AsyncConnection, org_id: str, key: str, cutoff: datetime) -> _Verdict:
    match = _BUNDLE_KEY.fullmatch(key)
    if match is None or match[1] != org_id:
        return "keep"
    if not (await conn.execute(_TRY_LOCK_KEY, {"cls": LOCK_CLASS, "key": key})).scalar():
        return "busy"
    params = {"org": org_id, "app": match[2], "digest": DIGEST_PREFIX + match[3]}
    if (await conn.execute(_RELEASED, params)).scalar():
        return "keep"
    row_id = (await conn.execute(_ROW, params)).scalar()
    if row_id is None:
        return "delete"
    row = (await conn.execute(_LOCK_ROW, {"org": org_id, "id": row_id})).first()
    if row is None:
        return "busy"
    return "delete" if row.state == "pending" and row.created_at < cutoff else "keep"


async def _delete_if_stale(store: BlobStore, key: str, cutoff: datetime) -> _Verdict:
    """Delete ``key`` unless it was replaced since it was listed."""
    info = await store.stat(key)
    if info is None or info.created_at >= cutoff:
        return "keep"
    await store.delete(key)
    log.info("bundle object deleted", extra={"key": key})
    return "delete"
