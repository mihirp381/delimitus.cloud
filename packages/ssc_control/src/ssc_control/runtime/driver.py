"""What should be running: the pure function that turns database rows into a ``ServiceSpec``
(decision 014). The protocol and its types live in ``ssc_shared.runtime`` so the cell agent, which
runs them on Cloud Run, speaks them too; they are re-exported here.

Every ``RuntimeDriver`` passes ``conformance/ssc_conformance/contracts/runtime_driver.py``:
``FakeRuntimeDriver`` (``fake.py``), ``ssc_agent.cloud_run.CloudRunDriver`` and
``CellAgentDriver`` (``cell_agent.py``), which reaches the Cloud Run driver through the cell agent.
"""

from dataclasses import dataclass
from typing import Final, Literal

from ssc_contracts import app_env
from ssc_contracts.manifest import Manifest, max_instances
from ssc_shared.runtime import (
    FINGERPRINT_VERSION,
    SERVICE_PREFIX,
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

# Frameworks that keep per-user state in process memory; more than one instance breaks them.
# Anything else declares ``sessions = true`` in ssc.toml.
SESSION_FRAMEWORKS: Final = frozenset({"streamlit"})

EnvName = Literal["prod", "preview"]
AppStatus = Literal["active", "disabled", "quarantined"]


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


def min_instances_for(env_name: EnvName, manifest: Manifest) -> int:
    """One warm instance for production apps that use a database, connections or internet
    access (C10): their private network path can take a minute to connect after a cold start."""
    needs_network = (
        manifest.state.postgres or bool(manifest.connections.names) or bool(manifest.egress.hosts)
    )
    return 1 if env_name == "prod" and needs_network else 0


def max_instances_for(manifest: Manifest, framework: str | None) -> int:
    """The class limit, or 1 for session apps and session frameworks (C11). A start command that
    names a session framework anywhere counts too (``uv run streamlit run app.py``); a false match
    only lowers the ceiling, which is the safe direction."""
    if framework is not None and framework.lower() in SESSION_FRAMEWORKS:
        return 1
    start = manifest.runtime.start or ""
    if any(token.rsplit("/", 1)[-1] in SESSION_FRAMEWORKS for token in start.split()):
        return 1
    return max_instances(manifest.runtime)


def desired_for(
    *,
    env: EnvironmentRow,
    release: ReleaseRow,
    manifest: Manifest,
    app_status: AppStatus,
    framework: str | None = None,
) -> ServiceSpec | Stopped:
    """What should be running for one app environment. Pure: rows in, spec out. ``framework`` is
    what the build detected (B4 records it); None until builds do."""
    service = service_name(env.id)
    if app_status != "active":
        return Stopped(service=service, reason=app_status)
    runtime = manifest.runtime
    return ServiceSpec(
        service=service,
        image_digest=release.image_digest,
        port=runtime.port,
        health_path=runtime.health_path,
        resource_class=runtime.class_,
        env={app_env.PORT: str(runtime.port), app_env.HOME: app_env.HOME_VALUE},
        min_instances=min_instances_for(env.name, manifest),
        max_instances=max_instances_for(manifest, framework),
        labels={"ssc-org": env.org_id, "ssc-app": env.app_id, "ssc-env": env.id},
    )


__all__ = [
    "FINGERPRINT_VERSION",
    "SERVICE_PREFIX",
    "SESSION_FRAMEWORKS",
    "AppStatus",
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
    "min_instances_for",
    "revision_fingerprint",
    "service_name",
]
