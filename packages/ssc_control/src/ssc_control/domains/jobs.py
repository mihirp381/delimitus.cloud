"""The worker's domains tick (CD-4.1), registered by ``worker.build_app`` under the ``domains``
namespace: one ``tick.tick`` over every open row each minute (cron's finest step). With no
resolvers in the ports it does nothing.

``blueprint()`` builds fresh tasks on each call: ``App.add_tasks_from`` renames what it copies.
"""

import logging
from typing import Final

from procrastinate import Blueprint, JobContext

from ssc_control.domains import tick as domains_tick
from ssc_control.worker_ports import Ports, ports_of

log = logging.getLogger("ssc.domains")

NAMESPACE: Final = "domains"
TICK_TASK: Final = f"{NAMESPACE}:tick"
TICK_CRON: Final = "* * * * *"


async def tick_with(ports: Ports) -> domains_tick.TickReport | None:
    if ports.resolvers is None:
        log.warning("domains tick skipped: no resolvers")
        return None
    notice = (
        domains_tick.StallNotice(
            mailer=ports.mailer, to=ports.operator_mail, console_url=ports.console_url
        )
        if ports.mailer is not None and ports.operator_mail
        else None
    )
    report = await domains_tick.tick(
        ports.engine, ports.cells, ports.resolvers, ports.clock(), notice=notice
    )
    log.info(
        "domains tick: %d open, %d changed, %d stalled, %d errors",
        report.checked,
        len(report.changed),
        len(report.stalled),
        len(report.errors),
        extra={"changed": report.changed, "stalled": report.stalled, "errors": report.errors},
    )
    return report


def blueprint(*, tick_cron: str = TICK_CRON) -> Blueprint:
    bp = Blueprint()

    @bp.periodic(cron=tick_cron, periodic_id="domains_tick", queueing_lock="domains_tick")
    @bp.task(name="tick", pass_context=True, lock="domains_tick")
    async def tick(context: JobContext, timestamp: int) -> int:  # pyright: ignore[reportUnusedFunction]
        report = await tick_with(ports_of(context))
        return 0 if report is None else len(report.changed)

    return bp
