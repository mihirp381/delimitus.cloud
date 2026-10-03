"""The worker's directory sync (SSC-019, SSC-064, decision 024). Registered by
``worker.build_app`` under the ``identity`` namespace.

``directory_sync`` runs every minute: one ``sync.tick`` for each org, which does nothing for an org
without a directory connection. One org's failure is logged and the others still sync; the next
minute tries again. One run at a time (``lock``) and at most one waiting (``queueing_lock``). With
no WorkOS client in the ports it does nothing.

``blueprint()`` builds fresh tasks on each call: ``App.add_tasks_from`` renames what it copies.
"""

import logging
from typing import Final

from procrastinate import Blueprint, JobContext
from sqlalchemy.ext.asyncio import AsyncEngine

from ssc_control.db.orgs import all_org_ids
from ssc_control.identity import sync
from ssc_control.identity.workos import WorkOSClient
from ssc_control.worker_ports import ports_of

log = logging.getLogger(__name__)

NAMESPACE: Final = "identity"
SYNC_TASK: Final = f"{NAMESPACE}:directory_sync"
SYNC_CRON: Final = "* * * * *"


async def sync_all(engine: AsyncEngine, client: WorkOSClient) -> int:
    """One tick for every org; returns how many orgs had a connection to sync."""
    synced = 0
    for org_id in await all_org_ids(engine):
        try:
            report = await sync.tick(engine, client, org_id)
        except Exception:
            log.exception("directory sync of %s raised", org_id)
            continue
        if report is not None:
            log.info("directory sync %s", report)
            synced += 1
    return synced


def blueprint(*, cron: str = SYNC_CRON) -> Blueprint:
    """The directory sync, every minute by default."""
    bp = Blueprint()

    @bp.periodic(cron=cron, periodic_id="directory_sync", queueing_lock="directory_sync")
    @bp.task(name="directory_sync", pass_context=True, lock="directory_sync")
    async def directory_sync(context: JobContext, timestamp: int) -> int:  # pyright: ignore[reportUnusedFunction]
        """Sync every org's directory; returns how many orgs had a connection."""
        ports = ports_of(context)
        if ports.directory is None:
            log.warning("directory sync skipped: no WorkOS client", extra={"tick": timestamp})
            return 0
        return await sync_all(ports.engine, ports.directory)

    return bp
