"""The worker: one Procrastinate app, every lane's tasks, and the composition root (decision 014).

``python -m ssc_control.worker`` reads ``SSC_DATABASE_DSN`` (the ``ssc_app`` DSN the API uses),
builds the ``Ports`` and runs until SIGINT or SIGTERM. Each lane adds its tasks with one
``add_tasks_from`` line in ``build_app``, from a blueprint factory.

``stalled_sweep`` is decision 008's recovery: a job left in ``doing`` by a worker whose heartbeat
stopped (a SIGKILL, a lost node) is put back to ``todo``, unless a waiting twin already holds its
``queueing_lock``. Procrastinate 3.10 has no single call for this, so the sweep is
``get_stalled_jobs`` then ``retry_job``. Every task must therefore be safe to run twice.

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
from procrastinate.exceptions import ConnectorException, UniqueViolation
from procrastinate.jobs import Status
from procrastinate.manager import QUEUEING_LOCK_CONSTRAINT
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ssc_control.audit import jobs as audit_jobs
from ssc_control.db.catalog import QUEUE_SCHEMA
from ssc_control.db.engine import make_engine
from ssc_control.deploy import jobs as deploy_jobs
from ssc_control.deploy.build_driver import BuildDriver, FakeBuildDriver
from ssc_control.deploy.gates import approvals_prod_gate
from ssc_control.lifecycle import jobs as lifecycle_jobs
from ssc_control.metrics import MetricsKeyError, metrics_port, parse_master_key
from ssc_control.ports import MetricsPort
from ssc_control.runtime import jobs as runtime_jobs
from ssc_control.runtime.cell_agent import CellAgentDriver, MetadataIdTokens
from ssc_control.runtime.driver import RuntimeDriver
from ssc_control.runtime.fake import FakeRuntimeDriver
from ssc_control.runtime.specs import BundleReleaseSpecs
from ssc_control.snapshot import jobs as snapshot_jobs
from ssc_control.snapshot.service import Snapshots
from ssc_control.storage import (
    CellStores,
    StorageConfigError,
    blob_store_from_env,
    cell_stores_from_env,
)
from ssc_control.timers import jobs as timers_jobs
from ssc_control.timers.dispatch import FakeScheduleDispatcher, ScheduleDispatcher
from ssc_control.timers.service import Timers
from ssc_control.worker_ports import PORTS_KEY, Ports, PortsMissingError, ports_of
from ssc_shared.blobstore import BlobStore

log = logging.getLogger(__name__)

DSN_ENV: Final = "SSC_DATABASE_DSN"
ENV_ENV: Final = "SSC_ENV"
RUNTIME_DRIVER_ENV: Final = "SSC_RUNTIME_DRIVER"
CELL_AGENT_URL_ENV: Final = "SSC_CELL_AGENT_URL"
BUILD_DRIVER_ENV: Final = "SSC_BUILD_DRIVER"
METRICS_KEY_ENV: Final = "SSC_METRICS_KEY"
TIMER_DISPATCHER_ENV: Final = "SSC_TIMER_DISPATCHER"
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
    """``get_stalled_jobs`` then ``retry_job`` for each (decision 008); how many were handled.

    A stalled job whose ``queueing_lock`` a waiting (``todo``) twin already holds cannot go back
    to ``todo``: it is marked ``failed`` instead, since every queueing lock names one unit of
    work and the twin does it. Any other refusal is logged and left to the next sweep."""
    stalled = list(await app.job_manager.get_stalled_jobs(seconds_since_heartbeat=seconds))
    handled = 0
    for job in stalled:
        extra = {"job_id": job.id, "task_name": job.task_name}
        try:
            try:
                await app.job_manager.retry_job(job)
                log.warning("stalled job retried", extra=extra)
            except UniqueViolation as exc:
                if exc.constraint_name != QUEUEING_LOCK_CONSTRAINT:
                    raise
                await app.job_manager.finish_job(job, status=Status.FAILED, delete_job=False)
                log.warning("stalled job superseded by its waiting twin", extra=extra)
        except ConnectorException:
            log.exception("stalled job not retried", extra=extra)
            continue
        handled += 1
    return handled


def build_app(dsn: str, *, settings: WorkerSettings | None = None) -> App:
    """The worker's Procrastinate app with every lane's tasks. Not opened."""
    s = settings or WorkerSettings()
    app = App(connector=PsycopgConnector(conninfo=queue_conninfo(dsn)))
    app.add_tasks_from(
        core_blueprint(sweep_cron=s.sweep_cron, stalled_after_seconds=s.stalled_after_seconds),
        namespace="core",
    )
    app.add_tasks_from(runtime_jobs.blueprint(tick_cron=s.tick_cron), namespace="runtime")
    app.add_tasks_from(snapshot_jobs.blueprint(), namespace="snapshot")
    app.add_tasks_from(deploy_jobs.blueprint(), namespace="deploy")
    app.add_tasks_from(audit_jobs.blueprint(), namespace="audit")
    app.add_tasks_from(lifecycle_jobs.blueprint(), namespace="lifecycle")
    app.add_tasks_from(timers_jobs.blueprint(), namespace="timers")
    return app


def runtime_driver_from_env(env: Mapping[str, str]) -> RuntimeDriver | None:
    """``SSC_RUNTIME_DRIVER``: unset means none (the reconciler defers nothing), ``fake`` the
    in-memory driver, ``cell_agent`` the cell agent at ``SSC_CELL_AGENT_URL`` with this
    instance's ID token. One cell until placement (which cell holds an org) has its ticket."""
    match env.get(RUNTIME_DRIVER_ENV, ""):
        case "":
            return None
        case "fake":
            return FakeRuntimeDriver()
        case "cell_agent":
            url = env.get(CELL_AGENT_URL_ENV, "")
            if not url.startswith("https://"):
                raise CompositionError(f"{CELL_AGENT_URL_ENV} must be an https URL")
            return CellAgentDriver(url, MetadataIdTokens())
        case other:
            raise CompositionError(f"unknown {RUNTIME_DRIVER_ENV} {other!r}")


def build_driver_from_env(env: Mapping[str, str]) -> BuildDriver | None:
    """``SSC_BUILD_DRIVER``: unset means none (builds fail with ``BUILD_DRIVER_UNAVAILABLE``),
    ``fake`` the in-memory builder. Cloud Build arrives with SSC-015."""
    match env.get(BUILD_DRIVER_ENV, ""):
        case "":
            return None
        case "fake":
            return FakeBuildDriver()
        case other:
            raise CompositionError(f"unknown {BUILD_DRIVER_ENV} {other!r}")


def timer_dispatcher_from_env(env: Mapping[str, str]) -> ScheduleDispatcher | None:
    """``SSC_TIMER_DISPATCHER``: unset means none (timer runs fail with ``dispatch_unavailable``),
    ``fake`` the in-memory dispatcher. The real one arrives with the cell's timer endpoint
    (SSC-018)."""
    match env.get(TIMER_DISPATCHER_ENV, ""):
        case "":
            return None
        case "fake":
            return FakeScheduleDispatcher()
        case other:
            raise CompositionError(f"unknown {TIMER_DISPATCHER_ENV} {other!r}")


def metrics_from_env(env: Mapping[str, str]) -> MetricsPort:
    """``SSC_METRICS_KEY`` keys the recorder; unset records nothing, malformed refuses to start."""
    value = env.get(METRICS_KEY_ENV)
    if not value:
        log.warning("%s is not set: no metrics events will be recorded", METRICS_KEY_ENV)
        return metrics_port(None)
    try:
        return metrics_port(parse_master_key(value))
    except MetricsKeyError as exc:
        raise CompositionError(str(exc)) from None


def blob_store_of(env: Mapping[str, str]) -> BlobStore | None:
    """``SSC_BLOB_*``, read as the API reads them (``storage.blob_store_from_env``)."""
    try:
        return blob_store_from_env(env)
    except StorageConfigError as exc:
        raise CompositionError(str(exc)) from exc


def cell_stores_of(env: Mapping[str, str]) -> CellStores | None:
    """``SSC_CELL_BUCKET_TEMPLATE`` (``storage.cell_stores_from_env``)."""
    try:
        return cell_stores_from_env(env)
    except StorageConfigError as exc:
        raise CompositionError(str(exc)) from exc


def refuse_fakes(ports: Ports, env: Mapping[str, str]) -> None:
    """Refuse any fake port unless ``SSC_ENV`` is ``dev`` or ``test``."""
    fakes = [
        name
        for name, value in (
            ("runtime_driver", ports.runtime_driver),
            ("build_driver", ports.build_driver),
            ("timer_dispatcher", ports.timer_dispatcher),
        )
        if isinstance(value, FakeRuntimeDriver | FakeBuildDriver | FakeScheduleDispatcher)
    ]
    if fakes and env.get(ENV_ENV) not in FAKE_ENVIRONMENTS:
        raise CompositionError(
            f"fake {', '.join(fakes)} needs {ENV_ENV} in {sorted(FAKE_ENVIRONMENTS)}"
        )


def compose_ports(env: Mapping[str, str]) -> Ports:
    """The production ``Ports`` from the environment. The one place ports are chosen."""
    engine = make_engine(env[DSN_ENV])
    ports = Ports(
        engine=engine,
        runtime_driver=runtime_driver_from_env(env),
        release_specs=BundleReleaseSpecs(),
        blob_store=blob_store_of(env),
        cell_stores=cell_stores_of(env),
        snapshot=Snapshots(engine),
        prod_gate=approvals_prod_gate(),
        build_driver=build_driver_from_env(env),
        metrics=metrics_from_env(env),
        timers=Timers(),
        timer_dispatcher=timer_dispatcher_from_env(env),
    )
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
    "blob_store_of",
    "cell_stores_of",
    "build_app",
    "build_driver_from_env",
    "compose_ports",
    "core_blueprint",
    "metrics_from_env",
    "ports_of",
    "queue_conninfo",
    "refuse_fakes",
    "retry_stalled",
    "run",
    "run_worker",
    "runtime_driver_from_env",
    "timer_dispatcher_from_env",
]

if __name__ == "__main__":
    sys.exit(main())
