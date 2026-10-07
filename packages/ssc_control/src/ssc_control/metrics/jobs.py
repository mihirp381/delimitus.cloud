"""The worker's usage collection (SSC-028). Registered by ``worker.build_app`` under the
``metrics`` namespace.

``usage_collect`` runs at twenty past every hour: ``collect.collect_all`` reads the hours that
ended at least ``collect.LAG`` ago from each cell and writes their usage events. One run at a
time (``lock``) and at most one waiting (``queueing_lock``). With no cells in the ports it
does nothing and logs why.

``blueprint()`` builds fresh tasks on each call: ``App.add_tasks_from`` renames what it copies.
"""

import logging
from typing import Final

from procrastinate import Blueprint, JobContext

from ssc_control.metrics.collect import collect_all
from ssc_control.worker_ports import ports_of

log = logging.getLogger(__name__)

NAMESPACE: Final = "metrics"
COLLECT_TASK: Final = f"{NAMESPACE}:usage_collect"
COLLECT_CRON: Final = "20 * * * *"


def blueprint(*, cron: str = COLLECT_CRON) -> Blueprint:
    """The usage collection, hourly by default."""
    bp = Blueprint()

    @bp.periodic(cron=cron, periodic_id="usage_collect", queueing_lock="usage_collect")
    @bp.task(name="usage_collect", pass_context=True, lock="usage_collect")
    async def usage_collect(context: JobContext, timestamp: int) -> int:  # pyright: ignore[reportUnusedFunction]
        """Collect every cell's usage; returns how many events were written."""
        ports = ports_of(context)
        if ports.cells is None:
            log.warning("usage events skipped: no cell usage source", extra={"tick": timestamp})
            return 0
        return await collect_all(ports.engine, ports.cells, ports.clock())

    return bp
