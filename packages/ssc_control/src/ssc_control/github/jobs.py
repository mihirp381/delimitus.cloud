"""The GitHub task (SSC-047). Registered by ``worker.build_app`` under the ``github`` namespace;
the webhook defers it by name (``github.tasks``). A step that raises is retried (``RETRY``, as the
snapshot compile); every step is safe to run again (``push``)."""

from typing import Final

from procrastinate import Blueprint, JobContext, RetryStrategy
from sqlalchemy.exc import DBAPIError

from ssc_control.github import push
from ssc_control.worker_ports import ports_of
from ssc_shared.blobstore import BlobError

RETRY: Final = RetryStrategy(
    max_attempts=6,
    wait=1,
    linear_wait=2,
    retry_exceptions=[DBAPIError, BlobError, OSError, TimeoutError],
)


def blueprint() -> Blueprint:
    bp = Blueprint()

    @bp.task(name="run_push", pass_context=True, retry=RETRY)
    async def run_push(  # noqa: PLR0913  # pyright: ignore[reportUnusedFunction]
        context: JobContext,
        *,
        org_id: str,
        app_id: str,
        sha: str,
        check_run_id: int | None = None,
        build_id: str | None = None,
        deployment_id: str | None = None,
    ) -> str:
        """One step of a push: store and build the commit, deploy preview, report on the
        commit; returns where it got to."""
        return await push.run_push(
            ports_of(context),
            org_id=org_id,
            app_id=app_id,
            sha=sha,
            check_run_id=check_run_id,
            build_id=build_id,
            deployment_id=deployment_id,
        )

    return bp
