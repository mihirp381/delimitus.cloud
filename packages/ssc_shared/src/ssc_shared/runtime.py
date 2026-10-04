"""The runtime driver protocol and the types it speaks (decision 014).

The control plane (``ssc_control.runtime``) decides what should run; the cell agent
(``ssc_agent``) runs it on Cloud Run. Both speak this protocol, so it lives below both.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol, cast, get_args

from ssc_contracts import app_database
from ssc_contracts.app_env import secret_name_problem
from ssc_contracts.manifest import (
    RESOURCE_CLASSES,
    ResourceClass,
    ResourceClassName,
    Runtime,
    is_session_app,
)

SERVICE_PREFIX: Final = "ssc-a-"
FINGERPRINT_VERSION: Final = "ssc-spec-v2"
MAX_TIMEOUT_SECONDS: Final = 3600
SESSION_TIMEOUT_SECONDS: Final = MAX_TIMEOUT_SECONDS
REQUEST_TIMEOUT_SECONDS: Final = 300
MAX_CONCURRENCY: Final = 1000

Billing = Literal["instance", "request"]
BILLINGS: Final[tuple[Billing, ...]] = get_args(Billing)

_ENV_ID = re.compile(r"env_([a-z0-9]{20})")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SERVICE_NAME: Final = re.compile(re.escape(SERVICE_PREFIX) + r"[a-z0-9]{20}")
SECRET_ID: Final = re.compile(re.escape(SERVICE_PREFIX) + r"[a-z0-9]{20}-[A-Z][A-Z0-9_]{0,63}")
SECRET_VERSION: Final = re.compile(r"[1-9][0-9]{0,18}")
CONNECTION_SECRET_PREFIX: Final = "ssc-conn-"  # noqa: S105
CONNECTION_SECRET_ID: Final = re.compile(re.escape(CONNECTION_SECRET_PREFIX) + r"[a-z0-9]{20}")
CONNECTION_ENV_PREFIX: Final = "SSC_CONNECTION_"
CONNECTION_ID: Final = re.compile(r"con_([a-z0-9]{20})")


def billing_for(runtime: Runtime, framework: str | None = None) -> Billing:
    """``instance`` for a session app (``is_session_app``): its one instance is billed while it
    runs. ``request`` for any other: billed only while it answers."""
    return "instance" if is_session_app(runtime, framework) else "request"


def timeout_for(runtime: Runtime, framework: str | None = None) -> int:
    """Seconds Cloud Run lets one request run: ``SESSION_TIMEOUT_SECONDS`` for a session app, so
    one WebSocket can last an hour; ``REQUEST_TIMEOUT_SECONDS`` for any other."""
    return (
        SESSION_TIMEOUT_SECONDS if is_session_app(runtime, framework) else REQUEST_TIMEOUT_SECONDS
    )


def service_name(environment_id: str) -> str:
    """``ssc-a-`` plus the environment id's 20 characters: unique, and under Cloud Run's 49."""
    m = _ENV_ID.fullmatch(environment_id)
    if m is None:
        raise ValueError(f"not an environment id: {environment_id!r}")
    return SERVICE_PREFIX + m.group(1)


def secret_id(service: str, name: str) -> str:
    """The cell Secret Manager id of one app environment's secret: ``<service>-<NAME>``, so the
    ``ssc-a-*`` IAM conditions cover it and the service it belongs to is its prefix. The app
    database's own secrets (``app_database.SECRETS``) are the only platform names allowed."""
    if SERVICE_NAME.fullmatch(service) is None:
        raise ValueError(f"not an SSC app service name: {service!r}")
    if (problem := _secret_problem(name)) is not None:
        raise ValueError(f"secret name {name!r} {problem}")
    return f"{service}-{name}"


def database_name(service: str) -> str:
    """The app database of a service, and its login role: ``app_`` plus the environment id's 20
    characters (decision 003)."""
    if SERVICE_NAME.fullmatch(service) is None:
        raise ValueError(f"not an SSC app service name: {service!r}")
    return "app_" + service.removeprefix(SERVICE_PREFIX)


def connection_secret_id(connection_id: str) -> str:
    """The cell Secret Manager id holding one customer connection's credentials (SSC-051):
    ``ssc-conn-`` plus the connection id's 20 characters. Only the data gateway's identity may
    read it, and only because the secret carries the cell's connection tag."""
    m = CONNECTION_ID.fullmatch(connection_id)
    if m is None:
        raise ValueError(f"not a connection id: {connection_id!r}")
    return CONNECTION_SECRET_PREFIX + m.group(1)


def connection_env(connection_id: str) -> str:
    """The data gateway's variable for one connection: ``SSC_CONNECTION_CON_<20>``, the
    connection id in upper case (``ssc_datagw.settings``)."""
    if CONNECTION_ID.fullmatch(connection_id) is None:
        raise ValueError(f"not a connection id: {connection_id!r}")
    return CONNECTION_ENV_PREFIX + connection_id.upper()


def _secret_problem(name: str) -> str | None:
    return None if name in app_database.SECRETS else secret_name_problem(name)


def is_image_digest(value: str) -> bool:
    return _DIGEST.fullmatch(value) is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceSpec:
    """One app environment's service as it should be. Images are by digest only; a tag can never
    reach a runtime. ``spec_fingerprint`` covers what defines a revision (image, port, health
    path, class, environment, secrets, billing, request timeout, concurrency); scaling and labels
    are service settings outside it. ``billing`` is ``instance`` (CPU always allocated) or
    ``request``; ``concurrency`` is the most requests one instance takes at once. ``secrets``
    maps an environment variable to the pinned version of the secret ``secret_id(service,
    name)``: references only, never a value (SSC-026)."""

    service: str
    image_digest: str
    port: int
    health_path: str
    resource_class: ResourceClassName
    env: Mapping[str, str]
    billing: Billing
    timeout_seconds: int
    concurrency: int
    min_instances: int
    max_instances: int
    labels: Mapping[str, str]
    secrets: Mapping[str, str] = field(default_factory=dict[str, str])
    spec_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.service.startswith(SERVICE_PREFIX):
            raise ValueError(f"service names start with {SERVICE_PREFIX!r}: {self.service!r}")
        if not is_image_digest(self.image_digest):
            raise ValueError(f"images are pinned by sha256 digest only: {self.image_digest!r}")
        if not 0 <= self.min_instances <= self.max_instances:
            raise ValueError("need 0 <= min_instances <= max_instances")
        if self.billing not in BILLINGS:
            raise ValueError(f"billing is one of {', '.join(BILLINGS)}: {self.billing!r}")
        if not 1 <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"need 1 <= timeout_seconds <= {MAX_TIMEOUT_SECONDS}")
        if not 1 <= self.concurrency <= MAX_CONCURRENCY:
            raise ValueError(f"need 1 <= concurrency <= {MAX_CONCURRENCY}")
        _check_secrets(self.secrets, self.env)
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "secrets", MappingProxyType(dict(self.secrets)))
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
                secrets=self.secrets,
                billing=self.billing,
                timeout_seconds=self.timeout_seconds,
                concurrency=self.concurrency,
            ),
        )

    @property
    def resources(self) -> ResourceClass:
        return RESOURCE_CLASSES[self.resource_class]


def _check_secrets(secrets: Mapping[str, str], env: Mapping[str, str]) -> None:
    for name, version in secrets.items():
        if (problem := _secret_problem(name)) is not None:
            raise ValueError(f"secret name {name!r} {problem}")
        if name in env:
            raise ValueError(f"{name!r} is both a plain variable and a secret")
        if SECRET_VERSION.fullmatch(version) is None:
            raise ValueError(f"secret {name!r} needs a numbered version, not {version!r}")


def revision_fingerprint(  # noqa: PLR0913  (keyword-only)
    *,
    image_digest: str,
    port: int,
    health_path: str,
    resource_class: ResourceClassName,
    env: Mapping[str, str],
    secrets: Mapping[str, str],
    billing: Billing,
    timeout_seconds: int,
    concurrency: int,
) -> str:
    """What makes two revisions the same. A driver computes this from a revision's actual
    configuration when it observes one, never from a label it wrote, so drift in any field shows."""
    size = RESOURCE_CLASSES[resource_class]
    return fingerprint_of(
        image_digest=image_digest,
        port=port,
        health_path=health_path,
        vcpu=size.vcpu,
        memory_mib=size.memory_mib,
        env=env,
        secrets=secrets,
        billing=billing,
        timeout_seconds=timeout_seconds,
        concurrency=concurrency,
    )


def fingerprint_of(  # noqa: PLR0913  (keyword-only)
    *,
    image_digest: str,
    port: int,
    health_path: str,
    vcpu: float,
    memory_mib: int,
    env: Mapping[str, str],
    secrets: Mapping[str, str],
    billing: Billing,
    timeout_seconds: int,
    concurrency: int,
) -> str:
    """``revision_fingerprint`` over raw sizes, for a revision whose size is no class at all."""
    if float(vcpu).is_integer():
        vcpu = int(vcpu)
    body = {
        "v": FINGERPRINT_VERSION,
        "image_digest": image_digest,
        "port": port,
        "health_path": health_path,
        "vcpu": vcpu,
        "memory_mib": memory_mib,
        "env": dict(sorted(env.items())),
        "secrets": dict(sorted(secrets.items())),
        "billing": billing,
        "timeout_seconds": timeout_seconds,
        "concurrency": concurrency,
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(raw.encode("ascii")).hexdigest()


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


# ── wire form, between the control plane and the cell agent ─────────────────────────────────


def spec_to_wire(spec: ServiceSpec) -> dict[str, object]:
    return {
        "service": spec.service,
        "image_digest": spec.image_digest,
        "port": spec.port,
        "health_path": spec.health_path,
        "resource_class": spec.resource_class,
        "env": dict(spec.env),
        "billing": spec.billing,
        "timeout_seconds": spec.timeout_seconds,
        "concurrency": spec.concurrency,
        "min_instances": spec.min_instances,
        "max_instances": spec.max_instances,
        "labels": dict(spec.labels),
        "secrets": dict(spec.secrets),
        "spec_fingerprint": spec.spec_fingerprint,
    }


def spec_from_wire(body: Mapping[str, Any]) -> ServiceSpec:
    """Raises ``ValueError`` for anything malformed, including a fingerprint the fields do not
    hash to (two sides that disagree on the fingerprint version)."""
    try:
        resource_class = body["resource_class"]
        if resource_class not in RESOURCE_CLASSES:
            raise ValueError(f"unknown resource class {resource_class!r}")
        billing = body["billing"]
        if billing not in BILLINGS:
            raise ValueError(f"unknown billing {billing!r}")
        spec = ServiceSpec(
            service=_str(body["service"]),
            image_digest=_str(body["image_digest"]),
            port=_int(body["port"]),
            health_path=_str(body["health_path"]),
            resource_class=resource_class,
            env=_str_map(body["env"]),
            billing=billing,
            timeout_seconds=_int(body["timeout_seconds"]),
            concurrency=_int(body["concurrency"]),
            min_instances=_int(body["min_instances"]),
            max_instances=_int(body["max_instances"]),
            labels=_str_map(body["labels"]),
            secrets=_str_map(body["secrets"]),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed service spec: {exc}") from None
    if body.get("spec_fingerprint") != spec.spec_fingerprint:
        raise ValueError("spec_fingerprint does not match the spec's fields")
    return spec


def observation_to_wire(seen: ServiceObservation) -> dict[str, object]:
    return {
        "service": seen.service,
        "revisions": [
            {
                "revision": r.revision,
                "spec_fingerprint": r.spec_fingerprint,
                "image_digest": r.image_digest,
                "ready": r.ready,
                "failed": r.failed,
                "traffic_percent": r.traffic_percent,
            }
            for r in seen.revisions
        ],
        "min_instances": seen.min_instances,
        "max_instances": seen.max_instances,
        "stopped": seen.stopped,
    }


def observation_from_wire(body: Mapping[str, Any]) -> ServiceObservation:
    try:
        return ServiceObservation(
            service=_str(body["service"]),
            revisions=tuple(
                RevisionObservation(
                    revision=_str(r["revision"]),
                    spec_fingerprint=_str(r["spec_fingerprint"]),
                    image_digest=_str(r["image_digest"]),
                    ready=_opt_bool(r["ready"]),
                    failed=_bool(r["failed"]),
                    traffic_percent=_int(r["traffic_percent"]),
                )
                for r in body["revisions"]
            ),
            min_instances=_int(body["min_instances"]),
            max_instances=_int(body["max_instances"]),
            stopped=_bool(body["stopped"]),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed service observation: {exc}") from None


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {type(value).__name__}")
    return value


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected an integer, got {type(value).__name__}")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"expected a boolean, got {type(value).__name__}")
    return value


def _opt_bool(value: object) -> bool | None:
    return None if value is None else _bool(value)


def _str_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError(f"expected an object, got {type(value).__name__}")
    items = cast("dict[object, object]", value).items()
    return {_str(k): _str(v) for k, v in items}
