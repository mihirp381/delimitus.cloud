"""The worker's snapshot tasks (decision 019). Registered by ``worker.build_app`` under the
``snapshot`` namespace; the API defers ``compile`` by name (``service.COMPILE_TASK``).

``compile``: one job per org at a time (``lock``), at most one waiting (``queueing_lock``). A
failure is retried six times. With no blob store configured it does nothing.

``stale_sweep`` is periodic: for every org whose newest version lags a live compile, or whose
``latest.json`` lags its newest version (a compile that ran out of retries, a lost job), it marks
the snapshot dirty, which defers a compile. One org's failure is logged and the sweep goes on.
"""

import logging
from typing import Final

from procrastinate import Blueprint, JobContext, RetryStrategy
from sqlalchemy.exc import DBAPIError

from ssc_control.db.bind import bound_org
from ssc_control.db.orgs import all_org_ids
from ssc_control.snapshot.compiler import is_stale, point_latest, publish
from ssc_control.snapshot.service import mark_dirty
from ssc_control.worker_ports import ports_of
from ssc_shared.blobstore import BlobError

log = logging.getLogger(__name__)

SWEEP_CRON: Final = "*/5 * * * *"
"""Every five minutes: each pass compiles every org once in memory, so it is not run faster."""
RETRY: Final = RetryStrategy(
    max_attempts=6,
    wait=1,
    linear_wait=2,
    retry_exceptions=[DBAPIError, BlobError, OSError, TimeoutError],
)


def blueprint(*, sweep_cron: str = SWEEP_CRON) -> Blueprint:
    bp = Blueprint()

    @bp.task(name="compile", pass_context=True, retry=RETRY)
    async def compile_snapshot(context: JobContext, org_id: str) -> int | None:  # pyright: ignore[reportUnusedFunction]
        """Publish the org's next version and move ``latest.json``; returns the version."""
        ports = ports_of(context)
        if ports.blob_store is None:
            log.warning("snapshot compile skipped: no blob store", extra={"org_id": org_id})
            return None
        async with bound_org(ports.engine, org_id) as conn:
            version = await publish(conn, org_id, ports.blob_store, at=ports.clock())
        await point_latest(ports.engine, org_id, ports.blob_store)
        return version

    @bp.periodic(cron=sweep_cron, periodic_id="snapshot_sweep", queueing_lock="snapshot_sweep")
    @bp.task(name="stale_sweep", pass_context=True)
    async def stale_sweep(context: JobContext, timestamp: int) -> int:  # pyright: ignore[reportUnusedFunction]
        """Mark every lagging org's snapshot dirty; returns how many."""
        ports = ports_of(context)
        if ports.blob_store is None:
            log.info("snapshot sweep skipped: no blob store", extra={"tick": timestamp})
            return 0
        dirty = 0
        for org_id in await all_org_ids(ports.engine):
            try:
                if not await is_stale(ports.engine, org_id, ports.blob_store):
                    continue
                async with bound_org(ports.engine, org_id) as conn:
                    await mark_dirty(conn, org_id)
            except DBAPIError, BlobError, OSError, ValueError:
                log.exception("snapshot sweep failed for one org", extra={"org_id": org_id})
                continue
            log.warning("stale snapshot marked dirty", extra={"org_id": org_id})
            dirty += 1
        return dirty

    return bp
