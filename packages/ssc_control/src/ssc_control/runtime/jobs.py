"""The reconciler's worker tasks (SSC-017, decision 014).

``reconcile_tick`` is periodic: every tick it reads every org id from ``ssc.org_index``, then, in
that org's bound transaction, defers one ``reconcile_env`` job per app environment that has a live
deployment. The job's ``queueing_lock`` keeps at most one waiting pass per environment, and its
``lock`` (``env:<id>``) serialises it with every other job that changes the environment's runtime:
any lane's job that calls the runtime driver for an environment takes the same lock.

Each org is reconciled through its own cell (``runtime.cells``). An org whose cell this worker
cannot reach is skipped by the tick, with one warning per org per tick, so it never holds up the
others; a pass already deferred for it fails with ``CellUnavailableError``.

``blueprint()`` builds fresh tasks on each call, because ``App.add_tasks_from`` renames the tasks
it copies: one blueprint per app.
"""

import logging
from typing import Final

from procrastinate import Blueprint, JobContext
from sqlalchemy import text

from ssc_control.db.bind import bound_org
from ssc_control.db.orgs import all_org_ids
from ssc_control.deferral import defer, env_lock
from ssc_control.runtime import reconciler
from ssc_control.runtime.cells import CellUnavailableError
from ssc_control.worker_ports import ports_of

log = logging.getLogger(__name__)

NAMESPACE: Final = "runtime"
TICK_CRON: Final = "* * * * * */15"
"""Every 15 seconds: cron with a sixth, seconds, field."""
RECONCILE_ENV: Final = f"{NAMESPACE}:reconcile_env"

_LIVE_ENVS: Final = text(
    "select id from ssc.environment where org_id = :org and current_deployment_id is not null "
    "order by id"
)


class NoRuntimeDriverError(RuntimeError):
    pass


def blueprint(*, tick_cron: str = TICK_CRON) -> Blueprint:
    bp = Blueprint()

    @bp.periodic(cron=tick_cron, periodic_id="reconcile", queueing_lock="reconcile_tick")
    @bp.task(name="reconcile_tick", pass_context=True)
    async def reconcile_tick(context: JobContext, timestamp: int) -> int:  # pyright: ignore[reportUnusedFunction]
        """Defer a pass for every live environment; returns how many were deferred."""
        ports = ports_of(context)
        if ports.cells is None:
            log.info("reconcile tick skipped: no runtime driver", extra={"tick": timestamp})
            return 0
        deferred = 0
        for org_id in await all_org_ids(ports.engine):
            try:
                cell = await ports.cells.for_org(org_id)
            except CellUnavailableError as exc:
                log.warning(
                    "reconcile tick skipped an org: its cell is not configured",
                    extra={"tick": timestamp, "org_id": org_id, "cell_label": exc.label},
                )
                continue
            if cell.runtime is None:
                continue
            async with bound_org(ports.engine, org_id) as conn:
                env_ids = (await conn.execute(_LIVE_ENVS, {"org": org_id})).scalars().all()
                for env_id in env_ids:
                    job_id = await defer(
                        conn,
                        RECONCILE_ENV,
                        queueing_lock=f"reconcile:{env_id}",
                        lock=env_lock(env_id),
                        org_id=org_id,
                        env_id=env_id,
                    )
                    deferred += job_id is not None
        return deferred

    @bp.task(name="reconcile_env", pass_context=True)
    async def reconcile_env(context: JobContext, org_id: str, env_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """One pass for one environment; returns the outcome kind."""
        ports = ports_of(context)
        if ports.cells is None:
            raise NoRuntimeDriverError("reconcile_env needs Ports.cells")
        cell = await ports.cells.for_org(org_id)
        if cell.runtime is None:
            raise NoRuntimeDriverError("reconcile_env needs a runtime in the org's cell")
        outcome = await reconciler.reconcile_env(
            ports.engine,
            cell.runtime,
            ports.release_specs,
            org_id=org_id,
            env_id=env_id,
            identity=cell.identity,
        )
        return outcome.kind

    return bp
