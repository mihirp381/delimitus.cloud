"""The worker's onboarding tick (ticket 19D), registered by ``worker.build_app`` under the
``onboarding`` namespace: one ``service.tick`` over every open row, every five minutes unless
``SSC_ONBOARDING_TICK_CRON`` says otherwise. With no WorkOS client in the ports it does nothing.

``blueprint()`` builds fresh tasks on each call: ``App.add_tasks_from`` renames what it copies.
"""

import logging
from typing import Final

from procrastinate import Blueprint, JobContext

from ssc_control.onboarding import service
from ssc_control.worker_ports import Ports, ports_of

log = logging.getLogger("ssc.onboarding")

NAMESPACE: Final = "onboarding"
TICK_TASK: Final = f"{NAMESPACE}:tick"
TICK_CRON: Final = "*/5 * * * *"


async def tick_with(ports: Ports) -> service.TickReport | None:
    if ports.directory is None:
        log.warning("onboarding tick skipped: no WorkOS client")
        return None
    report = await service.tick(ports.engine, ports.directory, ports.clock())
    log.info(
        "onboarding tick: %d open, %d changed, %d live, %d failed",
        report.checked,
        len(report.changed),
        len(report.live),
        len(report.failed),
        extra={"changed": report.changed, "live": report.live, "failed": report.failed},
    )
    return report


def blueprint(*, tick_cron: str = TICK_CRON) -> Blueprint:
    bp = Blueprint()

    @bp.periodic(cron=tick_cron, periodic_id="onboarding_tick", queueing_lock="onboarding_tick")
    @bp.task(name="tick", pass_context=True, lock="onboarding_tick")
    async def tick(context: JobContext, timestamp: int) -> int:  # pyright: ignore[reportUnusedFunction]
        """Move every open onboarding; returns how many rows changed."""
        report = await tick_with(ports_of(context))
        return 0 if report is None else len(report.changed)

    return bp
