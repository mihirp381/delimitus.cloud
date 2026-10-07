"""Deployments whose job is gone (readiness review): a job that raised, for instance on a database
error while going live, leaves its deployment ``pending`` or ``running``, and the
``deployment_one_in_flight`` index then blocks the environment.

``sweep`` runs every five minutes. For each ``pending`` or ``running`` deployment with no job
waiting or running under its ``queueing_lock`` (``dep:<id>``), and not waiting on a lazy cell
resource (``cell_resource_waiter``, whose job re-defers it), it defers the deployment job again;
the job is safe to run twice (``deployments``). Once :data:`MAX_ATTEMPTS` jobs have failed it
fails the deployment with ``DEPLOYMENT_STALLED`` instead, audited as ``deploy.failed`` or
``rollback.failed`` by the deployment's actor, and frees the environment. The reaper never calls
the runtime: a lowered request timeout stays until the next deployment, as after a lost traffic
call. A job left ``doing`` by a dead worker is the stalled sweep's (``worker.retry_stalled``).
"""

import logging
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_control.db.bind import bound_org
from ssc_control.db.orgs import all_org_ids
from ssc_control.deploy import deployments
from ssc_control.deploy.tasks import defer_deployment

log = logging.getLogger(__name__)

SWEEP_CRON: Final = "*/5 * * * *"
MAX_ATTEMPTS: Final = 3
STALLED: Final = "DEPLOYMENT_STALLED"

_IN_FLIGHT = text(
    "select d.id, d.environment_id, d.kind from ssc.deployment d "
    "where d.org_id = :org and d.state in ('pending', 'running') and not exists ("
    "select 1 from ssc.cell_resource_waiter w "
    "where w.org_id = d.org_id and w.deployment_id = d.id) "
    "order by d.started_at"
)
_JOBS = text(
    "select count(*) filter (where status in ('todo', 'doing')) as live, "
    "count(*) filter (where status = 'failed') as failed "
    "from procrastinate.procrastinate_jobs where queueing_lock = :lock"
)


async def sweep(engine: AsyncEngine) -> int:
    """Every org's stuck deployments, deferred again or failed; how many."""
    handled = 0
    for org_id in await all_org_ids(engine):
        try:
            async with bound_org(engine, org_id) as conn:
                handled += await sweep_org(conn, org_id)
        except DBAPIError:
            log.exception("deployment sweep failed for one org", extra={"org_id": org_id})
    return handled


async def sweep_org(conn: AsyncConnection, org_id: str) -> int:
    handled = 0
    for dep_id, env_id, kind in (await conn.execute(_IN_FLIGHT, {"org": org_id})).all():
        jobs = (await conn.execute(_JOBS, {"lock": f"dep:{dep_id}"})).one()
        if jobs.live:
            continue
        extra = {"deployment_id": dep_id, "failed_jobs": jobs.failed}
        if jobs.failed >= MAX_ATTEMPTS:
            if await deployments.fail_stuck(conn, org_id, str(dep_id), STALLED):
                log.warning("stuck deployment failed", extra=extra)
                handled += 1
            continue
        job = await defer_deployment(
            conn,
            org_id=org_id,
            environment_id=str(env_id),
            deployment_id=str(dep_id),
            rollback=kind == "rollback",
        )
        if job is not None:
            log.warning("stuck deployment re-deferred", extra=extra)
            handled += 1
    return handled
