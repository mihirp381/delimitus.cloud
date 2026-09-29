"""The timer tasks (SSC-041, decision 020). Registered by ``worker.build_app`` under the
``timers`` namespace; the API and the timers service defer ``run`` by name (``timers.tasks``).

``run`` claims, dispatches and records one run (``runner.run_timer``). It retries database and
network failures; a retry after the claim committed finds the run stale, so a dispatch happens at
most once. ``sweep`` runs every minute over every org (``runner.sweep_org``); one org's failure
is logged and the others are swept. Neither calls the runtime driver, so neither takes an
``env:<id>`` lock.
"""

import logging
from typing import Final

from procrastinate import Blueprint, JobContext, RetryStrategy
from sqlalchemy.exc import DBAPIError

from ssc_control.db.bind import bound_org
from ssc_control.db.orgs import all_org_ids
from ssc_control.timers.runner import Outcome, RunDeps, run_timer, sweep_org
from ssc_control.worker_ports import ports_of

log = logging.getLogger(__name__)

SWEEP_CRON: Final = "* * * * *"
"""Every minute."""
RETRY: Final = RetryStrategy(
    max_attempts=6, wait=1, linear_wait=2, retry_exceptions=[DBAPIError, OSError]
)


def blueprint(*, sweep_cron: str = SWEEP_CRON) -> Blueprint:
    bp = Blueprint()

    @bp.task(name="run", pass_context=True, retry=RETRY)
    async def run(  # pyright: ignore[reportUnusedFunction]
        context: JobContext,
        org_id: str,
        schedule_id: str,
        scheduled_for: str | None = None,
        run_id: str | None = None,
    ) -> Outcome:
        """One scheduled instant or one queued manual run; returns what became of it."""
        ports = ports_of(context)
        deps = RunDeps(
            engine=ports.engine,
            dispatcher=ports.timer_dispatcher,
            metrics=ports.metrics,
            clock=ports.clock,
        )
        return await run_timer(
            deps, org_id=org_id, schedule_id=schedule_id, scheduled_for=scheduled_for, run_id=run_id
        )

    @bp.periodic(cron=sweep_cron, periodic_id="timers_sweep", queueing_lock="timers_sweep")
    @bp.task(name="sweep", pass_context=True)
    async def sweep(context: JobContext, timestamp: int) -> int:  # pyright: ignore[reportUnusedFunction]
        """Sweep every org; returns how many armed instants were deferred again."""
        del timestamp
        ports = ports_of(context)
        deferred = 0
        for org_id in await all_org_ids(ports.engine):
            try:
                async with bound_org(ports.engine, org_id) as conn:
                    deferred += await sweep_org(conn, org_id, now=ports.clock())
            except DBAPIError:
                log.exception("timers sweep failed for one org", extra={"org_id": org_id})
        return deferred

    return bp
