"""The worker: one Procrastinate app, every lane's tasks, and the composition root (decision 014).

``python -m ssc_control.worker`` reads ``SSC_DATABASE_DSN`` (the ``ssc_app`` DSN the API uses),
builds the ``Ports`` and runs until SIGINT or SIGTERM. Each lane adds its tasks with one
``add_tasks_from`` line in ``build_app``, from a blueprint factory.

``stalled_sweep`` is decision 008's recovery: a job left in ``doing`` by a worker whose heartbeat
stopped (a SIGKILL, a lost node) is put back to ``todo``. Procrastinate 3.10 has no single call for
this, so the sweep is ``get_stalled_jobs`` then ``retry_job``. Every task must therefore be safe to
run twice.

Fakes run only when ``SSC_ENV`` is ``dev`` or ``test``: ``compose_ports`` refuses otherwise, so no
production worker can silently drive an in-memory runtime.
"""

import asyncio
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from procrastinate import App, Blueprint, JobContext, PsycopgConnector
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ssc_control.db.catalog import QUEUE_SCHEMA
from ssc_control.db.engine import make_engine
from ssc_control.runtime import jobs as runtime_jobs
from ssc_control.runtime.driver import RuntimeDriver
from ssc_control.runtime.fake import FakeRuntimeDriver
from ssc_control.worker_ports import PORTS_KEY, Ports, PortsMissingError, ports_of

log = logging.getLogger(__name__)

DSN_ENV: Final = "SSC_DATABASE_DSN"
ENV_ENV: Final = "SSC_ENV"
RUNTIME_DRIVER_ENV: Final = "SSC_RUNTIME_DRIVER"
FAKE_ENVIRONMENTS: Final = frozenset({"dev", "test"})
SWEEP_CRON: Final = "* * * * * */30"
"""Every 30 seconds."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerSettings:
    """Timings. A job is stalled once its worker's heartbeat is ``stalled_after_seconds`` old,
    which must be well above ``heartbeat_seconds`` so a live but busy worker is never swept."""

    tick_cron: str = runtime_jobs.TICK_CRON
    sweep_cron: str = SWEEP_CRON
    stalled_after_seconds: float = 30.0
    heartbeat_seconds: float = 5.0
    stalled_worker_timeout: float = 30.0
    polling_seconds: float = 5.0
    concurrency: int = 1
    delete_jobs: Literal["never", "successful", "always"] = "successful"
    """Succeeded jobs are deleted: a tick every 15 s would otherwise grow the table forever.
    Failed jobs stay for inspection."""

    def __post_init__(self) -> None:
        if self.stalled_after_seconds <= 2 * self.heartbeat_seconds:
            raise ValueError("stalled_after_seconds must exceed two heartbeats")


class CompositionError(RuntimeError):
    pass


def queue_conninfo(dsn: str) -> str:
    """``dsn`` with ``search_path`` set to Procrastinate's schema, keeping any other options."""
    options = conninfo_to_dict(dsn).get("options")
    path = f"-c search_path={QUEUE_SCHEMA}"
    return make_conninfo(dsn, options=f"{options} {path}" if options else path)


def core_blueprint(*, sweep_cron: str, stalled_after_seconds: float) -> Blueprint:
    bp = Blueprint()

    @bp.periodic(cron=sweep_cron, periodic_id="stalled_sweep", queueing_lock="stalled_sweep")
    @bp.task(name="stalled_sweep", pass_context=True)
    async def stalled_sweep(context: JobContext, timestamp: int) -> int:  # pyright: ignore[reportUnusedFunction]
        """Put jobs of dead workers back to ``todo``; returns how many."""
        del timestamp
        return await retry_stalled(context.app, seconds=stalled_after_seconds)

    return bp


async def retry_stalled(app: App, *, seconds: float) -> int:
    """``get_stalled_jobs`` then ``retry_job`` for each (decision 008)."""
    stalled = list(await app.job_manager.get_stalled_jobs(seconds_since_heartbeat=seconds))
    for job in stalled:
        await app.job_manager.retry_job(job)
        log.warning("stalled job retried", extra={"job_id": job.id, "task_name": job.task_name})
    return len(stalled)


def build_app(dsn: str, *, settings: WorkerSettings | None = None) -> App:
    """The worker's Procrastinate app with every lane's tasks. Not opened."""
    s = settings or WorkerSettings()
    app = App(connector=PsycopgConnector(conninfo=queue_conninfo(dsn)))
    app.add_tasks_from(
        core_blueprint(sweep_cron=s.sweep_cron, stalled_after_seconds=s.stalled_after_seconds),
        namespace="core",
    )
    app.add_tasks_from(runtime_jobs.blueprint(tick_cron=s.tick_cron), namespace="runtime")
    return app


def runtime_driver_from_env(env: Mapping[str, str]) -> RuntimeDriver | None:
    """``SSC_RUNTIME_DRIVER``: unset means none (the reconciler defers nothing), ``fake`` the
    in-memory driver. Cloud drivers arrive with SSC-013."""
    match env.get(RUNTIME_DRIVER_ENV, ""):
        case "":
            return None
        case "fake":
            return FakeRuntimeDriver()
        case other:
            raise CompositionError(f"unknown {RUNTIME_DRIVER_ENV} {other!r}")


def refuse_fakes(ports: Ports, env: Mapping[str, str]) -> None:
    """Refuse any fake port unless ``SSC_ENV`` is ``dev`` or ``test``."""
    fakes = [
        name
        for name, value in (("runtime_driver", ports.runtime_driver),)
        if isinstance(value, FakeRuntimeDriver)
    ]
    if fakes and env.get(ENV_ENV) not in FAKE_ENVIRONMENTS:
        raise CompositionError(
            f"fake {', '.join(fakes)} needs {ENV_ENV} in {sorted(FAKE_ENVIRONMENTS)}"
        )


def compose_ports(env: Mapping[str, str]) -> Ports:
    """The production ``Ports`` from the environment. The one place ports are chosen."""
    ports = Ports(engine=make_engine(env[DSN_ENV]), runtime_driver=runtime_driver_from_env(env))
    refuse_fakes(ports, env)
    return ports


async def run_worker(
    app: App,
    ports: Ports,
    *,
    settings: WorkerSettings | None = None,
    wait: bool = True,
    install_signal_handlers: bool = True,
) -> None:
    """Run ``app``'s worker with ``ports`` in every job's context until stopped (SIGINT, SIGTERM
    or cancellation, each a graceful stop)."""
    s = settings or WorkerSettings()
    async with app.open_async():
        await app.run_worker_async(
            additional_context={PORTS_KEY: ports},
            install_signal_handlers=install_signal_handlers,
            update_heartbeat_interval=s.heartbeat_seconds,
            stalled_worker_timeout=s.stalled_worker_timeout,
            fetch_job_polling_interval=s.polling_seconds,
            concurrency=s.concurrency,
            delete_jobs=s.delete_jobs,
            wait=wait,
        )


async def run(env: Mapping[str, str] | None = None) -> None:
    e = os.environ if env is None else env
    if not e.get(DSN_ENV):
        raise CompositionError(f"{DSN_ENV} is not set")
    ports = compose_ports(e)
    try:
        await run_worker(build_app(e[DSN_ENV]), ports)
    finally:
        await ports.engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run())
    except CompositionError as exc:
        sys.stderr.write(f"ssc worker: {exc}\n")
        return 2
    return 0


__all__ = [
    "PORTS_KEY",
    "CompositionError",
    "Ports",
    "PortsMissingError",
    "WorkerSettings",
    "build_app",
    "compose_ports",
    "core_blueprint",
    "ports_of",
    "queue_conninfo",
    "refuse_fakes",
    "retry_stalled",
    "run",
    "run_worker",
    "runtime_driver_from_env",
]

if __name__ == "__main__":
    sys.exit(main())
