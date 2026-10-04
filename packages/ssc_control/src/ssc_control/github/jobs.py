"""The GitHub task (SSC-047). Registered by ``worker.build_app`` under the ``github`` namespace;
the webhook defers it by name (``github.tasks``)."""

from procrastinate import Blueprint, JobContext

from ssc_control.github import push
from ssc_control.worker_ports import ports_of


def blueprint() -> Blueprint:
    bp = Blueprint()

    @bp.task(name="run_push", pass_context=True)
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
