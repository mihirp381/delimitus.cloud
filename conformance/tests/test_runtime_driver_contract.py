"""``FakeRuntimeDriver`` passes the ``RuntimeDriver`` contract, and its injection points work."""

import asyncio

import pytest

from ssc_conformance.contracts.runtime_driver import (
    Images,
    RuntimeDriverContract,
    Settle,
    new_spec,
    observed,
    traffic,
)
from ssc_control.runtime.driver import RuntimeDriverError
from ssc_control.runtime.fake import FakeRuntimeDriver, changed

FIRST = "sha256:" + "1" * 64
SECOND = "sha256:" + "2" * 64


@pytest.fixture
def runtime_driver() -> FakeRuntimeDriver:
    return FakeRuntimeDriver()


@pytest.fixture
def images() -> Images:
    return Images(FIRST, SECOND)


@pytest.fixture
def settle() -> Settle:
    async def nothing() -> None:
        return None

    return nothing


class TestFakeRuntimeDriver(RuntimeDriverContract):
    pass


async def test_fail_next_raises_once_and_changes_nothing(
    runtime_driver: FakeRuntimeDriver,
) -> None:
    spec = new_spec(FIRST)
    runtime_driver.fail_next("apply", RuntimeDriverError("quota"))
    with pytest.raises(RuntimeDriverError, match="quota"):
        await runtime_driver.apply(spec)
    assert await runtime_driver.observe(spec.service) is None
    rev = await runtime_driver.apply(spec)
    runtime_driver.fail_next("set_traffic", RuntimeDriverError("busy"))
    with pytest.raises(RuntimeDriverError):
        await runtime_driver.set_traffic(spec.service, rev)
    runtime_driver.fail_next("observe", TimeoutError())
    with pytest.raises(TimeoutError):
        await runtime_driver.observe(spec.service)
    assert changed(runtime_driver.calls, spec.service) == ["apply", "apply", "set_traffic"]


async def test_health_flags_per_image(runtime_driver: FakeRuntimeDriver) -> None:
    spec = new_spec(FIRST)
    rev = await runtime_driver.apply(spec)
    runtime_driver.unhealthy(FIRST)
    (seen,) = (await observed(runtime_driver, spec.service)).revisions
    assert (seen.revision, seen.ready, seen.failed) == (rev, False, True)
    runtime_driver.starting(FIRST)
    (seen,) = (await observed(runtime_driver, spec.service)).revisions
    assert (seen.ready, seen.failed) == (None, False)
    runtime_driver.healthy(FIRST)
    (seen,) = (await observed(runtime_driver, spec.service)).revisions
    assert (seen.ready, seen.failed) == (True, False)


async def test_drift_shows_in_the_observed_fingerprint(runtime_driver: FakeRuntimeDriver) -> None:
    spec = new_spec(FIRST)
    rev = await runtime_driver.apply(spec)
    runtime_driver.drift(spec.service, port=9000, max_instances=8)
    seen = await observed(runtime_driver, spec.service)
    (serving,) = [r for r in seen.revisions if r.traffic_percent == 100]
    assert serving.revision != rev
    assert serving.spec_fingerprint != spec.spec_fingerprint
    assert serving.image_digest == FIRST
    assert seen.max_instances == 8
    assert traffic(seen) == {serving.revision: 100}


async def test_slow_calls_wait_first() -> None:
    waited: list[float] = []

    async def record(seconds: float) -> None:
        waited.append(seconds)
        await asyncio.sleep(0)

    driver = FakeRuntimeDriver(sleep=record)
    driver.slow("observe", 2.5)
    await driver.observe(new_spec(FIRST).service)
    driver.slow("observe", 0)
    await driver.observe(new_spec(FIRST).service)
    assert waited == [2.5]


async def test_call_log_keeps_order(runtime_driver: FakeRuntimeDriver) -> None:
    spec = new_spec(FIRST)
    rev = await runtime_driver.apply(spec)
    await runtime_driver.observe(spec.service)
    await runtime_driver.set_traffic(spec.service, rev)
    await runtime_driver.scale_to_zero(spec.service)
    assert runtime_driver.calls == [
        ("apply", spec.service),
        ("observe", spec.service),
        ("set_traffic", spec.service),
        ("scale_to_zero", spec.service),
    ]
