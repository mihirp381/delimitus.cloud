"""The worker's snapshot task (decision 019). Registered by ``worker.build_app`` under the
``snapshot`` namespace; the API defers it by name (``service.COMPILE_TASK``).

One job per org at a time (``lock``), at most one waiting (``queueing_lock``). A failure is
retried; with no blob store configured the job fails at once, without retries.
"""

from typing import Final

from procrastinate import Blueprint, JobContext, RetryStrategy
from sqlalchemy.exc import DBAPIError

from ssc_control.db.bind import bound_org
from ssc_control.snapshot.compiler import point_latest, publish
from ssc_control.worker_ports import ports_of
from ssc_shared.blobstore import BlobError

RETRY: Final = RetryStrategy(
    max_attempts=6,
    wait=1,
    linear_wait=2,
    retry_exceptions=[DBAPIError, BlobError, OSError, TimeoutError],
)


class NoBlobStoreError(RuntimeError):
    pass


def blueprint() -> Blueprint:
    bp = Blueprint()

    @bp.task(name="compile", pass_context=True, retry=RETRY)
    async def compile_snapshot(context: JobContext, org_id: str) -> int:  # pyright: ignore[reportUnusedFunction]
        """Publish the org's next version and move ``latest.json``; returns the version."""
        ports = ports_of(context)
        if ports.blob_store is None:
            raise NoBlobStoreError("snapshot:compile needs Ports.blob_store")
        async with bound_org(ports.engine, org_id) as conn:
            version = await publish(conn, org_id, ports.blob_store, at=ports.clock())
        await point_latest(ports.engine, org_id, ports.blob_store)
        return version

    return bp
