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

from ssc_contracts import app_env
from ssc_contracts.manifest import Manifest, is_session_app, max_instances
from ssc_shared.runtime import (
    FINGERPRINT_VERSION,
    SERVICE_PREFIX,
    Billing,
    RevisionNotFoundError,
    RevisionObservation,
    RuntimeDriver,
    RuntimeDriverError,
    ServiceNotFoundError,
    ServiceObservation,
    ServiceSpec,
    revision_fingerprint,
    service_name,
)

EnvName = Literal["prod", "preview"]
AppStatus = Literal["active", "disabled", "quarantined"]
SESSION_TIMEOUT_SECONDS: Final = 3600
REQUEST_TIMEOUT_SECONDS: Final = 300
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


def max_instances_for(manifest: Manifest, framework: str | None) -> int:
    """The class limit, or 1 for a session app (C11), detected by
    ``ssc_contracts.manifest.is_session_app``."""
    return max_instances(manifest.runtime, framework)


def desired_for(  # noqa: PLR0913  (keyword-only)
    *,
    env: EnvironmentRow,
    release: ReleaseRow,
    manifest: Manifest,
    app_status: AppStatus,
    framework: str | None = None,
    secrets: Mapping[str, str] | None = None,
) -> ServiceSpec | Stopped:
    """What should be running for one app environment. Pure: rows in, spec out. ``framework`` is
    what the build detected (SSC-015 records it on the release), or None. Every environment
    scales to zero. A session environment is instance-billed with the 60-minute timeout and
    takes 1000 requests at once, since its one instance holds every user's WebSocket; any other
    is request-billed with 5 minutes and 80. ``secrets`` are the versions the deployment runs
    (``deployment.secret_refs``), each mounted as its variable (SSC-026)."""
    service = service_name(env.id)
    if app_status != "active":
        return Stopped(service=service, reason=app_status)
    runtime = manifest.runtime
    session = is_session_app(runtime, framework)
    billing: Billing = "instance" if session else "request"
    return ServiceSpec(
        service=service,
        image_digest=release.image_digest,
        port=runtime.port,
        health_path=runtime.health_path,
        resource_class=runtime.class_,
        env={app_env.PORT: str(runtime.port), app_env.HOME: app_env.HOME_VALUE},
        billing=billing,
        timeout_seconds=SESSION_TIMEOUT_SECONDS if session else REQUEST_TIMEOUT_SECONDS,
        concurrency=SESSION_CONCURRENCY if session else REQUEST_CONCURRENCY,
        min_instances=0,
        max_instances=max_instances_for(manifest, framework),
        labels={"ssc-org": env.org_id, "ssc-app": env.app_id, "ssc-env": env.id},
        secrets=dict(secrets or {}),
    )


__all__ = [
    "FINGERPRINT_VERSION",
    "REQUEST_CONCURRENCY",
    "REQUEST_TIMEOUT_SECONDS",
    "SERVICE_PREFIX",
    "SESSION_CONCURRENCY",
    "SESSION_TIMEOUT_SECONDS",
    "AppStatus",
    "Billing",
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
    "desired_for",
    "max_instances_for",
    "revision_fingerprint",
    "service_name",
]
