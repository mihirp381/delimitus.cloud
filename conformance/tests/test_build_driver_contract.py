"""``FakeBuildDriver`` passes the ``BuildDriver`` contract, and its injection points work."""

import hashlib

import pytest

from ssc_conformance.contracts.build_driver import BuildDriverContract, NewRequest
from ssc_contracts.ids import new_id
from ssc_contracts.manifest import Manifest
from ssc_control.deploy.build_driver import (
    BUILD_REASONS,
    BuildDriverError,
    BuildRequest,
    Failed,
    FakeBuildDriver,
    Running,
    Succeeded,
    fake_image_digest,
)
from ssc_shared.canonical import canonical_bytes

BROKEN = "sha256:" + "b" * 64


def request(source_digest: str | None = None, **overrides: object) -> BuildRequest:
    build_id = new_id("bld")
    fields: dict[str, object] = {
        "build_id": build_id,
        "org_id": new_id("org"),
        "app_id": new_id("app"),
        "env_name": "preview",
        "bundle_key": "bundles/x",
        "source_digest": source_digest or "sha256:" + hashlib.sha256(build_id.encode()).hexdigest(),
        "manifest": Manifest.model_validate({"schema": "ssc/v1"}),
        "public_env": {},
    }
    return BuildRequest(**{**fields, **overrides})


@pytest.fixture
def build_driver() -> FakeBuildDriver:
    driver = FakeBuildDriver(polls=2)
    driver.fail(BROKEN, "BUILD_EXITED_NONZERO", "npm run build exited 1")
    return driver


@pytest.fixture
def buildable() -> NewRequest:
    return request


@pytest.fixture
def broken() -> NewRequest:
    return lambda: request(BROKEN)


class TestFakeBuildDriver(BuildDriverContract):
    pass


def test_the_image_digest_is_the_documented_formula() -> None:
    source = "sha256:" + "5" * 64
    env = {"VITE_API": "https://api.example.com", "VITE_A": "1"}
    body = b"image:" + source.encode() + b"prod" + canonical_bytes(dict(sorted(env.items())))
    assert fake_image_digest(source, "prod", env) == "sha256:" + hashlib.sha256(body).hexdigest()
    # Each environment builds its own image from the same source (C08).
    assert fake_image_digest(source, "prod", env) != fake_image_digest(source, "preview", env)
    assert fake_image_digest(source, "prod", env) != fake_image_digest(source, "prod", {})


async def test_a_build_runs_for_the_set_polls_then_reports_its_image() -> None:
    driver = FakeBuildDriver(polls=2)
    req = request(public_env={"VITE_A": "1"})
    ref = await driver.start(req)
    assert [await driver.poll(ref), await driver.poll(ref)] == [Running(), Running()]
    result = await driver.poll(ref)
    assert result == Succeeded(
        image_digest=fake_image_digest(req.source_digest, "preview", {"VITE_A": "1"}),
        scan_refs=(f"fake-scan:{req.build_id}",),
    )
    assert driver.requests == [req]


async def test_fail_uses_the_delimitus_build_reason_codes() -> None:
    assert {"BUILD_EXITED_NONZERO", "BUILD_DEPENDENCY_UNRESOLVED"} <= BUILD_REASONS
    driver = FakeBuildDriver()
    driver.fail(BROKEN, "BUILD_DEPENDENCY_UNRESOLVED", "no such package")
    ref = await driver.start(request(BROKEN))
    assert await driver.poll(ref) == Failed(
        code="BUILD_DEPENDENCY_UNRESOLVED", message="no such package"
    )
    with pytest.raises(ValueError, match="code"):
        Failed(code="not a code", message="x")


async def test_fail_next_raises_once_and_starts_nothing() -> None:
    driver = FakeBuildDriver()
    req = request()
    driver.fail_next("start", BuildDriverError("quota"))
    with pytest.raises(BuildDriverError, match="quota"):
        await driver.start(req)
    assert driver.requests == []
    ref = await driver.start(req)
    driver.fail_next("poll", TimeoutError())
    with pytest.raises(TimeoutError):
        await driver.poll(ref)
    assert isinstance(await driver.poll(ref), Succeeded)


def test_a_request_is_frozen_and_checks_its_digest() -> None:
    req = request(public_env={"VITE_A": "1"})
    with pytest.raises(TypeError):
        req.public_env["VITE_A"] = "2"
    with pytest.raises(ValueError, match="digest"):
        request("sha256:short")
