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
import base64
import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast

from procrastinate import App, Blueprint, JobContext, PsycopgConnector
from procrastinate.exceptions import ConnectorException, UniqueViolation
from procrastinate.jobs import Status
from procrastinate.manager import QUEUEING_LOCK_CONSTRAINT
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ssc_control.audit import jobs as audit_jobs
from ssc_control.cell import jobs as cell_jobs
from ssc_control.cell.deployer import CellDeployer, FakeCellDeployer, cell_deployer_from_env
from ssc_control.db.catalog import QUEUE_SCHEMA
from ssc_control.db.engine import make_engine
from ssc_control.deploy import jobs as deploy_jobs
from ssc_control.deploy.build_driver import BuildDriver, FakeBuildDriver
from ssc_control.deploy.cell_build import CellAgentBuildDriver
from ssc_control.deploy.gates import approvals_prod_gate
from ssc_control.github import jobs as github_jobs
from ssc_control.github.client import DEFAULT_BASE as GITHUB_BASE
from ssc_control.github.client import GitHubApp
from ssc_control.identity import jobs as identity_jobs
from ssc_control.identity.workos import DEFAULT_BASE, WorkOSClient
from ssc_control.lifecycle import jobs as lifecycle_jobs
from ssc_control.metrics import MetricsKeyError, metrics_port, parse_master_key
from ssc_control.metrics import jobs as metrics_jobs
from ssc_control.notifications import jobs as notify_jobs
from ssc_control.notifications.mailer import LogMailer, Mailer, Security, SmtpConfig, SmtpMailer
from ssc_control.ports import MetricsPort
from ssc_control.runtime import jobs as runtime_jobs
from ssc_control.runtime.app_databases import AppDatabases, CellAppDatabases, FakeAppDatabases
from ssc_control.runtime.cell_agent import CellAgentDriver, MetadataIdTokens
from ssc_control.runtime.cell_egress import AgentCellEgress, CellEgress, FakeCellEgress
from ssc_control.runtime.cell_usage import AgentCellUsage
from ssc_control.runtime.driver import AppIdentity, RuntimeDriver
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
from ssc_control.timers.https import HttpsScheduleDispatcher, ScheduleSigner
from ssc_control.timers.service import Timers
from ssc_control.worker_ports import APPS_DOMAIN, PORTS_KEY, Ports, PortsMissingError, ports_of
from ssc_shared import redaction
from ssc_shared.blobstore import BlobStore
from ssc_shared.hosts import check_apps_domain, check_cell_label
from ssc_shared.usage import CellUsage

log = logging.getLogger(__name__)

DSN_ENV: Final = "SSC_DATABASE_DSN"
ENV_ENV: Final = "SSC_ENV"
RUNTIME_DRIVER_ENV: Final = "SSC_RUNTIME_DRIVER"
CELL_AGENT_URL_ENV: Final = "SSC_CELL_AGENT_URL"
BUILD_DRIVER_ENV: Final = "SSC_BUILD_DRIVER"
METRICS_KEY_ENV: Final = "SSC_METRICS_KEY"
TIMER_DISPATCHER_ENV: Final = "SSC_TIMER_DISPATCHER"
TIMER_SIGNING_KEY_ENV: Final = "SSC_TIMER_SIGNING_KEY"
TIMER_KEY_ID_ENV: Final = "SSC_TIMER_KEY_ID"
IDENTITY_JWKS_ENV: Final = "SSC_IDENTITY_JWKS"
IDENTITY_ISSUER_ENV: Final = "SSC_IDENTITY_ISSUER"
APPS_DOMAIN_ENV: Final = "SSC_APPS_DOMAIN"
ISSUER_PREFIX: Final = "https://keys.delimitus.com/"
WORKOS_KEY_ENV: Final = "SSC_WORKOS_API_KEY"
WORKOS_CLIENT_ENV: Final = "SSC_WORKOS_CLIENT_ID"
WORKOS_BASE_ENV: Final = "SSC_WORKOS_BASE"
GITHUB_APP_ID_ENV: Final = "SSC_GITHUB_APP_ID"
GITHUB_KEY_ENV: Final = "SSC_GITHUB_PRIVATE_KEY"
GITHUB_BASE_ENV: Final = "SSC_GITHUB_API_BASE"
MAIL_TRANSPORT_ENV: Final = "SSC_MAIL_TRANSPORT"
SMTP_HOST_ENV: Final = "SSC_SMTP_HOST"
SMTP_PORT_ENV: Final = "SSC_SMTP_PORT"
SMTP_TLS_ENV: Final = "SSC_SMTP_TLS"
SMTP_USER_ENV: Final = "SSC_SMTP_USER"
SMTP_PASSWORD_ENV: Final = "SSC_SMTP_PASSWORD"  # noqa: S105  (an environment variable name)
MAIL_FROM_ENV: Final = "SSC_MAIL_FROM"
CONSOLE_URL_ENV: Final = "SSC_CONSOLE_URL"
DEV_CONSOLE_URL: Final = "http://localhost:5173"
SMTP_PORTS: Final[dict[str, int]] = {"starttls": 587, "tls": 465}
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
    concurrency: int = 4
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
    app.add_tasks_from(cell_jobs.blueprint(), namespace="cell")
    app.add_tasks_from(identity_jobs.blueprint(), namespace="identity")
    app.add_tasks_from(metrics_jobs.blueprint(), namespace="metrics")
    app.add_tasks_from(github_jobs.blueprint(), namespace="github")
    app.add_tasks_from(notify_jobs.blueprint(), namespace="notify")
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


def app_databases_from_env(env: Mapping[str, str]) -> AppDatabases | None:
    """App databases (SSC-040) go with ``SSC_RUNTIME_DRIVER``: none without a runtime, in
    memory with ``fake``, and through the same cell agent with ``cell_agent``."""
    match env.get(RUNTIME_DRIVER_ENV, ""):
        case "fake":
            return FakeAppDatabases()
        case "cell_agent":
            url = env.get(CELL_AGENT_URL_ENV, "")
            if not url.startswith("https://"):
                raise CompositionError(f"{CELL_AGENT_URL_ENV} must be an https URL")
            return CellAppDatabases(url, MetadataIdTokens())
        case _:
            return None


def cell_egress_from_env(env: Mapping[str, str]) -> CellEgress | None:
    """Proxy credentials (SSC-053) go with ``SSC_RUNTIME_DRIVER`` like app databases: none
    without a runtime, in memory with ``fake``, and through the cell agent with ``cell_agent``."""
    match env.get(RUNTIME_DRIVER_ENV, ""):
        case "fake":
            return FakeCellEgress()
        case "cell_agent":
            url = env.get(CELL_AGENT_URL_ENV, "")
            if not url.startswith("https://"):
                raise CompositionError(f"{CELL_AGENT_URL_ENV} must be an https URL")
            return AgentCellEgress(url, MetadataIdTokens())
        case _:
            return None


def cell_usage_from_env(env: Mapping[str, str]) -> CellUsage | None:
    """Usage reads (SSC-028) go through the cell agent with ``SSC_RUNTIME_DRIVER=cell_agent``;
    otherwise there is no cell to read and the usage collection does nothing."""
    if env.get(RUNTIME_DRIVER_ENV, "") != "cell_agent":
        return None
    url = env.get(CELL_AGENT_URL_ENV, "")
    if not url.startswith("https://"):
        raise CompositionError(f"{CELL_AGENT_URL_ENV} must be an https URL")
    return AgentCellUsage(url, MetadataIdTokens())


def build_driver_from_env(
    env: Mapping[str, str], blob_store: BlobStore | None = None
) -> BuildDriver | None:
    """``SSC_BUILD_DRIVER``: unset means none (builds fail with ``BUILD_DRIVER_UNAVAILABLE``),
    ``fake`` the in-memory builder, ``cell_agent`` Cloud Build in the cell through its agent at
    ``SSC_CELL_AGENT_URL`` (SSC-015). The real builder needs the blob store: it signs the
    bundle's URL there, and the build job analyses the bundle from it."""
    match env.get(BUILD_DRIVER_ENV, ""):
        case "":
            return None
        case "fake":
            return FakeBuildDriver()
        case "cell_agent":
            url = env.get(CELL_AGENT_URL_ENV, "")
            if not url.startswith("https://"):
                raise CompositionError(f"{CELL_AGENT_URL_ENV} must be an https URL")
            if blob_store is None:
                raise CompositionError(f"{BUILD_DRIVER_ENV}=cell_agent needs SSC_BLOB_* set")
            return CellAgentBuildDriver(url, MetadataIdTokens(), blob_store)
        case other:
            raise CompositionError(f"unknown {BUILD_DRIVER_ENV} {other!r}")


def timer_dispatcher_from_env(env: Mapping[str, str]) -> ScheduleDispatcher | None:
    """``SSC_TIMER_DISPATCHER``: unset means none (timer runs fail with ``dispatch_unavailable``),
    ``fake`` the in-memory dispatcher, ``https`` calls through each app's public host
    (``SSC_APPS_DOMAIN``) with tokens signed by ``SSC_TIMER_SIGNING_KEY``, a P-256 PEM, under
    ``SSC_TIMER_KEY_ID`` (SSC-041). A missing or unreadable key refuses to start."""
    match env.get(TIMER_DISPATCHER_ENV, ""):
        case "":
            return None
        case "fake":
            return FakeScheduleDispatcher()
        case "https":
            pem, kid = env.get(TIMER_SIGNING_KEY_ENV, ""), env.get(TIMER_KEY_ID_ENV, "")
            if not pem or not kid:
                raise CompositionError(
                    f"{TIMER_DISPATCHER_ENV}=https needs {TIMER_SIGNING_KEY_ENV} and "
                    f"{TIMER_KEY_ID_ENV}"
                )
            try:
                signer = ScheduleSigner(pem.encode(), kid)
                domain = check_apps_domain(env.get(APPS_DOMAIN_ENV, APPS_DOMAIN))
            except ValueError as exc:
                raise CompositionError(
                    f"{TIMER_DISPATCHER_ENV}=https: {type(exc).__name__}: the timer key or "
                    f"{APPS_DOMAIN_ENV} does not load"
                ) from None
            return HttpsScheduleDispatcher(signer, apps_domain=domain)
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


def _cell_deployer(env: Mapping[str, str]) -> CellDeployer | None:
    try:
        return cell_deployer_from_env(env)
    except ValueError as exc:
        raise CompositionError(str(exc)) from None


def app_identity_from_env(env: Mapping[str, str]) -> AppIdentity | None:
    """``SSC_IDENTITY_JWKS``, the cell's public JWKS (stack output ``identity_jwks``), and
    ``SSC_IDENTITY_ISSUER``, ``https://keys.delimitus.com/<cell label>``: one cell, as for
    ``SSC_CELL_AGENT_URL``. Either may be unset; None when both are. The JWKS is re-serialised
    compactly, so its whitespace never changes an app's spec."""
    jwks, issuer = env.get(IDENTITY_JWKS_ENV, ""), env.get(IDENTITY_ISSUER_ENV, "")
    if not jwks and not issuer:
        return None
    keys_url = None
    if jwks:
        try:
            parsed: object = json.loads(jwks)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("keys"), list):  # pyright: ignore[reportUnknownMemberType]
            raise CompositionError(f'{IDENTITY_JWKS_ENV} must be a JSON object with "keys"')
        compact = json.dumps(parsed, separators=(",", ":"), sort_keys=True).encode()
        keys_url = "data:application/json;base64," + base64.b64encode(compact).decode()
    label = None
    if issuer:
        try:
            label = check_cell_label(issuer.removeprefix(ISSUER_PREFIX))
        except ValueError:
            raise CompositionError(
                f"{IDENTITY_ISSUER_ENV} must be {ISSUER_PREFIX}<label>"
            ) from None
    try:
        domain = check_apps_domain(env.get(APPS_DOMAIN_ENV, APPS_DOMAIN))
    except ValueError as exc:
        raise CompositionError(str(exc)) from None
    return AppIdentity(keys_url=keys_url, cell_label=label, apps_domain=domain)


def directory_from_env(env: Mapping[str, str]) -> WorkOSClient | None:
    """``SSC_WORKOS_API_KEY`` and ``SSC_WORKOS_CLIENT_ID``, both or neither, and optionally
    ``SSC_WORKOS_BASE``: the client the directory sync reads WorkOS with. None skips the sync."""
    key, client_id = env.get(WORKOS_KEY_ENV, ""), env.get(WORKOS_CLIENT_ENV, "")
    if not key and not client_id:
        return None
    if not (key and client_id):
        raise CompositionError(f"set both {WORKOS_KEY_ENV} and {WORKOS_CLIENT_ENV}, or neither")
    return WorkOSClient(
        api_key=key, client_id=client_id, base=env.get(WORKOS_BASE_ENV, DEFAULT_BASE)
    )


def github_from_env(env: Mapping[str, str]) -> GitHubApp | None:
    """``SSC_GITHUB_APP_ID`` and ``SSC_GITHUB_PRIVATE_KEY`` (the App's PEM), both or neither,
    and optionally ``SSC_GITHUB_API_BASE``: the App push jobs call GitHub as. None: a push job
    does nothing."""
    app_id, key = env.get(GITHUB_APP_ID_ENV, ""), env.get(GITHUB_KEY_ENV, "")
    if not app_id and not key:
        return None
    if not (app_id and key):
        raise CompositionError(f"set both {GITHUB_APP_ID_ENV} and {GITHUB_KEY_ENV}, or neither")
    return GitHubApp(app_id=app_id, private_key=key, base=env.get(GITHUB_BASE_ENV, GITHUB_BASE))


def console_url_from_env(env: Mapping[str, str]) -> str:
    """``SSC_CONSOLE_URL``: where the console lives, the base of the link in each approval mail.
    https only, except that development and tests may use ``http://``; unset is the local
    console there and empty elsewhere."""
    url = env.get(CONSOLE_URL_ENV, "")
    fake = env.get(ENV_ENV) in FAKE_ENVIRONMENTS
    if not url:
        return DEV_CONSOLE_URL if fake else ""
    if not url.startswith("https://") and not (fake and url.startswith("http://")):
        raise CompositionError(f"{CONSOLE_URL_ENV} must be an https URL")
    return url.rstrip("/")


def mailer_from_env(env: Mapping[str, str], console_url: str) -> Mailer | None:
    """``SSC_MAIL_TRANSPORT``: unset is the log mailer in development and tests and none
    elsewhere (queued mail waits), ``log`` the log mailer, ``smtp`` plain SMTP to
    ``SSC_SMTP_HOST`` with ``SSC_SMTP_USER`` and ``SSC_SMTP_PASSWORD`` from ``SSC_MAIL_FROM``.
    ``SSC_SMTP_TLS`` is ``starttls`` (default, port 587) or ``tls`` (port 465); there is no
    plaintext setting. ``SSC_SMTP_PORT`` overrides the port. SMTP needs ``SSC_CONSOLE_URL``."""
    match env.get(MAIL_TRANSPORT_ENV, ""):
        case "":
            if env.get(ENV_ENV) in FAKE_ENVIRONMENTS:
                return LogMailer()
            log.warning("%s is not set: approval mail will wait in the outbox", MAIL_TRANSPORT_ENV)
            return None
        case "log":
            return LogMailer()
        case "smtp":
            host, user = env.get(SMTP_HOST_ENV, ""), env.get(SMTP_USER_ENV, "")
            password, sender = env.get(SMTP_PASSWORD_ENV, ""), env.get(MAIL_FROM_ENV, "")
            if not (host and user and password and sender and console_url):
                raise CompositionError(
                    f"{MAIL_TRANSPORT_ENV}=smtp needs {SMTP_HOST_ENV}, {SMTP_USER_ENV}, "
                    f"{SMTP_PASSWORD_ENV}, {MAIL_FROM_ENV} and {CONSOLE_URL_ENV}"
                )
            security = env.get(SMTP_TLS_ENV, "starttls")
            if security not in SMTP_PORTS:
                raise CompositionError(f"{SMTP_TLS_ENV} must be starttls or tls")
            try:
                port = int(env.get(SMTP_PORT_ENV, SMTP_PORTS[security]))
            except ValueError:
                raise CompositionError(f"{SMTP_PORT_ENV} must be a port number") from None
            config = SmtpConfig(
                host=host,
                port=port,
                security=cast(Security, security),
                username=user,
                password=password,
                sender=sender,
            )
            return SmtpMailer(config)
        case other:
            raise CompositionError(f"unknown {MAIL_TRANSPORT_ENV} {other!r}")


def apps_domain_from_env(env: Mapping[str, str]) -> str:
    try:
        return check_apps_domain(env.get(APPS_DOMAIN_ENV, APPS_DOMAIN))
    except ValueError as exc:
        raise CompositionError(str(exc)) from None


def refuse_fakes(ports: Ports, env: Mapping[str, str]) -> None:
    """Refuse any fake port unless ``SSC_ENV`` is ``dev`` or ``test``."""
    fakes = [
        name
        for name, value in (
            ("runtime_driver", ports.runtime_driver),
            ("build_driver", ports.build_driver),
            ("timer_dispatcher", ports.timer_dispatcher),
            ("cell_deployer", ports.cell_deployer),
            ("app_databases", ports.app_databases),
            ("cell_egress", ports.cell_egress),
            ("mailer", ports.mailer),
        )
        if isinstance(
            value,
            FakeRuntimeDriver
            | FakeBuildDriver
            | FakeScheduleDispatcher
            | FakeCellDeployer
            | FakeAppDatabases
            | FakeCellEgress
            | LogMailer,
        )
    ]
    if fakes and env.get(ENV_ENV) not in FAKE_ENVIRONMENTS:
        raise CompositionError(
            f"fake {', '.join(fakes)} needs {ENV_ENV} in {sorted(FAKE_ENVIRONMENTS)}"
        )


def compose_ports(env: Mapping[str, str]) -> Ports:
    """The production ``Ports`` from the environment. The one place ports are chosen."""
    engine = make_engine(env[DSN_ENV])
    blob_store, cell_stores = blob_store_of(env), cell_stores_of(env)
    console_url = console_url_from_env(env)
    ports = Ports(
        engine=engine,
        runtime_driver=runtime_driver_from_env(env),
        release_specs=BundleReleaseSpecs(),
        blob_store=blob_store,
        cell_stores=cell_stores,
        snapshot=Snapshots(engine, blob_store=blob_store, cell_stores=cell_stores),
        prod_gate=approvals_prod_gate(),
        build_driver=build_driver_from_env(env, blob_store),
        metrics=metrics_from_env(env),
        timers=Timers(),
        timer_dispatcher=timer_dispatcher_from_env(env),
        cell_deployer=_cell_deployer(env),
        app_databases=app_databases_from_env(env),
        app_identity=app_identity_from_env(env),
        directory=directory_from_env(env),
        cell_usage=cell_usage_from_env(env),
        github=github_from_env(env),
        apps_domain=apps_domain_from_env(env),
        cell_egress=cell_egress_from_env(env),
        mailer=mailer_from_env(env, console_url),
        console_url=console_url,
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
        if ports.directory is not None:
            await ports.directory.aclose()
        if ports.github is not None:
            await ports.github.aclose()
        if isinstance(ports.timer_dispatcher, HttpsScheduleDispatcher):
            await ports.timer_dispatcher.aclose()
        await ports.engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    redaction.install()
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
    "app_databases_from_env",
    "blob_store_of",
    "cell_stores_of",
    "build_app",
    "build_driver_from_env",
    "cell_usage_from_env",
    "compose_ports",
    "console_url_from_env",
    "core_blueprint",
    "directory_from_env",
    "mailer_from_env",
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
