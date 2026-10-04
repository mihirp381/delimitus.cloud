"""What should be running: the pure function that turns database rows into a ``ServiceSpec``
(decision 014). The protocol and its types live in ``ssc_shared.runtime`` so the cell agent, which
runs them on Cloud Run, speaks them too; they are re-exported here.

Every ``RuntimeDriver`` passes ``conformance/ssc_conformance/contracts/runtime_driver.py``:
``FakeRuntimeDriver`` (``fake.py``), ``ssc_agent.cloud_run.CloudRunDriver`` and
``CellAgentDriver`` (``cell_agent.py``), which reaches the Cloud Run driver through the cell agent.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from ssc_contracts import app_database, app_env, egress
from ssc_contracts.manifest import Manifest, is_session_app, max_instances
from ssc_shared.hosts import app_origin, slug_problem
from ssc_shared.runtime import (
    FINGERPRINT_VERSION,
    REQUEST_TIMEOUT_SECONDS,
    SERVICE_PREFIX,
    SESSION_TIMEOUT_SECONDS,
    Billing,
    RevisionNotFoundError,
    RevisionObservation,
    RuntimeDriver,
    RuntimeDriverError,
    ServiceNotFoundError,
    ServiceObservation,
    ServiceSpec,
    billing_for,
    database_name,
    revision_fingerprint,
    service_name,
    timeout_for,
)

EnvName = Literal["prod", "preview"]
AppStatus = Literal["active", "disabled", "quarantined"]
SESSION_CONCURRENCY: Final = 1000
REQUEST_CONCURRENCY: Final = 80


@dataclass(frozen=True, slots=True, kw_only=True)
class Stopped:
    """The service must serve nothing and hold no instances. Beats everything, including a
    missing service: a disabled app is never created or restarted."""

    service: str
    reason: Literal["disabled", "quarantined"]


@dataclass(frozen=True, slots=True, kw_only=True)
class EnvironmentRow:
    id: str
    org_id: str
    app_id: str
    name: EnvName


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseRow:
    id: str
    image_digest: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DatabaseRow:
    """Where the environment's app database is (``ssc.app_database``, SSC-040)."""

    host: str
    port: int


@dataclass(frozen=True, slots=True, kw_only=True)
class AppIdentity:
    """The cell's identity settings (SSC-018). ``keys_url`` is its public JWKS as a ``data:``
    URL; ``cell_label`` and ``apps_domain`` make each app's origin. Either is None when its
    setting is unset, and the variable it makes is then left out."""

    keys_url: str | None
    cell_label: str | None
    apps_domain: str


def identity_env(identity: AppIdentity | None, slug: str | None, env: EnvName) -> dict[str, str]:
    """``SSC_IDENTITY_KEYS_URL`` and ``SSC_APP_ORIGIN``, the identity note's keys and audience.
    No origin for a slug stored before the host rule refused it."""
    if identity is None:
        return {}
    plain: dict[str, str] = {}
    if identity.keys_url:
        plain[app_env.IDENTITY_KEYS_URL] = identity.keys_url
    if identity.cell_label and slug and slug_problem(slug) is None:
        plain[app_env.APP_ORIGIN] = app_origin(slug, env, identity.cell_label, identity.apps_domain)
    return plain


def max_instances_for(manifest: Manifest, framework: str | None) -> int:
    """The class limit, or 1 for a session app (C11), detected by
    ``ssc_contracts.manifest.is_session_app``. An app with a database runs at most
    ``app_database.MAX_INSTANCES``, which leaves a connection of its login role's limit for the
    next revision."""
    limit = max_instances(manifest.runtime, framework)
    return min(limit, app_database.MAX_INSTANCES) if manifest.state.postgres else limit


def database_env(service: str, database: DatabaseRow) -> dict[str, str]:
    """The plain ``PG*`` variables beside the ``DATABASE_URL`` and ``PGPASSWORD`` secrets."""
    name = database_name(service)
    return {
        app_env.PGHOST: database.host,
        app_env.PGPORT: str(database.port),
        app_env.PGDATABASE: name,
        app_env.PGUSER: name,
        app_env.PGSSLMODE: "verify-full",
        app_env.PGSSLROOTCERT: app_env.DATABASE_CA_PATH,
    }


def desired_for(  # noqa: PLR0913  (keyword-only)
    *,
    env: EnvironmentRow,
    release: ReleaseRow,
    manifest: Manifest,
    app_status: AppStatus,
    framework: str | None = None,
    secrets: Mapping[str, str] | None = None,
    database: DatabaseRow | None = None,
    slug: str | None = None,
    identity: AppIdentity | None = None,
    warm: bool = False,
) -> ServiceSpec | Stopped:
    """What should be running for one app environment. Pure: rows in, spec out. ``framework`` is
    what the build detected (SSC-015 records it on the release), or None. Every environment
    scales to zero except a production one the org's warm option names (``warm``, SSC-092),
    which keeps one instance; a preview environment is never warm. A session environment is
    instance-billed with the 60-minute timeout and takes 1000 requests at once, since its one
    instance holds every user's WebSocket; any other is request-billed with 5 minutes and 80.
    ``secrets`` are the versions the deployment runs (``deployment.secret_refs``), each mounted
    as its variable (SSC-026). With ``[state] postgres = true`` and its ``database``, the ``PG*``
    parts join them (SSC-040); without, the database's secrets are left out. With ``[egress]
    hosts`` and its proxy credential (``HTTPS_PROXY``), ``egress.PLAIN_ENV`` joins them
    (SSC-053); without, the credential is left out. Every app gets ``identity_env`` from
    ``identity`` and its ``slug``, as plain values: the keys are public."""
    service = service_name(env.id)
    if app_status != "active":
        return Stopped(service=service, reason=app_status)
    runtime = manifest.runtime
    session = is_session_app(runtime, framework)
    billing = billing_for(runtime, framework)
    plain = {app_env.PORT: str(runtime.port), app_env.HOME: app_env.HOME_VALUE}
    plain |= identity_env(identity, slug, env.name)
    mounted = dict(secrets or {})
    if manifest.state.postgres and database is not None:
        plain |= database_env(service, database)
    else:
        mounted = {k: v for k, v in mounted.items() if k not in app_database.SECRETS}
    if manifest.egress.hosts and app_env.HTTPS_PROXY in mounted:
        plain |= egress.PLAIN_ENV
    else:
        mounted = {k: v for k, v in mounted.items() if k not in egress.SECRETS}
    return ServiceSpec(
        service=service,
        image_digest=release.image_digest,
        port=runtime.port,
        health_path=runtime.health_path,
        resource_class=runtime.class_,
        env=plain,
        billing=billing,
        timeout_seconds=timeout_for(runtime, framework),
        concurrency=SESSION_CONCURRENCY if session else REQUEST_CONCURRENCY,
        min_instances=1 if warm and env.name == "prod" else 0,
        max_instances=max_instances_for(manifest, framework),
        labels={"ssc-org": env.org_id, "ssc-app": env.app_id, "ssc-env": env.id},
        secrets=mounted,
    )


__all__ = [
    "FINGERPRINT_VERSION",
    "REQUEST_CONCURRENCY",
    "REQUEST_TIMEOUT_SECONDS",
    "SERVICE_PREFIX",
    "SESSION_CONCURRENCY",
    "SESSION_TIMEOUT_SECONDS",
    "AppIdentity",
    "AppStatus",
    "Billing",
    "DatabaseRow",
    "EnvName",
    "EnvironmentRow",
    "ReleaseRow",
    "RevisionNotFoundError",
    "RevisionObservation",
    "RuntimeDriver",
    "RuntimeDriverError",
    "ServiceNotFoundError",
    "ServiceObservation",
    "ServiceSpec",
    "Stopped",
    "database_env",
    "desired_for",
    "identity_env",
    "max_instances_for",
    "revision_fingerprint",
    "service_name",
]
