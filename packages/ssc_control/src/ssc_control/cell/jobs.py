"""The cell tasks (SSC-087). Registered by ``worker.build_app`` under the ``cell`` namespace;
callers defer them by name (``cell.tasks``)."""

from procrastinate import Blueprint, JobContext

from ssc_contracts.cells import CellResource
from ssc_control.cell import create
from ssc_control.worker_ports import ports_of


def blueprint() -> Blueprint:
    bp = Blueprint()

    @bp.task(name="create_resource", pass_context=True)
    async def create_resource(context: JobContext, org_id: str, resource: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """One step of creating a lazy resource; returns its state after the step."""
        return await create.create_step(
            ports_of(context), org_id=org_id, resource=CellResource(resource)
        )

    return bp
