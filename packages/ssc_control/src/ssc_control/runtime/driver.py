"""The runtime driver seam: what the control plane asks of a container runtime, and the pure
function that turns database rows into what should be running (decision 014).

The Cloud Run implementation waits for SSC-001 (the cloud) and SSC-013 (a cell). Until then
``FakeRuntimeDriver`` in ``fake.py`` implements this protocol, and every implementation must pass
``conformance/ssc_conformance/contracts/runtime_driver.py``.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal, Protocol

from ssc_contracts import app_env
from ssc_contracts.manifest import (
    RESOURCE_CLASSES,
    Manifest,
    ResourceClass,
    ResourceClassName,
    max_instances,
)

SERVICE_PREFIX: Final = "ssc-a-"
FINGERPRINT_VERSION: Final = "ssc-spec-v1"
# Frameworks that keep per-user state in process memory; more than one instance breaks them.
# Anything else declares ``sessions = true`` in ssc.toml.
SESSION_FRAMEWORKS: Final = frozenset({"streamlit"})

_ENV_ID = re.compile(r"env_([a-z0-9]{20})")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

EnvName = Literal["prod", "preview"]
AppStatus = Literal["active", "disabled", "quarantined"]


def service_name(environment_id: str) -> str:
    """``ssc-a-`` plus the environment id's 20 characters: unique, and under Cloud Run's 49."""
    m = _ENV_ID.fullmatch(environment_id)
    if m is None:
        raise ValueError(f"not an environment id: {environment_id!r}")
    return SERVICE_PREFIX + m.group(1)


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceSpec:
    """One app environment's service as it should be. Images are by digest only; a tag can never
    reach a runtime. ``spec_fingerprint`` covers what defines a revision (image, port, health
    path, class, environment); scaling and labels are service settings outside it."""

    service: str
    image_digest: str
    port: int
    health_path: str
    resource_class: ResourceClassName
    env: Mapping[str, str]
    min_instances: int
    max_instances: int
    labels: Mapping[str, str]
    spec_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.service.startswith(SERVICE_PREFIX):
            raise ValueError(f"service names start with {SERVICE_PREFIX!r}: {self.service!r}")
        if not _DIGEST.fullmatch(self.image_digest):
            raise ValueError(f"images are pinned by sha256 digest only: {self.image_digest!r}")
        if not 0 <= self.min_instances <= self.max_instances:
            raise ValueError("need 0 <= min_instances <= max_instances")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))
        object.__setattr__(
            self,
            "spec_fingerprint",
            revision_fingerprint(
                image_digest=self.image_digest,
                port=self.port,
                health_path=self.health_path,
                resource_class=self.resource_class,
                env=self.env,
            ),
        )

    @property
    def resources(self) -> ResourceClass:
        return RESOURCE_CLASSES[self.resource_class]


def revision_fingerprint(
    *,
    image_digest: str,
    port: int,
    health_path: str,
    resource_class: ResourceClassName,
    env: Mapping[str, str],
) -> str:
    """What makes two revisions the same. A driver computes this from a revision's actual
    configuration when it observes one, never from a label it wrote, so drift in any field shows."""
    size = RESOURCE_CLASSES[resource_class]
    body = {
        "v": FINGERPRINT_VERSION,
        "image_digest": image_digest,
        "port": port,
        "health_path": health_path,
        "vcpu": size.vcpu,
        "memory_mib": size.memory_mib,
        "env": dict(sorted(env.items())),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(raw.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class Stopped:
    """The service must serve nothing and hold no instances. Beats everything, including a
    missing service: a disabled app is never created or restarted."""

    service: str
    reason: Literal["disabled", "quarantined"]


@dataclass(frozen=True, slots=True, kw_only=True)
class RevisionObservation:
    revision: str
    spec_fingerprint: str
    image_digest: str
    ready: bool | None  # None: still starting
    failed: bool
    traffic_percent: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceObservation:
    service: str
    revisions: tuple[RevisionObservation, ...]
    min_instances: int
    max_instances: int
    stopped: bool


class RuntimeDriverError(Exception):
    """The runtime refused or failed a call. The reconciler logs it and tries again next pass."""


class ServiceNotFoundError(RuntimeDriverError):
    pass


class RevisionNotFoundError(RuntimeDriverError):
    pass


class RuntimeDriver(Protocol):
    async def apply(self, spec: ServiceSpec) -> str:
        """Create or update the service and return the revision for ``spec.spec_fingerprint``:
        an existing one if there is one, else a new one. Never moves traffic: a new revision
        gets none, except a new service's first revision, which is all there is to serve. Sets
        the service's scaling and labels to the spec's and clears ``stopped``."""
        ...

    async def set_traffic(self, service: str, revision: str) -> None:
        """Send 100% of the service's traffic to ``revision``."""
        ...

    async def scale_to_zero(self, service: str) -> None:
        """Serve nothing and hold no instances until the next ``apply``. Revisions are kept."""
        ...

    async def observe(self, service: str) -> ServiceObservation | None:
        """What is running, or None when the service does not exist."""
        ...


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
