"""The deploy tasks (SSC-016). Registered by ``worker.build_app`` under the ``deploy`` namespace;
the API defers them by name (``deploy.tasks``).

``run_build`` has one job per build (``queueing_lock`` ``bld:<id>``) and defers its own next
poll. ``run_deployment`` (``dep:<id>``) takes the environment's lock, like every job that calls
the runtime driver for that environment. ``collect_bundles`` runs hourly and deletes the bundle
objects ``deploy.bundle_gc`` finds unusable, in each org's cell bucket when ``Ports.cell_stores``
is set, else in the blob store; with neither it does nothing.
"""

from typing import Final

from procrastinate import Blueprint, JobContext

from ssc_control.deploy import builds, bundle_gc, deployments
from ssc_control.worker_ports import ports_of

GC_CRON: Final = "17 * * * *"


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

    @bp.periodic(cron=GC_CRON, periodic_id="bundle_gc", queueing_lock="bundle_gc")
    @bp.task(name="collect_bundles", pass_context=True)
    async def collect_bundles(context: JobContext, timestamp: int) -> int:  # pyright: ignore[reportUnusedFunction]
        """Delete stale pending and orphan bundle objects in every org; returns how many."""
        del timestamp
        ports = ports_of(context)
        if ports.blob_store is None and ports.cell_stores is None:
            return 0
        collected = await bundle_gc.collect_all(
            ports.engine,
            blob_store=ports.blob_store,
            cell_stores=ports.cell_stores,
            now=ports.clock(),
        )
        return collected.deleted

    return bp
