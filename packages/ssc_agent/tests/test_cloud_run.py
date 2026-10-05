import json
from collections.abc import Sequence
from typing import Any

import httpx2
import pytest

from ssc_agent.cloud_run import (
    IDENTITY_CREATE_TRIES,
    CellRuntime,
    CloudRunDriver,
    _ApiError,  # pyright: ignore[reportPrivateUsage]
)

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


async def test_each_service_gets_its_own_post() -> None:
    iam = Iam([200])
    driver = iam.driver()
    await driver.ensure_identity("ssc-a-one")
    await driver.ensure_identity("ssc-a-two")
    assert [post["accountId"] for post in iam.posts] == ["ssc-a-one", "ssc-a-two"]
