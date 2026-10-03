"""``RuntimeDriver`` contract. The fake passes it now; the Cloud Run driver (SSC-013) must too.

Subclass ``RuntimeDriverContract`` as a ``Test*`` class and provide three fixtures:

- ``runtime_driver``: the driver under test;
- ``images``: two image digests the runtime can pull and that pass their health check (for a
  cloud runtime, the probe image and a rebuild of it);
- ``settle``: an async callable that waits until the runtime has finished starting what was
  last applied (a cloud runtime polls; the fake returns at once).

Every test works on a fresh service name, so a real runtime can run the suite repeatedly.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

import pytest

from ssc_contracts.ids import new_id
from ssc_control.runtime.driver import (
    RevisionNotFoundError,
    RevisionObservation,
    RuntimeDriver,
    ServiceNotFoundError,
    ServiceObservation,
    ServiceSpec,
    service_name,
)

type Settle = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Images:
    first: str
    second: str


def new_spec(image_digest: str, *, service: str | None = None) -> ServiceSpec:
    env = new_id("env")
    return ServiceSpec(
        service=service or service_name(env),
        image_digest=image_digest,
        port=8080,
        health_path="/healthz",
        resource_class="small",
        env={"PORT": "8080", "HOME": "/tmp"},  # noqa: S108  (the platform's HOME)
        billing="request",
        timeout_seconds=300,
        concurrency=80,
        min_instances=0,
        max_instances=2,
        labels={"ssc-env": env, "ssc-contract": "runtime-driver"},
    )


async def observed(driver: RuntimeDriver, service: str) -> ServiceObservation:
    seen = await driver.observe(service)
    assert seen is not None, f"{service} does not exist"
    return seen


def revision(seen: ServiceObservation, name: str) -> RevisionObservation:
    (match,) = [r for r in seen.revisions if r.revision == name]
    return match


def traffic(seen: ServiceObservation) -> dict[str, int]:
    return {r.revision: r.traffic_percent for r in seen.revisions if r.traffic_percent}


class RuntimeDriverContract:
    """Behaviour every ``RuntimeDriver`` shows. Test methods take the fixtures by name."""

    async def test_missing_service_is_none(self, runtime_driver: RuntimeDriver) -> None:
        assert await runtime_driver.observe(service_name(new_id("env"))) is None

    async def test_first_apply_creates_a_serving_revision(
        self, runtime_driver: RuntimeDriver, images: Images, settle: Settle
    ) -> None:
        spec = new_spec(images.first)
        rev = await runtime_driver.apply(spec)
        await settle()
        seen = await observed(runtime_driver, spec.service)
        assert seen.service == spec.service
        assert (seen.min_instances, seen.max_instances, seen.stopped) == (0, 2, False)
        first = revision(seen, rev)
        assert first.spec_fingerprint == spec.spec_fingerprint
        assert first.image_digest == spec.image_digest
        assert (first.ready, first.failed) == (True, False)
        assert traffic(seen) == {rev: 100}

    async def test_apply_is_idempotent_on_the_fingerprint(
        self, runtime_driver: RuntimeDriver, images: Images, settle: Settle
    ) -> None:
        spec = new_spec(images.first)
        rev = await runtime_driver.apply(spec)
        assert await runtime_driver.apply(spec) == rev
        rescaled = replace(spec, min_instances=1, max_instances=1, labels={"ssc-other": "x"})
        assert rescaled.spec_fingerprint == spec.spec_fingerprint
        assert await runtime_driver.apply(rescaled) == rev
        await settle()
        seen = await observed(runtime_driver, spec.service)
        assert [r.revision for r in seen.revisions] == [rev]
        assert (seen.min_instances, seen.max_instances) == (1, 1)

    async def test_new_revision_gets_no_traffic(
        self, runtime_driver: RuntimeDriver, images: Images, settle: Settle
    ) -> None:
        spec = new_spec(images.first)
        old = await runtime_driver.apply(spec)
        changed = replace(spec, image_digest=images.second)
        new = await runtime_driver.apply(changed)
        assert new != old
        await settle()
        seen = await observed(runtime_driver, spec.service)
        assert revision(seen, new).spec_fingerprint == changed.spec_fingerprint
        assert revision(seen, new).image_digest == images.second
        assert traffic(seen) == {old: 100}

    async def test_billing_timeout_and_concurrency_define_the_revision(
        self, runtime_driver: RuntimeDriver, images: Images, settle: Settle
    ) -> None:
        spec = new_spec(images.first)
        old = await runtime_driver.apply(spec)
        session = replace(
            spec, billing="instance", timeout_seconds=3600, concurrency=1000, max_instances=1
        )
        assert session.spec_fingerprint != spec.spec_fingerprint
        new = await runtime_driver.apply(session)
        assert new != old
        await settle()
        seen = await observed(runtime_driver, spec.service)
        assert revision(seen, new).spec_fingerprint == session.spec_fingerprint
        assert revision(seen, old).spec_fingerprint == spec.spec_fingerprint
        assert await runtime_driver.apply(replace(session, timeout_seconds=300)) not in {old, new}
        assert await runtime_driver.apply(replace(session, concurrency=80)) not in {old, new}

    async def test_set_traffic_moves_everything(
        self, runtime_driver: RuntimeDriver, images: Images, settle: Settle
    ) -> None:
        spec = new_spec(images.first)
        old = await runtime_driver.apply(spec)
        new = await runtime_driver.apply(replace(spec, image_digest=images.second))
        await settle()
        await runtime_driver.set_traffic(spec.service, new)
        assert traffic(await observed(runtime_driver, spec.service)) == {new: 100}
        await runtime_driver.set_traffic(spec.service, old)
        assert traffic(await observed(runtime_driver, spec.service)) == {old: 100}
        assert await runtime_driver.apply(spec) == old  # an old fingerprint finds its revision

    async def test_set_traffic_refuses_unknown_targets(
        self, runtime_driver: RuntimeDriver, images: Images
    ) -> None:
        spec = new_spec(images.first)
        rev = await runtime_driver.apply(spec)
        with pytest.raises(RevisionNotFoundError):
            await runtime_driver.set_traffic(spec.service, spec.service + "-99999")
        with pytest.raises(ServiceNotFoundError):
            await runtime_driver.set_traffic(service_name(new_id("env")), rev)
        assert traffic(await observed(runtime_driver, spec.service)) == {rev: 100}

    async def test_scale_to_zero_stops_until_the_next_apply(
        self, runtime_driver: RuntimeDriver, images: Images, settle: Settle
    ) -> None:
        spec = new_spec(images.first)
        rev = await runtime_driver.apply(spec)
        await settle()
        await runtime_driver.scale_to_zero(spec.service)
        stopped = await observed(runtime_driver, spec.service)
        assert stopped.stopped
        assert [r.revision for r in stopped.revisions] == [rev]  # revisions are kept
        assert await runtime_driver.apply(spec) == rev
        assert not (await observed(runtime_driver, spec.service)).stopped
        with pytest.raises(ServiceNotFoundError):
            await runtime_driver.scale_to_zero(service_name(new_id("env")))
