"""``CellAgentBuildDriver`` passes the ``BuildDriver`` contract as the control plane reaches it:
control → agent app → ``CloudBuildDriver`` → the Cloud Build emulator, which fetches the bundle
through the control plane's signed URL."""

import hashlib
import secrets
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest
from fastapi import FastAPI

from ssc_agent.app import create_app
from ssc_agent.cloud_build import CellBuildConfig, CloudBuildDriver
from ssc_agent.cloud_run import CellRuntime, CloudRunDriver
from ssc_conformance.cloud_build_emulator import PROJECT, REGION, CloudBuildEmulator
from ssc_conformance.cloud_run_emulator import CloudRunEmulator
from ssc_conformance.contracts.blobstore import ManualClock
from ssc_conformance.contracts.build_driver import BuildDriverContract, NewRequest, until_done
from ssc_conformance.contracts.runtime_driver import CONTRACT_ORG
from ssc_contracts.ids import new_id
from ssc_contracts.manifest import Manifest
from ssc_control.deploy.build_driver import (
    BuildDriverError,
    BuildNotFoundError,
    BuildRequest,
    Failed,
    Succeeded,
)
from ssc_control.deploy.cell_build import CellAgentBuildDriver
from ssc_shared.blobstore_fs import FsBlobStore, SignedUrlError, UrlSigner
from ssc_shared.runtime import ORG_HEADER

AGENT = "https://ssc-cell-agent.test"
HEADERS = {ORG_HEADER: CONTRACT_ORG}
BLOBS = "https://control.test/v1/blobs"
REPO = f"{REGION}-docker.pkg.dev/{PROJECT}/ssc-apps/apps"
BUILD_CELL = CellBuildConfig(
    project=PROJECT,
    region=REGION,
    image_repository=REPO,
    service_account=f"ssc-build@{PROJECT}.iam.gserviceaccount.com",
    tools_image=f"{REGION}-docker.pkg.dev/ssc-platform/tools/ssc-build-tools@sha256:" + "a" * 64,
    frontend_image="ghcr.io/railwayapp/railpack-frontend@sha256:" + "f" * 64,
)
RUN_CELL = CellRuntime(
    project=PROJECT,
    region=REGION,
    network=f"projects/{PROJECT}/global/networks/ssc-cell",
    subnetwork=f"projects/{PROJECT}/regions/{REGION}/subnetworks/apps",
    image_repository=REPO,
    invoker=f"ssc-gateway@{PROJECT}.iam.gserviceaccount.com",
)


class Bundles:
    """The bundles the control plane holds, and the route that serves their signed GET URLs."""

    def __init__(self, clock: ManualClock) -> None:
        self.signer = UrlSigner({"k1": secrets.token_bytes(32)}, active="k1", clock=clock)
        self.data: dict[str, bytes] = {}
        self.fetched: list[str] = []

    def fetch(self, url: str) -> bytes:
        parts = urlsplit(url)
        assert url.startswith(BLOBS + "/")
        key = parts.path.removeprefix(urlsplit(BLOBS).path + "/")
        self.signer.verify("GET", key, dict(parse_qsl(parts.query)))
        self.fetched.append(key)
        return self.data[key]

    def new(self, *, start: str | None = None, **overrides: Any) -> BuildRequest:
        data = secrets.token_bytes(64)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        org, app = new_id("org"), new_id("app")
        key = f"bundles/{org}/{app}/sha256/{digest.removeprefix('sha256:')}.tar.gz"
        self.data[key] = data
        manifest = (
            {"schema": "ssc/v1", "runtime": {"start": start}} if start else {"schema": "ssc/v1"}
        )
        fields: dict[str, Any] = {
            "build_id": new_id("bld"),
            "org_id": org,
            "app_id": app,
            "env_name": "preview",
            "bundle_key": key,
            "source_digest": digest,
            "manifest": Manifest.model_validate(manifest),
            "public_env": {"VITE_API": "https://api.example.com"},
        }
        return BuildRequest(**{**fields, **overrides})


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def bundles(clock: ManualClock) -> Bundles:
    return Bundles(clock)


@pytest.fixture
def emulator(bundles: Bundles) -> CloudBuildEmulator:
    return CloudBuildEmulator(bundles.fetch)


async def token() -> str:
    return "access-token"


def runtime_driver() -> CloudRunDriver:
    transport = httpx2.MockTransport(CloudRunEmulator().handler)
    return CloudRunDriver(RUN_CELL, token, client=httpx2.AsyncClient(transport=transport))


@pytest.fixture
async def agent_app(emulator: CloudBuildEmulator) -> AsyncIterator[FastAPI]:
    transport = httpx2.MockTransport(emulator.handler)
    builder = CloudBuildDriver(BUILD_CELL, token, client=httpx2.AsyncClient(transport=transport))
    yield create_app(runtime_driver(), builder, org_id=CONTRACT_ORG)
    await builder.aclose()


@pytest.fixture
async def agent_driver(
    tmp_path: Path, clock: ManualClock, bundles: Bundles, agent_app: FastAPI
) -> AsyncIterator[CellAgentBuildDriver]:
    async def id_token(audience: str) -> str:
        assert audience == AGENT
        return "id-token"

    store = FsBlobStore(tmp_path / "blobs", signer=bundles.signer, base_url=BLOBS, clock=clock)
    transport = httpx2.ASGITransport(app=agent_app)
    driver = CellAgentBuildDriver(
        AGENT, id_token, store, org_id=CONTRACT_ORG, client=httpx2.AsyncClient(transport=transport)
    )
    yield driver
    await driver.aclose()


class TestCellAgentBuildDriver(BuildDriverContract):
    @pytest.fixture
    def build_driver(self, agent_driver: CellAgentBuildDriver) -> CellAgentBuildDriver:
        return agent_driver

    @pytest.fixture
    def buildable(self, bundles: Bundles) -> NewRequest:
        return bundles.new

    @pytest.fixture
    def broken(self, bundles: Bundles, emulator: CloudBuildEmulator) -> NewRequest:
        def new() -> BuildRequest:
            request = bundles.new()
            emulator.fail(request.source_digest.removeprefix("sha256:"), 11)
            return request

        return new


async def test_the_build_pushes_one_image_named_by_the_build_id(
    agent_driver: CellAgentBuildDriver, bundles: Bundles, emulator: CloudBuildEmulator
) -> None:
    request = bundles.new(start="gunicorn app:server --bind 0.0.0.0:$PORT")
    ref = await agent_driver.start(request)
    result = await until_done(agent_driver, ref, 0)
    assert isinstance(result, Succeeded)
    build = emulator.builds[ref]
    image = f"{REPO}:{request.build_id}"
    assert build["images"] == [image]
    assert build["results"]["images"][0]["digest"] == result.image_digest
    assert build["serviceAccount"].endswith(f"/ssc-build@{PROJECT}.iam.gserviceaccount.com")
    plan_env = next(s["env"] for s in build["steps"] if s["id"] == "plan")
    assert "SSC_START=gunicorn app:server --bind 0.0.0.0:$$PORT" in plan_env
    assert bundles.fetched == [request.bundle_key]


async def test_the_listed_system_packages_reach_the_plan_step(
    agent_driver: CellAgentBuildDriver, bundles: Bundles, emulator: CloudBuildEmulator
) -> None:
    request = bundles.new(system_packages=("pkg-config", "poppler-utils"))
    ref = await agent_driver.start(request)
    assert isinstance(await until_done(agent_driver, ref, 0), Succeeded)
    plan_env = next(s["env"] for s in emulator.builds[ref]["steps"] if s["id"] == "plan")
    assert "SSC_APT_PACKAGES=pkg-config poppler-utils" in plan_env


@pytest.mark.parametrize(
    ("exit_code", "code"),
    [
        (10, "SECRET_IN_BUNDLE"),
        (11, "BUILD_DEPENDENCY_UNRESOLVED"),
        (12, "BUILD_PRIVATE_REGISTRY"),
        (13, "BUILD_NO_ENTRYPOINT"),
        (14, "BUILD_EXITED_NONZERO"),
    ],
)
async def test_each_step_exit_reaches_control_as_its_reason(
    agent_driver: CellAgentBuildDriver,
    bundles: Bundles,
    emulator: CloudBuildEmulator,
    exit_code: int,
    code: str,
) -> None:
    request = bundles.new()
    emulator.fail(request.source_digest.removeprefix("sha256:"), exit_code)
    result = await until_done(agent_driver, await agent_driver.start(request), 0)
    assert isinstance(result, Failed)
    assert result.code == code


async def test_the_bundle_url_reads_one_object_for_ten_minutes(
    agent_driver: CellAgentBuildDriver,
    bundles: Bundles,
    emulator: CloudBuildEmulator,
    clock: ManualClock,
) -> None:
    request = bundles.new()
    ref = await agent_driver.start(request)
    url = next(
        e.split("=", 1)[1].replace("$$", "$")
        for e in emulator.builds[ref]["steps"][0]["env"]
        if e.startswith("SSC_BUNDLE_URL=")
    )
    params = dict(parse_qsl(urlsplit(url).query))
    other = request.bundle_key.rsplit("/", 1)[0] + "/" + "0" * 64 + ".tar.gz"
    with pytest.raises(SignedUrlError):
        bundles.signer.verify("GET", other, params)
    with pytest.raises(SignedUrlError):
        bundles.signer.verify("GET", request.bundle_key.rsplit("/", 1)[0] + "/", params)
    clock.set(clock.now() + timedelta(minutes=10, seconds=1))
    with pytest.raises(SignedUrlError):
        bundles.signer.verify("GET", request.bundle_key, params)
    result = await until_done(agent_driver, ref, 0)
    assert isinstance(result, Failed)
    assert result.code == "BUILD_DRIVER_ERROR"


async def test_a_bundle_that_is_not_the_digest_fails_in_fetch(
    agent_driver: CellAgentBuildDriver, bundles: Bundles
) -> None:
    request = bundles.new()
    bundles.data[request.bundle_key] = b"something else"
    result = await until_done(agent_driver, await agent_driver.start(request), 0)
    assert isinstance(result, Failed)
    assert result.code == "BUILD_DRIVER_ERROR"


async def test_a_cloud_build_outage_is_a_driver_error_without_the_url(
    tmp_path: Path, bundles: Bundles, clock: ManualClock
) -> None:
    async def id_token(_: str) -> str:
        return "id-token"

    down = httpx2.MockTransport(lambda _: httpx2.Response(503))
    builder = CloudBuildDriver(BUILD_CELL, token, client=httpx2.AsyncClient(transport=down))
    store = FsBlobStore(tmp_path, signer=bundles.signer, base_url=BLOBS, clock=clock)
    agent = create_app(runtime_driver(), builder, org_id=CONTRACT_ORG)
    transport = httpx2.ASGITransport(app=agent)
    driver = CellAgentBuildDriver(
        AGENT, id_token, store, org_id=CONTRACT_ORG, client=httpx2.AsyncClient(transport=transport)
    )
    with pytest.raises(BuildDriverError) as caught:
        await driver.start(bundles.new())
    assert "BUILD_ERROR" in str(caught.value)
    assert "sig=" not in str(caught.value)
    with pytest.raises(BuildNotFoundError):
        await driver.poll("not-a-cloud-build-id")


async def test_an_agent_without_a_builder_refuses_builds() -> None:
    transport = httpx2.ASGITransport(app=create_app(runtime_driver(), org_id=CONTRACT_ORG))
    async with httpx2.AsyncClient(transport=transport, base_url=AGENT, headers=HEADERS) as client:
        refused = await client.post("/v1/build/poll", json={"ref": "x"})
        assert refused.status_code == 503
        assert refused.json()["code"] == "BUILD_NOT_CONFIGURED"
        assert (await client.post("/v1/build/cancel", json={})).status_code == 404


async def test_the_agent_refuses_a_malformed_build(agent_app: FastAPI) -> None:
    plain_http = {
        "build_id": new_id("bld"),
        "bundle_url": "http://blobs.test/b.tar.gz",
        "bundle_sha256": "0" * 64,
        "public_env": {},
        "start": None,
    }
    transport = httpx2.ASGITransport(app=agent_app)
    async with httpx2.AsyncClient(transport=transport, base_url=AGENT, headers=HEADERS) as client:
        for body in ({"build": {"build_id": "nope"}}, {"build": plain_http}, []):
            answer = await client.post("/v1/build/start", json=body)
            assert answer.status_code == 400, body
            assert answer.json()["code"] == "INVALID_REQUEST"
