"""The deploy tasks (SSC-016). Registered by ``worker.build_app`` under the ``deploy`` namespace;
the API defers them by name (``deploy.tasks``).

``run_build`` has one job per build (``queueing_lock`` ``bld:<id>``) and defers its own next
poll. ``run_deployment`` (``dep:<id>``) takes the environment's lock, like every job that calls
the runtime driver for that environment.
"""

from procrastinate import Blueprint, JobContext

from ssc_control.deploy import builds, deployments
from ssc_control.worker_ports import ports_of


def blueprint() -> Blueprint:
    bp = Blueprint()

    @bp.task(name="run_build", pass_context=True)
    async def run_build(context: JobContext, org_id: str, build_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """One step of a build; returns its state after the step."""
        return await builds.run_build(ports_of(context), org_id=org_id, build_id=build_id)

    @bp.task(name="run_deployment", pass_context=True)
    async def run_deployment(context: JobContext, org_id: str, deployment_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """Run a deployment to healthy or failed; returns its final state."""
        return await deployments.run_deployment(
            ports_of(context), org_id=org_id, deployment_id=deployment_id
        )

    return bp
