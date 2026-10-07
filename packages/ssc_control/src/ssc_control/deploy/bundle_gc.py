"""Collect bundle objects nobody can use (SSC-014, decision 015).

``collect_org`` walks ``bundles/{org}/`` in the org's store (its cell's bucket when cell stores
are configured, ``storage.org_bundle_store``) and deletes an object older than ``GRACE``:
  * whose bundle row is still ``pending`` and older than ``GRACE`` too (the row stays: asking
    again answers a fresh upload URL), or that no row names (an orphan);
  * whose bundle is ``stored`` but was stored more than ``RETENTION`` ago, with no release of
    that app on its digest and no build of it queued or running. Its row goes back to
    ``pending`` in the same transaction, so sending the same source again uploads it again.
Never an object whose digest a release of that app carries, whatever its row says (a promote
rebuilds from it, and a rollback may), nor a key that is not a bundle key. Every deletion is
logged with the org, app, digest and age. Offboarding deletes the cell project, and with it
every bundle that is left.

Each decision and its delete share one transaction holding the key's advisory lock
``(15, hashtext(key))``, which ``create_bundle`` takes before it records a digest, and the row
``FOR UPDATE``, which ``complete`` takes before it reads the object and a build start takes
``FOR SHARE`` before it queues. The collector never waits: a key or row another transaction
holds is left for the next run. A crash between an expired object's delete and the row's reset
leaves a ``stored`` row with no object; ``create_bundle`` finds that and resets the row itself.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_control.db.bind import bound_org
from ssc_control.db.orgs import all_org_ids
from ssc_control.deploy.bundles import BUNDLE_KEY, DIGEST_PREFIX
from ssc_control.storage import CellStores, org_bundle_store
from ssc_shared.blobstore import BlobError, BlobNotFoundError, BlobStore

log = logging.getLogger(__name__)

GRACE: Final = timedelta(hours=24)
RETENTION: Final = timedelta(days=7)
"""How long a stored bundle no release uses keeps its object."""
LOCK_CLASS: Final = 15
"""First key of a bundle key's advisory lock; the second is ``hashtext(key)``."""

_LOCK_KEY = text("select pg_advisory_xact_lock(:cls, hashtext(:key))")
_TRY_LOCK_KEY = text("select pg_try_advisory_xact_lock(:cls, hashtext(:key))")
_RELEASED = text(
    "select exists (select 1 from ssc.release "
    "where org_id = :org and app_id = :app and source_digest = :digest)"
)
_ROW = text("select id from ssc.bundle where org_id = :org and app_id = :app and digest = :digest")
_LOCK_ROW = text(
    "select state, created_at, stored_at from ssc.bundle where org_id = :org and id = :id "
    "for update skip locked"
)
_BUILDING = text(
    "select exists (select 1 from ssc.build where org_id = :org and bundle_id = :id "
    "and state in ('queued', 'running'))"
)
_EXPIRE = text(
    "update ssc.bundle set state = 'pending', stored_at = null, manifest = null, "
    "manifest_digest = null, file_count = null "
    "where org_id = :org and id = :id and state = 'stored'"
)

type _Verdict = Literal["delete", "expire", "keep", "busy"]


@dataclass(frozen=True, slots=True)
class _Decision:
    verdict: _Verdict
    row_id: str | None = field(default=None)


class BundleGcError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Collected:
    deleted: int = 0
    """Objects deleted, ``expired`` among them."""
    kept: int = 0
    busy: int = 0
    expired: int = 0
    """Stored bundles past ``RETENTION`` whose row went back to ``pending``."""

    def __add__(self, other: Collected) -> Collected:
        return Collected(
            self.deleted + other.deleted,
            self.kept + other.kept,
            self.busy + other.busy,
            self.expired + other.expired,
        )


async def lock_bundle_key(conn: AsyncConnection, key: str) -> None:
    """Wait for the key's advisory lock, held to the end of ``conn``'s transaction."""
    await conn.execute(_LOCK_KEY, {"cls": LOCK_CLASS, "key": key})


async def collect_all(
    engine: AsyncEngine,
    *,
    blob_store: BlobStore | None,
    cell_stores: CellStores | None,
    now: datetime,
) -> Collected:
    """``collect_org`` for every org in ``org_index``, each in its own store. One org's
    failure is logged and the others are collected; then ``BundleGcError`` is raised."""
    total, failed = Collected(), 0
    for org_id in await all_org_ids(engine):
        try:
            store = await org_bundle_store(
                engine, org_id, blob_store=blob_store, cell_stores=cell_stores
            )
            if store is not None:
                total += await collect_org(engine, store, org_id, now=now)
        except DBAPIError, BlobError, OSError:
            log.exception("bundle collection failed for one org", extra={"org_id": org_id})
            failed += 1
    log.info(
        "bundles collected",
        extra={"deleted": total.deleted, "expired": total.expired, "busy": total.busy},
    )
    if failed:
        raise BundleGcError(f"bundle collection failed for {failed} orgs")
    return total


async def collect_org(
    engine: AsyncEngine, store: BlobStore, org_id: str, *, now: datetime
) -> Collected:
    cutoff = now - GRACE
    counts = {"delete": 0, "keep": 0, "busy": 0, "expire": 0}
    try:
        listed = [info async for info in store.list(f"bundles/{org_id}/")]
    except BlobNotFoundError:
        log.info("no bundle store for this org yet", extra={"org_id": org_id})
        return Collected()
    for info in listed:
        verdict: _Verdict = "keep"
        if info.created_at < cutoff:
            async with bound_org(engine, org_id) as conn:
                decision = await _decide(conn, org_id, info.key, cutoff, now - RETENTION)
                verdict = decision.verdict
                if verdict in ("delete", "expire"):
                    deleted = await _delete_if_stale(store, org_id, info.key, cutoff, now)
                    if deleted and verdict == "expire":
                        await conn.execute(_EXPIRE, {"org": org_id, "id": decision.row_id})
                        counts["expire"] += 1
                    verdict = "delete" if deleted else "keep"
        counts[verdict] += 1
    return Collected(counts["delete"], counts["keep"], counts["busy"], counts["expire"])


async def _decide(  # noqa: PLR0911  (one return per rule, in order)
    conn: AsyncConnection, org_id: str, key: str, cutoff: datetime, retained: datetime
) -> _Decision:
    match = BUNDLE_KEY.fullmatch(key)
    if match is None or match[1] != org_id:
        return _Decision("keep")
    if not (await conn.execute(_TRY_LOCK_KEY, {"cls": LOCK_CLASS, "key": key})).scalar():
        return _Decision("busy")
    params = {"org": org_id, "app": match[2], "digest": DIGEST_PREFIX + match[3]}
    if (await conn.execute(_RELEASED, params)).scalar():
        return _Decision("keep")
    row_id = (await conn.execute(_ROW, params)).scalar()
    if row_id is None:
        return _Decision("delete")
    row = (await conn.execute(_LOCK_ROW, {"org": org_id, "id": row_id})).first()
    if row is None:
        return _Decision("busy")
    if row.state == "pending":
        return _Decision("delete" if row.created_at < cutoff else "keep")
    if row.stored_at >= retained:
        return _Decision("keep")
    building = (await conn.execute(_BUILDING, {"org": org_id, "id": row_id})).scalar()
    return _Decision("keep") if building else _Decision("expire", str(row_id))


async def _delete_if_stale(
    store: BlobStore, org_id: str, key: str, cutoff: datetime, now: datetime
) -> bool:
    """Delete ``key`` unless it was replaced since it was listed."""
    info = await store.stat(key)
    if info is None or info.created_at >= cutoff:
        return False
    await store.delete(key)
    match = BUNDLE_KEY.fullmatch(key)
    log.info(
        "bundle object deleted",
        extra={
            "org_id": org_id,
            "app_id": match[2] if match else None,
            "digest": DIGEST_PREFIX + match[3] if match else None,
            "age_hours": round((now - info.created_at).total_seconds() / 3600, 1),
        },
    )
    return True
