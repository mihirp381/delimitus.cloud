"""The worker's audit tasks (SSC-012, decision 012). Registered by ``worker.build_app`` under the
``audit`` namespace.

``anchor_tick`` runs at five past every hour: for each org without the day's anchor it defers
one ``anchor`` job, at most one waiting per org and day (``queueing_lock``). So the day's anchor
is written at 00:05 UTC, or within the hour after a worker that was down comes back
(Procrastinate forgets ticks missed while no worker ran). One org's failure is logged, the
others are deferred, and the tick then fails so it is retried; deferring again is harmless.

``anchor`` writes the org's anchor for its day. It is retried forever, backing off to once an
hour, because a missing anchor is a gap an operator must see (``verify --anchors`` reports it
after 36 hours), not a job that gave up. It writes to the org's cell bucket when
``Ports.cell_stores`` is set, else to ``Ports.blob_store`` (``storage.org_store``, the choice the
snapshot compile makes); with neither configured both do nothing.

``blueprint()`` builds fresh tasks on each call: ``App.add_tasks_from`` renames what it copies.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

from procrastinate import BaseRetryStrategy, Blueprint, JobContext, RetryDecision
from procrastinate.jobs import Job
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ssc_control.audit.anchor import daily_key, write_anchor
from ssc_control.db.bind import bound_org
from ssc_control.db.orgs import all_org_ids
from ssc_control.deferral import defer
from ssc_control.storage import org_store
from ssc_control.worker_ports import ports_of

log = logging.getLogger(__name__)

NAMESPACE: Final = "audit"
ANCHOR_TASK: Final = f"{NAMESPACE}:anchor"
TICK_CRON: Final = "5 * * * *"
"""Hourly at minute 5, UTC; the first tick of a day writes that day's anchor."""
_ANCHORED = text("select 1 from ssc.audit_anchor where org_id = :org and object_key = :key")


@dataclass(frozen=True, slots=True)
class CappedBackoff(BaseRetryStrategy):
    """Retry every failure, forever: ``first`` seconds, doubling, at most ``cap`` apart."""

    first: int = 30
    cap: int = 3600

    def get_retry_decision(self, *, exception: BaseException, job: Job) -> RetryDecision:
        del exception
        return RetryDecision(
            retry_in={"seconds": min(self.cap, self.first << min(job.attempts, 20))}
        )


class AnchorTickError(RuntimeError):
    pass


def anchor_lock(org_id: str, day: str) -> str:
    return f"anchor:{org_id}:{day}"


def blueprint(*, tick_cron: str = TICK_CRON) -> Blueprint:
    bp = Blueprint()

    @bp.periodic(cron=tick_cron, periodic_id="audit_anchor", queueing_lock="audit_anchor_tick")
    @bp.task(name="anchor_tick", pass_context=True, retry=CappedBackoff())
    async def anchor_tick(context: JobContext, timestamp: int) -> int:  # pyright: ignore[reportUnusedFunction]
        """Defer the day's anchor for every org without one; returns how many were deferred."""
        ports = ports_of(context)
        if ports.blob_store is None and ports.cell_stores is None:
            log.warning("anchor tick skipped: no blob store", extra={"tick": timestamp})
            return 0
        day = datetime.fromtimestamp(timestamp, UTC).date()
        deferred, failed = 0, 0
        for org_id in await all_org_ids(ports.engine):
            try:
                async with bound_org(ports.engine, org_id) as conn:
                    key = daily_key(org_id, day)
                    if (await conn.execute(_ANCHORED, {"org": org_id, "key": key})).first():
                        continue
                    job_id = await defer(
                        conn,
                        ANCHOR_TASK,
                        queueing_lock=anchor_lock(org_id, day.isoformat()),
                        org_id=org_id,
                        day=day.isoformat(),
                    )
            except DBAPIError:
                log.exception("anchor defer failed for one org", extra={"org_id": org_id})
                failed += 1
                continue
            deferred += job_id is not None
        if failed:
            raise AnchorTickError(f"{failed} orgs not deferred for {day}; retrying the tick")
        return deferred

    @bp.task(name="anchor", pass_context=True, retry=CappedBackoff())
    async def anchor(context: JobContext, org_id: str, day: str) -> int | None:  # pyright: ignore[reportUnusedFunction]
        """Write the org's anchor for ``day``; returns the anchored seq."""
        ports = ports_of(context)
        store = await org_store(
            ports.engine, org_id, blob_store=ports.blob_store, cell_stores=ports.cell_stores
        )
        if store is None:
            log.warning("anchor skipped: no blob store", extra={"org_id": org_id})
            return None
        async with bound_org(ports.engine, org_id) as conn:
            written = await write_anchor(conn, org_id, store, "daily", day=date.fromisoformat(day))
        return written.seq

    return bp
