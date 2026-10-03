"""``RuntimeDriver`` on Cloud Run, over the Admin API v2 (SSC-017, decision 014).

Runs as the cell agent, whose IAM reaches only services and service accounts named ``ssc-a-*``
(decision 022). That rules out the operations API, so a call that must finish (``set_traffic``,
``scale_to_zero``) polls the service until ``observedGeneration`` catches up instead.

Each app environment's service:

- runs as its own service account ``<service>@<project>.iam.gserviceaccount.com``, created here
  and granted nothing;
- has ingress ``INTERNAL_ONLY``, the invoker check on, and ``run.invoker`` held by the gateway
  alone (``setIamPolicy`` replaces the policy on every ``apply``);
- sends all egress into the cell's ``apps`` subnet (Direct VPC egress), where the firewall
  allows only the cell and Google's private range;
- bills per request (``cpuIdle``) or per instance, with the spec's request timeout and
  concurrency;
- names its revisions ``<service>-<generation>-<fingerprint>``, so ``apply`` knows the revision
  it asked for before Cloud Run has made it;
- pins traffic to named revisions after the first, so a new revision never takes traffic;
- stops with manual scaling at 0 instances, which keeps every revision.

A revision's fingerprint is computed from its container as Cloud Run reports it, never from a
label, so a change made outside SSC shows as drift.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

import httpx2

from ssc_shared.runtime import (
    SERVICE_NAME,
    Billing,
    RevisionNotFoundError,
    RevisionObservation,
    RuntimeDriver,
    RuntimeDriverError,
    ServiceNotFoundError,
    ServiceObservation,
    ServiceSpec,
    fingerprint_of,
)

log = logging.getLogger(__name__)

type AccessTokens = Callable[[], Awaitable[str]]
type Json = dict[str, Any]

RUN_API: Final = "https://run.googleapis.com/v2"
IAM_API: Final = "https://iam.googleapis.com/v1"
INVOKER_ROLE: Final = "roles/run.invoker"
INGRESS: Final = "INGRESS_TRAFFIC_INTERNAL_ONLY"
REVISION_TRAFFIC: Final = "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"
LATEST_TRAFFIC: Final = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
STARTUP_PERIOD_SECONDS: Final = 5
STARTUP_TIMEOUT_SECONDS: Final = 3
STARTUP_FAILURES: Final = 24  # two minutes to start
DEFAULT_TIMEOUT_SECONDS: Final = 300
DEFAULT_CONCURRENCY: Final = 80
CALL_TIMEOUT_SECONDS: Final = 30.0
SETTLE_TIMEOUT_SECONDS: Final = 180.0
RESERVED_ENV: Final = frozenset({"PORT", "K_SERVICE", "K_REVISION", "K_CONFIGURATION"})
"""Cloud Run sets these itself and refuses them in a template; ``PORT`` is the container port."""
CONFLICT_TRIES: Final = 6
IDENTITY_TRIES: Final = 6
_HTTP_NOT_FOUND: Final = 404
_HTTP_CONFLICT: Final = 409
_HTTP_PRECONDITION: Final = 412
_HTTP_BAD_REQUEST: Final = 400
# Fields a PATCH sends; the rest of a Service is output only or unused by SSC.
_WRITABLE: Final = ("labels", "ingress", "invokerIamDisabled", "scaling", "template", "traffic")


@dataclass(frozen=True, slots=True, kw_only=True)
class CellRuntime:
    """Where app services run. Every name comes from the cell stack's outputs."""

    project: str
    region: str
    network: str  # projects/<p>/global/networks/ssc-cell
    subnetwork: str  # projects/<p>/regions/<r>/subnetworks/apps
    image_repository: str  # <region>-docker.pkg.dev/<p>/ssc-apps/apps
    invoker: str  # the gateway's service account email

    @property
    def parent(self) -> str:
        return f"projects/{self.project}/locations/{self.region}"

    def service_path(self, service: str) -> str:
        return f"{self.parent}/services/{service}"

    def identity(self, service: str) -> str:
        return f"{service}@{self.project}.iam.gserviceaccount.com"

    def image(self, digest: str) -> str:
        return f"{self.image_repository}@{digest}"


class _ConflictError(Exception):
    """Someone changed the service between our read and our write; read again."""


class _ApiError(RuntimeDriverError):
    def __init__(self, what: str, status: int, reason: str) -> None:
        super().__init__(f"{what}: HTTP {status} {reason}")
        self.status = status
        self.reason = reason


class CloudRunDriver(RuntimeDriver):
    def __init__(
        self,
        cell: CellRuntime,
        tokens: AccessTokens,
        *,
        client: httpx2.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_seconds: float = 1.0,
    ) -> None:
        self.cell = cell
        self._tokens = tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)
        self._sleep = sleep
        self._poll = poll_seconds

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── RuntimeDriver ────────────────────────────────────────────────────────

    async def apply(self, spec: ServiceSpec) -> str:
        _check_service(spec.service)
        _check_env(spec)
        for attempt in range(CONFLICT_TRIES):
            try:
                revision = await self._apply_once(spec)
            except _ConflictError:
                await self._sleep(min(2.0**attempt, 10.0))
                continue
            await self._set_invoker(spec.service)
            return revision
        raise RuntimeDriverError(f"{spec.service}: still changing after {CONFLICT_TRIES} tries")

    async def set_traffic(self, service: str, revision: str) -> None:
        _check_service(service)
        await self._update(
            service, lambda svc, revisions: _route_all(svc, revisions, service, revision)
        )
        await self.wait_settled(service)

    async def scale_to_zero(self, service: str) -> None:
        _check_service(service)

        def stop(svc: Json, revisions: Sequence[Json]) -> Json:
            body = _writable(svc)
            body["scaling"] = {"scalingMode": "MANUAL", "manualInstanceCount": 0}
            return body

        await self._update(service, stop)
        await self.wait_settled(service)

    async def observe(self, service: str) -> ServiceObservation | None:
        _check_service(service)
        svc = await self._get_service(service)
        if svc is None:
            return None
        revisions = await self._revisions(service)
        traffic = _traffic(svc)
        seen = [self._observe_revision(r, traffic) for r in revisions]
        template = _obj(svc.get("template"))
        pending = template.get("revision")
        if pending and pending not in {r.revision for r in seen} and _reconciling(svc):
            fingerprint, digest = self._fingerprint(template)
            seen.append(
                RevisionObservation(
                    revision=pending,
                    spec_fingerprint=fingerprint,
                    image_digest=digest,
                    ready=None,
                    failed=False,
                    traffic_percent=traffic.get(pending, 0),
                )
            )
        scaling = _obj(svc.get("scaling"))
        stopped = scaling.get("scalingMode") == "MANUAL" and not scaling.get("manualInstanceCount")
        return ServiceObservation(
            service=service,
            revisions=tuple(seen),
            min_instances=int(scaling.get("minInstanceCount") or 0),
            max_instances=int(scaling.get("maxInstanceCount") or 0),
            stopped=stopped,
        )

    # ── waiting ──────────────────────────────────────────────────────────────

    async def settled(self, service: str) -> bool:
        """The last change to the service is done: Cloud Run has acted on its latest generation."""
        svc = await self._get_service(service)
        return svc is None or not _reconciling(svc)

    async def wait_settled(self, service: str, within: float = SETTLE_TIMEOUT_SECONDS) -> None:
        waited = 0.0
        while not await self.settled(service):
            if waited >= within:
                raise RuntimeDriverError(f"{service}: not settled after {within:.0f} s")
            await self._sleep(self._poll)
            waited += self._poll

    # ── apply ────────────────────────────────────────────────────────────────

    async def _apply_once(self, spec: ServiceSpec) -> str:
        svc = await self._get_service(spec.service)
        if svc is None:
            return await self._create(spec)
        revisions = await self._revisions(spec.service)
        traffic = _traffic(svc)
        matches = [r for r in revisions if self._fingerprint(r)[0] == spec.spec_fingerprint]
        template = _obj(svc.get("template"))
        body = _writable(svc)
        body["labels"] = dict(spec.labels)
        body["ingress"] = INGRESS
        body["invokerIamDisabled"] = False
        body["scaling"] = _automatic(spec)
        if matches:
            revision = max(matches, key=lambda r: traffic.get(_short(r["name"]), 0))
            name = _short(revision["name"])
        elif self._fingerprint(template)[0] == spec.spec_fingerprint and template.get("revision"):
            name = template["revision"]  # asked for already, not made yet
            made = [r for r in revisions if _short(r["name"]) == name]
            if made:
                # Cloud Run runs an image index's platform manifest under that manifest's digest.
                ran = self._fingerprint(made[0])[1]
                raise RuntimeDriverError(
                    f"{spec.service}: Cloud Run ran {ran} for {spec.image_digest}; "
                    "an app image must be a single-platform manifest"
                )
        elif _reconciling(svc):
            # Pinning traffic needs the serving revision to exist: let the last change finish.
            await self.wait_settled(spec.service)
            raise _ConflictError
        else:
            name = _revision_name(spec, int(svc.get("generation") or 0) + 1)
            body["template"] = self._template(spec, name)
            body["traffic"] = _pinned_traffic(svc, revisions)
        if any(_bare(body.get(k)) != _bare(svc.get(k)) for k in _WRITABLE):
            await self._patch(spec.service, body)
        return name

    async def _create(self, spec: ServiceSpec) -> str:
        await self._ensure_identity(spec.service)
        name = _revision_name(spec, 1)
        body = {
            "labels": dict(spec.labels),
            "ingress": INGRESS,
            "invokerIamDisabled": False,
            "scaling": _automatic(spec),
            "template": self._template(spec, name),
        }
        url = f"{RUN_API}/{self.cell.parent}/services"
        for attempt in range(IDENTITY_TRIES):
            try:
                await self._call("POST", url, json=body, params={"serviceId": spec.service})
            except _ApiError as exc:
                if exc.status == _HTTP_CONFLICT:
                    raise _ConflictError from None  # created by someone else meanwhile
                # A new service account takes a few seconds to be usable.
                if exc.status == _HTTP_BAD_REQUEST and "service account" in exc.reason.lower():
                    await self._sleep(min(2.0**attempt, 10.0))
                    continue
                raise
            return name
        raise RuntimeDriverError(f"{spec.service}: its service account is still not usable")

    def _template(self, spec: ServiceSpec, revision: str) -> Json:
        size = spec.resources
        return {
            "revision": revision,
            "serviceAccount": self.cell.identity(spec.service),
            "executionEnvironment": "EXECUTION_ENVIRONMENT_GEN2",
            "timeout": f"{spec.timeout_seconds}s",
            "maxInstanceRequestConcurrency": spec.concurrency,
            "vpcAccess": {
                "egress": "ALL_TRAFFIC",
                "networkInterfaces": [
                    {"network": self.cell.network, "subnetwork": self.cell.subnetwork}
                ],
            },
            "containers": [
                {
                    "image": self.cell.image(spec.image_digest),
                    "ports": [{"containerPort": spec.port}],
                    "env": [
                        {"name": k, "value": v}
                        for k, v in sorted(spec.env.items())
                        if k not in RESERVED_ENV
                    ],
                    "resources": {
                        "limits": {"cpu": str(size.vcpu), "memory": f"{size.memory_mib}Mi"},
                        "cpuIdle": spec.billing == "request",
                        "startupCpuBoost": True,
                    },
                    "startupProbe": {
                        "httpGet": {"path": spec.health_path, "port": spec.port},
                        "periodSeconds": STARTUP_PERIOD_SECONDS,
                        "timeoutSeconds": STARTUP_TIMEOUT_SECONDS,
                        "failureThreshold": STARTUP_FAILURES,
                    },
                }
            ],
        }

    async def _ensure_identity(self, service: str) -> None:
        """The environment's own service account, with no roles. Granting it anything is a
        decision of the ticket that needs it (secrets, the app database)."""
        try:
            await self._call(
                "POST",
                f"{IAM_API}/projects/{self.cell.project}/serviceAccounts",
                json={
                    "accountId": service,
                    "serviceAccount": {
                        "displayName": f"SSC app environment {service}",
                        "description": "Runs one SSC app environment. Holds no roles.",
                    },
                },
            )
        except _ApiError as exc:
            if exc.status != _HTTP_CONFLICT:
                raise

    async def _set_invoker(self, service: str) -> None:
        """Only the gateway may invoke an app. ``setIamPolicy`` replaces, never merges."""
        policy = {
            "bindings": [{"role": INVOKER_ROLE, "members": [f"serviceAccount:{self.cell.invoker}"]}]
        }
        url = f"{RUN_API}/{self.cell.service_path(service)}:setIamPolicy"
        await self._call("POST", url, json={"policy": policy})

    # ── reads and writes ─────────────────────────────────────────────────────

    async def _update(self, service: str, change: Callable[[Json, Sequence[Json]], Json]) -> None:
        for attempt in range(CONFLICT_TRIES):
            svc = await self._get_service(service)
            if svc is None:
                raise ServiceNotFoundError(service)
            body = change(svc, await self._revisions(service))
            try:
                await self._patch(service, body)
            except _ConflictError:
                await self._sleep(min(2.0**attempt, 10.0))
                continue
            return
        raise RuntimeDriverError(f"{service}: still changing after {CONFLICT_TRIES} tries")

    async def _patch(self, service: str, body: Json) -> None:
        try:
            await self._call("PATCH", f"{RUN_API}/{self.cell.service_path(service)}", json=body)
        except _ApiError as exc:
            if exc.status in {_HTTP_CONFLICT, _HTTP_PRECONDITION}:
                raise _ConflictError from None
            raise

    async def _get_service(self, service: str) -> Json | None:
        try:
            return await self._call("GET", f"{RUN_API}/{self.cell.service_path(service)}")
        except _ApiError as exc:
            if exc.status == _HTTP_NOT_FOUND:
                return None
            raise

    async def _revisions(self, service: str) -> list[Json]:
        url = f"{RUN_API}/{self.cell.service_path(service)}/revisions"
        found: list[Json] = []
        token: str | None = None
        while True:
            params = {"pageSize": "100"} | ({"pageToken": token} if token else {})
            try:
                page = await self._call("GET", url, params=params)
            except _ApiError as exc:
                if exc.status == _HTTP_NOT_FOUND:
                    return []
                raise
            found.extend(_objs(page.get("revisions")))
            token = page.get("nextPageToken")
            if not token:
                return sorted(found, key=lambda r: (r.get("createTime") or "", r["name"]))

    async def _call(
        self,
        method: str,
        url: str,
        *,
        json: Json | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Json:
        what = f"{method} {url.removeprefix(RUN_API).removeprefix(IAM_API)}"
        headers = {"Authorization": f"Bearer {await self._tokens()}"}
        try:
            response = await self._client.request(
                method, url, json=json, params=dict(params or {}), headers=headers
            )
        except httpx2.HTTPError as exc:
            raise RuntimeDriverError(f"{what}: {type(exc).__name__}") from None
        if response.status_code >= _HTTP_BAD_REQUEST:
            raise _ApiError(what, response.status_code, _reason(response))
        if not response.content:
            return {}
        return cast(Json, response.json())

    # ── observing ────────────────────────────────────────────────────────────

    def _observe_revision(self, revision: Json, traffic: Mapping[str, int]) -> RevisionObservation:
        name = _short(revision["name"])
        fingerprint, digest = self._fingerprint(revision)
        ready, failed = _readiness(revision)
        return RevisionObservation(
            revision=name,
            spec_fingerprint=fingerprint,
            image_digest=digest,
            ready=ready,
            failed=failed,
            traffic_percent=traffic.get(name, 0),
        )

    def _fingerprint(self, revision: Json) -> tuple[str, str]:
        """(fingerprint, image digest) of a revision or template, from what it actually runs.
        An image outside the cell's repository keeps its whole reference as the digest, so it
        never matches a spec."""
        containers = _objs(revision.get("containers")) or [{}]
        container = containers[0] if len(containers) == 1 else {"multiple": len(containers)}
        image = str(container.get("image") or "")
        prefix = f"{self.cell.image_repository}@"
        digest = image.removeprefix(prefix) if image.startswith(prefix) else image
        ports = _objs(container.get("ports")) or [{}]
        probe = _obj(_obj(container.get("startupProbe")).get("httpGet"))
        resources = _obj(container.get("resources"))
        limits = _obj(resources.get("limits"))
        billing: Billing = "request" if resources.get("cpuIdle") else "instance"
        env: dict[str, str] = {}
        for var in _objs(container.get("env")):
            source = var.get("valueSource")
            env[var["name"]] = str(var.get("value", "")) if source is None else f"source:{source}"
        port = int(ports[0].get("containerPort") or 8080)
        env["PORT"] = str(port)
        fingerprint = fingerprint_of(
            image_digest=digest,
            port=port,
            health_path=str(probe.get("path") or ""),
            vcpu=_cpu(str(limits.get("cpu") or "1")),
            memory_mib=_memory_mib(str(limits.get("memory") or "512Mi")),
            env=env,
            billing=billing,
            timeout_seconds=_seconds(revision.get("timeout")),
            concurrency=int(revision.get("maxInstanceRequestConcurrency") or DEFAULT_CONCURRENCY),
        )
        return fingerprint, digest


# ── pure helpers ─────────────────────────────────────────────────────────────


def _check_env(spec: ServiceSpec) -> None:
    if spec.env.get("PORT") != str(spec.port):
        raise RuntimeDriverError(f"{spec.service}: PORT must be the container port {spec.port}")
    if set_by_cloud_run := sorted(RESERVED_ENV.intersection(spec.env) - {"PORT"}):
        raise RuntimeDriverError(f"{spec.service}: Cloud Run sets {', '.join(set_by_cloud_run)}")


def _obj(value: object) -> Json:
    return cast(Json, value) if isinstance(value, dict) else {}


def _objs(value: object) -> list[Json]:
    items = cast("list[object]", value) if isinstance(value, list) else []
    return [cast(Json, v) for v in items if isinstance(v, dict)]


def _check_service(service: str) -> None:
    """The agent's IAM cannot limit a create by name, so its code does (decision 022)."""
    if SERVICE_NAME.fullmatch(service) is None:
        raise RuntimeDriverError(f"not an SSC app service name: {service!r}")


def _revision_name(spec: ServiceSpec, generation: int) -> str:
    return f"{spec.service}-{generation:05d}-{spec.spec_fingerprint.removeprefix('sha256:')[:6]}"


def _short(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _reconciling(svc: Json) -> bool:
    observed = int(svc.get("observedGeneration") or 0)
    return bool(svc.get("reconciling")) or observed < int(svc.get("generation") or 0)


def _automatic(spec: ServiceSpec) -> Json:
    return {
        "scalingMode": "AUTOMATIC",
        "minInstanceCount": spec.min_instances,
        "maxInstanceCount": spec.max_instances,
    }


def _bare(value: Any) -> Any:
    """JSON without its empty parts: proto3 leaves out zeros, so ``0`` and absent compare equal."""
    if isinstance(value, dict):
        pairs = ((k, _bare(v)) for k, v in cast("dict[str, Any]", value).items())
        return {k: v for k, v in pairs if v not in (None, 0, False, "", [], {})}
    if isinstance(value, list):
        return [_bare(v) for v in cast("list[Any]", value)]
    return value


def _writable(svc: Json) -> Json:
    return {k: svc[k] for k in _WRITABLE if k in svc} | {"etag": svc.get("etag")}


def _latest(svc: Json) -> str | None:
    for key in ("latestReadyRevision", "latestCreatedRevision"):
        if svc.get(key):
            return _short(svc[key])
    revision = _obj(svc.get("template")).get("revision")
    return revision if isinstance(revision, str) else None


def _traffic(svc: Json) -> dict[str, int]:
    """Revision name to percent, as routed (``trafficStatuses``), or as asked for while Cloud
    Run has not reported routing yet. ``LATEST`` resolves to the revision it means."""
    entries = _objs(svc.get("trafficStatuses")) or _objs(svc.get("traffic"))
    traffic: dict[str, int] = {}
    for entry in entries:
        name = entry.get("revision") or (
            _latest(svc) if entry.get("type") == LATEST_TRAFFIC else None
        )
        percent = int(entry.get("percent") or 0)
        if name and percent:
            traffic[name] = traffic.get(name, 0) + percent
    return traffic


def _pinned_traffic(svc: Json, revisions: Sequence[Json]) -> list[Json]:
    """Today's routing with every target named, so a new template takes no traffic."""
    traffic = _traffic(svc)
    if not traffic:
        names = [_short(r["name"]) for r in revisions] or [_latest(svc)]
        traffic = {names[0]: 100} if names[0] else {}
    return [
        {"type": REVISION_TRAFFIC, "revision": name, "percent": percent}
        for name, percent in sorted(traffic.items())
    ]


def _route_all(svc: Json, revisions: Sequence[Json], service: str, revision: str) -> Json:
    if revision not in {_short(r["name"]) for r in revisions}:
        raise RevisionNotFoundError(f"{service}: no revision {revision}")
    body = _writable(svc)
    body["traffic"] = [{"type": REVISION_TRAFFIC, "revision": revision, "percent": 100}]
    return body


def _readiness(revision: Json) -> tuple[bool | None, bool]:
    """(ready, failed) from the revision's ``Ready`` condition; None while it is starting."""
    for condition in _objs(revision.get("conditions")):
        if condition.get("type") != "Ready":
            continue
        match condition.get("state"):
            case "CONDITION_SUCCEEDED":
                return True, False
            case "CONDITION_FAILED":
                return False, True
            case _:
                return None, False
    return None, False


def _cpu(value: str) -> float:
    return float(value[:-1]) / 1000 if value.endswith("m") else float(value)


def _seconds(value: object) -> int:
    """A Duration such as ``"300s"``; Cloud Run's default when absent."""
    if not isinstance(value, str) or not value.endswith("s"):
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return int(float(value.removesuffix("s")))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def _memory_mib(value: str) -> int:
    units = {"Mi": 1, "Gi": 1024, "M": 1, "G": 1000}
    for unit, factor in units.items():
        if value.endswith(unit):
            return int(float(value.removesuffix(unit)) * factor)
    return int(value) // (1024 * 1024)


def _reason(response: httpx2.Response) -> str:
    try:
        error = _obj(_obj(response.json()).get("error"))
    except ValueError:
        return response.reason_phrase
    return f"{error.get('status', '')} {error.get('message', '')}".strip()


__all__ = ["AccessTokens", "CellRuntime", "CloudRunDriver"]
