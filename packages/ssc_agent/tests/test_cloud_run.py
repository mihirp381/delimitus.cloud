import json
from collections.abc import Sequence
from typing import Any

import httpx2
import pytest

from ssc_agent.cloud_run import (
    IDENTITY_CREATE_TRIES,
    INGRESS,
    TRANSIENT_TRIES,
    CellRuntime,
    CloudRunDriver,
    _ApiError,  # pyright: ignore[reportPrivateUsage]
)
from ssc_shared.runtime import RuntimeDriverError, ServiceSpec

PROJECT = "ssc-c-test"
CELL = CellRuntime(
    project=PROJECT,
    region="us-central1",
    network=f"projects/{PROJECT}/global/networks/ssc-cell",
    subnetwork=f"projects/{PROJECT}/regions/us-central1/subnetworks/apps",
    image_repository=f"us-central1-docker.pkg.dev/{PROJECT}/ssc-apps/apps",
    invoker=f"gateway@{PROJECT}.iam.gserviceaccount.com",
)


class Iam:
    """A fake IAM that answers each create with the next scripted status, then repeats the last."""

    def __init__(self, statuses: Sequence[int]) -> None:
        self.statuses = list(statuses)
        self.posts: list[dict[str, Any]] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        assert request.url.path == f"/v1/projects/{PROJECT}/serviceAccounts"
        self.posts.append(json.loads(request.content))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx2.Response(status, json={"error": {"message": f"status {status}"}})

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def driver(self) -> CloudRunDriver:
        async def token() -> str:
            return "dummy-token"

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))
        return CloudRunDriver(CELL, token, client=client, sleep=self.sleep)


async def test_three_calls_for_one_service_make_one_post() -> None:
    iam = Iam([200])
    driver = iam.driver()
    for _ in range(3):
        await driver.ensure_identity("ssc-a-one")
    assert [post["accountId"] for post in iam.posts] == ["ssc-a-one"]


async def test_a_409_counts_as_existing_and_is_remembered() -> None:
    iam = Iam([409])
    driver = iam.driver()
    await driver.ensure_identity("ssc-a-one")
    await driver.ensure_identity("ssc-a-one")
    assert len(iam.posts) == 1
    assert iam.sleeps == []


async def test_a_429_is_retried_with_back_off() -> None:
    iam = Iam([429, 429, 200])
    driver = iam.driver()
    await driver.ensure_identity("ssc-a-one")
    assert len(iam.posts) == 3
    assert iam.sleeps == [1.0, 2.0]
    await driver.ensure_identity("ssc-a-one")
    assert len(iam.posts) == 3


async def test_a_429_on_every_try_raises_after_all_tries() -> None:
    iam = Iam([429])
    driver = iam.driver()
    with pytest.raises(_ApiError) as raised:
        await driver.ensure_identity("ssc-a-one")
    assert raised.value.status == 429
    assert len(iam.posts) == IDENTITY_CREATE_TRIES
    assert iam.sleeps == [min(2.0**n, 10.0) for n in range(IDENTITY_CREATE_TRIES - 1)]


async def test_a_403_raises_at_once_and_is_not_remembered() -> None:
    iam = Iam([403, 200])
    driver = iam.driver()
    with pytest.raises(_ApiError) as raised:
        await driver.ensure_identity("ssc-a-one")
    assert raised.value.status == 403
    assert len(iam.posts) == 1
    assert iam.sleeps == []
    await driver.ensure_identity("ssc-a-one")
    assert len(iam.posts) == 2


async def test_what_the_api_answered_is_redacted_in_the_error() -> None:
    def refuse(_: httpx2.Request) -> httpx2.Response:
        error = {"status": "INVALID_ARGUMENT", "message": "bad env DB_URL password=hunter2"}
        return httpx2.Response(400, json={"error": error})

    async def token() -> str:
        return "dummy-token"

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(refuse))
    driver = CloudRunDriver(CELL, token, client=client)
    with pytest.raises(_ApiError) as raised:
        await driver.ensure_identity("ssc-a-one")
    assert raised.value.status == 400
    assert "hunter2" not in raised.value.reason
    assert "hunter2" not in str(raised.value)
    assert raised.value.reason == "INVALID_ARGUMENT bad env DB_URL password=[redacted]"


async def test_each_service_gets_its_own_post() -> None:
    iam = Iam([200])
    driver = iam.driver()
    await driver.ensure_identity("ssc-a-one")
    await driver.ensure_identity("ssc-a-two")
    assert [post["accountId"] for post in iam.posts] == ["ssc-a-one", "ssc-a-two"]


async def test_the_startup_probe_checks_every_second_for_two_minutes() -> None:
    spec = ServiceSpec(
        service="ssc-a-one",
        image_digest="sha256:" + "a" * 64,
        port=8080,
        health_path="/healthz",
        resource_class="small",
        env={"PORT": "8080"},
        billing="request",
        timeout_seconds=300,
        concurrency=80,
        min_instances=0,
        max_instances=1,
        labels={},
    )
    template = Iam([200]).driver()._template(spec, "rev-1")  # pyright: ignore[reportPrivateUsage]
    (container,) = template["containers"]
    probe = container["startupProbe"]
    assert probe["httpGet"] == {"path": "/healthz", "port": 8080}
    assert (probe["periodSeconds"], probe["timeoutSeconds"], probe["failureThreshold"]) == (
        1,
        1,
        120,
    )
    assert probe["periodSeconds"] * probe["failureThreshold"] == 120
    assert probe["timeoutSeconds"] <= probe["periodSeconds"]


SERVICE = "ssc-a-0123456789abcdefghij"
REVISION = f"{SERVICE}-00002-abcdef"
SERVICE_PATH = f"/v2/{CELL.service_path(SERVICE)}"
SETTLED: dict[str, Any] = {
    "etag": "e2",
    "generation": 2,
    "observedGeneration": 2,
    "ingress": INGRESS,
    "template": {"revision": REVISION},
}
RECONCILING = SETTLED | {"reconciling": True}
BEHIND = SETTLED | {"observedGeneration": 1}
HALF_FILLED = {"etag": "e2", "generation": 2, "observedGeneration": 2}


class Run:
    """A fake Cloud Run that answers each read of the service with the next scripted body or
    status, then repeats the last, and logs what it was asked."""

    def __init__(self, reads: Sequence[dict[str, Any] | int]) -> None:
        self.reads = list(reads)
        self.log: list[str] = []
        self.patches: list[dict[str, Any]] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == f"{SERVICE_PATH}/revisions":
            return httpx2.Response(
                200, json={"revisions": [{"name": f"{SERVICE_PATH}/{REVISION}"}]}
            )
        assert request.url.path == SERVICE_PATH
        if request.method == "PATCH":
            self.log.append("patch")
            self.patches.append(json.loads(request.content))
            return httpx2.Response(200, json={})
        assert request.method == "GET"
        read = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        if isinstance(read, int):
            self.log.append(str(read))
            error = {"status": "UNAVAILABLE", "message": "The service is currently unavailable"}
            return httpx2.Response(read, json={"error": error})
        self.log.append("settled" if read is SETTLED or read is HALF_FILLED else "reconciling")
        return httpx2.Response(200, json=read)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def driver(self) -> CloudRunDriver:
        async def token() -> str:
            return "dummy-token"

        client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))
        return CloudRunDriver(CELL, token, client=client, sleep=self.sleep)


async def test_set_traffic_waits_for_a_reconciling_service_before_it_reads_and_patches() -> None:
    run = Run([RECONCILING, RECONCILING, SETTLED])
    await run.driver().set_traffic(SERVICE, REVISION)
    assert run.log == ["reconciling", "reconciling", "settled", "settled", "patch", "settled"]
    assert run.sleeps == [1.0]
    (patch,) = run.patches
    assert patch["traffic"] == [
        {"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION", "revision": REVISION, "percent": 100}
    ]
    assert patch["ingress"] == INGRESS
    assert patch["invokerIamDisabled"] is False


async def test_set_traffic_keeps_ingress_internal_when_the_read_lacks_it() -> None:
    run = Run([HALF_FILLED])
    await run.driver().set_traffic(SERVICE, REVISION)
    (patch,) = run.patches
    assert patch["ingress"] == INGRESS
    assert patch["invokerIamDisabled"] is False


async def test_scale_to_zero_waits_for_a_service_behind_its_generation() -> None:
    run = Run([BEHIND, SETTLED])
    await run.driver().scale_to_zero(SERVICE)
    assert run.log == ["reconciling", "settled", "settled", "patch", "settled"]
    (patch,) = run.patches
    assert patch["scaling"] == {"scalingMode": "MANUAL", "manualInstanceCount": 0}
    assert patch["ingress"] == INGRESS


async def test_a_503_is_retried_with_back_off() -> None:
    run = Run([503, SETTLED])
    assert await run.driver().settled(SERVICE)
    assert run.log == ["503", "settled"]
    assert run.sleeps == [1.0]


async def test_a_503_on_every_try_raises_after_all_tries() -> None:
    run = Run([503])
    with pytest.raises(RuntimeDriverError, match="HTTP 503 UNAVAILABLE"):
        await run.driver().settled(SERVICE)
    assert run.log == ["503"] * TRANSIENT_TRIES
    assert run.sleeps == [min(2.0**n, 10.0) for n in range(TRANSIENT_TRIES - 1)]


async def test_a_dropped_connection_is_retried() -> None:
    run = Run([SETTLED])
    dropped = [True]

    def handler(request: httpx2.Request) -> httpx2.Response:
        if dropped and dropped.pop():
            raise httpx2.ConnectError("reset", request=request)
        return run.handler(request)

    async def token() -> str:
        return "dummy-token"

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    driver = CloudRunDriver(CELL, token, client=client, sleep=run.sleep)
    assert await driver.settled(SERVICE)
    assert run.sleeps == [1.0]


async def test_wait_settled_still_gives_up_after_its_time() -> None:
    run = Run([RECONCILING])
    with pytest.raises(RuntimeDriverError, match="not settled after 3 s"):
        await run.driver().wait_settled(SERVICE, within=3)
    assert run.sleeps == [1.0, 1.0, 1.0]
