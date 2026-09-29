"""An in-memory ``RuntimeDriver`` for tests and local runs, with injectable failures and drift.

It behaves like the runtime the contract describes: revisions are immutable, a new revision gets
no traffic unless it is the service's first, ``apply`` is idempotent on the fingerprint, and a
revision's fingerprint is recomputed from its actual fields on every ``observe``.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Final, Literal, get_args

from ssc_contracts.manifest import ResourceClassName
from ssc_control.runtime.driver import (
    RevisionNotFoundError,
    RevisionObservation,
    RuntimeDriver,
    ServiceNotFoundError,
    ServiceObservation,
    ServiceSpec,
    revision_fingerprint,
)

Method = Literal["apply", "set_traffic", "scale_to_zero", "observe"]
METHODS: Final[tuple[str, ...]] = get_args(Method)


@dataclass(frozen=True, slots=True)
class _Revision:
    name: str
    image_digest: str
    port: int
    health_path: str
    resource_class: ResourceClassName
    env: tuple[tuple[str, str], ...]

    @property
    def fingerprint(self) -> str:
        return revision_fingerprint(
            image_digest=self.image_digest,
            port=self.port,
            health_path=self.health_path,
            resource_class=self.resource_class,
            env=dict(self.env),
        )


@dataclass(slots=True)
class _Service:
    name: str
    min_instances: int
    max_instances: int
    labels: dict[str, str]
    stopped: bool = False
    revisions: list[_Revision] = field(default_factory=list[_Revision])
    traffic: dict[str, int] = field(default_factory=dict[str, int])
    created: int = 0

    def add(self, template: _Revision) -> _Revision:
        self.created += 1
        revision = replace(template, name=f"{self.name}-{self.created:05d}")
        self.revisions.append(revision)
        self.traffic[revision.name] = 100 if self.created == 1 else 0
        return revision

    def route_all(self, revision: str) -> None:
        self.traffic = {r.name: (100 if r.name == revision else 0) for r in self.revisions}


class FakeRuntimeDriver(RuntimeDriver):
    def __init__(self, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.services: dict[str, _Service] = {}
        self.calls: list[tuple[Method, str]] = []
        self._sleep = sleep
        self._failures: dict[Method, list[Exception]] = {}
        self._delays: dict[Method, float] = {}
        self._unhealthy: set[str] = set()
        self._starting: set[str] = set()

    # ── injection ────────────────────────────────────────────────────────────

    def fail_next(self, method: Method, exc: Exception) -> None:
        """The next call to ``method`` raises ``exc`` (after being logged) and changes nothing."""
        self._failures.setdefault(method, []).append(exc)

    def slow(self, method: Method, seconds: float) -> None:
        """Every call to ``method`` waits ``seconds`` first; 0 clears it."""
        self._delays[method] = seconds

    def unhealthy(self, image_digest: str) -> None:
        """Revisions of this image fail their health check."""
        self._starting.discard(image_digest)
        self._unhealthy.add(image_digest)

    def starting(self, image_digest: str) -> None:
        """Revisions of this image are still starting (``ready`` is None)."""
        self._unhealthy.discard(image_digest)
        self._starting.add(image_digest)

    def healthy(self, image_digest: str) -> None:
        self._unhealthy.discard(image_digest)
        self._starting.discard(image_digest)

    def drift(  # noqa: PLR0913  (keyword-only)
        self,
        service: str,
        *,
        image_digest: str | None = None,
        port: int | None = None,
        health_path: str | None = None,
        resource_class: ResourceClassName | None = None,
        env: Mapping[str, str] | None = None,
        min_instances: int | None = None,
        max_instances: int | None = None,
        stopped: bool | None = None,
    ) -> None:
        """Change the service behind the control plane's back. A revision field acts like a
        manual deploy: a new revision copied from the serving one, given all traffic. The other
        three change the service's own settings."""
        svc = self._service(service)
        if any(v is not None for v in (image_digest, port, health_path, resource_class, env)):
            serving = max(svc.revisions, key=lambda r: svc.traffic.get(r.name, 0))
            revision = svc.add(
                replace(
                    serving,
                    image_digest=image_digest or serving.image_digest,
                    port=port or serving.port,
                    health_path=health_path or serving.health_path,
                    resource_class=resource_class or serving.resource_class,
                    env=serving.env if env is None else tuple(sorted(env.items())),
                )
            )
            svc.route_all(revision.name)
        if min_instances is not None:
            svc.min_instances = min_instances
        if max_instances is not None:
            svc.max_instances = max_instances
        if stopped is not None:
            svc.stopped = stopped

    def delete_service(self, service: str) -> None:
        self.services.pop(service, None)

    def delete_revision(self, service: str, revision: str) -> None:
        svc = self._service(service)
        svc.revisions = [r for r in svc.revisions if r.name != revision]
        svc.traffic.pop(revision, None)

    def reset_calls(self) -> None:
        self.calls.clear()

    # ── RuntimeDriver ────────────────────────────────────────────────────────

    async def apply(self, spec: ServiceSpec) -> str:
        await self._enter("apply", spec.service)
        svc = self.services.get(spec.service)
        if svc is None:
            svc = _Service(spec.service, spec.min_instances, spec.max_instances, dict(spec.labels))
            self.services[spec.service] = svc
        existing = [r for r in svc.revisions if r.fingerprint == spec.spec_fingerprint]
        if existing:
            revision = max(existing, key=lambda r: svc.traffic.get(r.name, 0))
        else:
            revision = svc.add(
                _Revision(
                    name="",
                    image_digest=spec.image_digest,
                    port=spec.port,
                    health_path=spec.health_path,
                    resource_class=spec.resource_class,
                    env=tuple(sorted(spec.env.items())),
                )
            )
        svc.min_instances, svc.max_instances = spec.min_instances, spec.max_instances
        svc.labels = dict(spec.labels)
        svc.stopped = False
        return revision.name

    async def set_traffic(self, service: str, revision: str) -> None:
        await self._enter("set_traffic", service)
        svc = self._service(service)
        if revision not in {r.name for r in svc.revisions}:
            raise RevisionNotFoundError(f"{service}: no revision {revision}")
        svc.route_all(revision)

    async def scale_to_zero(self, service: str) -> None:
        await self._enter("scale_to_zero", service)
        self._service(service).stopped = True

    async def observe(self, service: str) -> ServiceObservation | None:
        await self._enter("observe", service)
        svc = self.services.get(service)
        if svc is None:
            return None
        return ServiceObservation(
            service=svc.name,
            revisions=tuple(self._observe_revision(svc, r) for r in svc.revisions),
            min_instances=svc.min_instances,
            max_instances=svc.max_instances,
            stopped=svc.stopped,
        )

    # ── internals ────────────────────────────────────────────────────────────

    def _observe_revision(self, svc: _Service, revision: _Revision) -> RevisionObservation:
        digest = revision.image_digest
        ready = None if digest in self._starting else digest not in self._unhealthy
        return RevisionObservation(
            revision=revision.name,
            spec_fingerprint=revision.fingerprint,
            image_digest=digest,
            ready=ready,
            failed=digest in self._unhealthy,
            traffic_percent=svc.traffic.get(revision.name, 0),
        )

    def _service(self, service: str) -> _Service:
        try:
            return self.services[service]
        except KeyError:
            raise ServiceNotFoundError(service) from None

    async def _enter(self, method: Method, service: str) -> None:
        self.calls.append((method, service))
        delay = self._delays.get(method, 0.0)
        if delay:
            await self._sleep(delay)
        queued = self._failures.get(method)
        if queued:
            raise queued.pop(0)


def changed(calls: list[tuple[Method, str]], service: str) -> list[Method]:
    """The mutating calls made for one service, in order (``observe`` left out)."""
    return [m for m, s in calls if s == service and m != "observe"]


__all__ = ["METHODS", "FakeRuntimeDriver", "Method", "changed"]
