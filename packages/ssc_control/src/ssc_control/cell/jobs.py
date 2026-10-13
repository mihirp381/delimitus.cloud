"""The cell tasks (SSC-087, SSC-092). Registered by ``worker.build_app`` under the ``cell``
namespace; callers defer them by name (``cell.tasks``).

Both steps are safe to run twice, so a step that raises (the database out for a moment) is
retried (``RETRY``, as the snapshot compile). When ``create_resource`` has no attempt left the
resource fails with ``CELL_RESOURCE_STALLED`` (``create.give_up``), which re-defers the
deployments waiting for it, instead of staying ``creating`` with no job.
"""

import logging
from typing import Final

from procrastinate import Blueprint, JobContext, RetryStrategy
from sqlalchemy.exc import DBAPIError

from ssc_contracts.cells import CellResource
from ssc_control.cell import create, warm
from ssc_control.worker_ports import ports_of
from ssc_shared.blobstore import BlobError

log = logging.getLogger(__name__)

RETRY: Final = RetryStrategy(
    max_attempts=6,
    wait=1,
    linear_wait=2,
    retry_exceptions=[DBAPIError, BlobError, OSError, TimeoutError],
)


def blueprint() -> Blueprint:
    bp = Blueprint()

    @bp.task(name="create_resource", pass_context=True, retry=RETRY)
    async def create_resource(context: JobContext, org_id: str, resource: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """One step of creating a lazy resource; returns its state after the step."""
        ports = ports_of(context)
        try:
            return await create.create_step(ports, org_id=org_id, resource=CellResource(resource))
        except Exception as exc:
            if RETRY.get_retry_decision(exception=exc, job=context.job) is None:
                try:
                    await create.give_up(ports, org_id=org_id, resource=CellResource(resource))
                except Exception:
                    log.exception("could not fail the cell resource", extra={"org_id": org_id})
            raise

    @bp.task(name="warm_gateway", pass_context=True, retry=RETRY)
    async def warm_gateway(context: JobContext, org_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """One step of setting the gateway's warm flag; returns its state after the step."""
        return await warm.gateway_step(ports_of(context), org_id=org_id)

    return bp
