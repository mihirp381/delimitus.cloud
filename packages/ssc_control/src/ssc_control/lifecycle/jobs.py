"""The lifecycle tasks (SSC-025). Registered by ``worker.build_app`` under the ``lifecycle``
namespace; the API defers them by name (``lifecycle.tasks``).

``run_kill_switch`` drives one run of an app's kill switch (``queueing_lock`` ``kil:<app id>``),
taking an environment's lock when it scales that environment. ``sweep`` is periodic and
re-defers any running run whose job is gone.
"""

from typing import Final

from procrastinate import Blueprint, JobContext

from ssc_control.lifecycle import kill_switch
from ssc_control.worker_ports import ports_of

SWEEP_CRON: Final = "* * * * *"
"""Every minute."""


def blueprint(*, sweep_cron: str = SWEEP_CRON) -> Blueprint:
    bp = Blueprint()

    @bp.task(name="run_kill_switch", pass_context=True)
    async def run_kill_switch(  # pyright: ignore[reportUnusedFunction]
        context: JobContext, org_id: str, run_id: str, env_id: str | None = None
    ) -> str:
        """Drive the run as far as this job can; returns its state after."""
        return await kill_switch.run(ports_of(context), org_id=org_id, run_id=run_id, env_id=env_id)

    @bp.periodic(
        cron=sweep_cron, periodic_id="kill_switch_sweep", queueing_lock="kill_switch_sweep"
    )
    @bp.task(name="sweep", pass_context=True)
    async def sweep(context: JobContext, timestamp: int) -> int:  # pyright: ignore[reportUnusedFunction]
        """Re-defer every running run that has no job; returns how many."""
        del timestamp
        return await kill_switch.sweep(ports_of(context).engine)

    return bp
