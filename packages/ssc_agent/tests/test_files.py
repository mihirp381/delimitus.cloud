"""The agent's drop of an environment's files (SSC-046), against a scripted Cloud Storage JSON
API: only the environment's own prefix, every page, never while its service runs."""

from typing import Any
from urllib.parse import unquote

import httpx2
import pytest

from ssc_agent.app import create_app
from ssc_agent.files import STORAGE_API, CellFiles, environment_files
from ssc_shared.runtime import ORG_HEADER, ServiceObservation, ServiceSpec

BUCKET = "ssc-c-bcdfghjklmnp-cell"
ORG = "org_aaaaaaaaaaaaaaaaaaaa"
LIVE, STOPPED, GONE = ("ssc-a-" + c * 20 for c in "lsg")


class Storage:
    """Objects by name; lists in pages of ``page`` and deletes, as the JSON API does."""

    def __init__(self, names: list[str], page: int = 2) -> None:
        self.names = sorted(names)
        self.page = page
        self.requests: list[httpx2.Request] = []
        self.fail = False

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer token"
        if self.fail:
            return httpx2.Response(403, json={"error": {"message": "denied"}})
        listing = f"{STORAGE_API}/b/{BUCKET}/o"
        url = str(request.url).split("?")[0]
        if request.method == "GET" and url == listing:
            prefix = request.url.params["prefix"]
            start = int(request.url.params.get("pageToken", "0"))
            matching = [n for n in self.names if n.startswith(prefix)]
            listed = matching[start : start + self.page]
            body: dict[str, Any] = {"items": [{"name": n} for n in listed]}
            if start + self.page < len(matching):
                body["nextPageToken"] = str(start + self.page)
            if not body["items"]:
                del body["items"]
            return httpx2.Response(200, json=body)
        if request.method == "DELETE" and url.startswith(listing + "/"):
            name = unquote(request.url.raw_path.decode().split("?")[0].rsplit("/", 1)[-1])
            if name not in self.names:
                return httpx2.Response(404)
            self.names.remove(name)
            return httpx2.Response(204)
        return httpx2.Response(400)


class Runtime:
    """Only ``observe``: what the agent checks before it drops files."""

    def __init__(self, services: dict[str, bool]) -> None:
        self.services = services

    async def apply(self, spec: ServiceSpec) -> str:
        raise AssertionError(spec.service)

    async def set_traffic(self, service: str, revision: str) -> None:
        raise AssertionError(service)

    async def scale_to_zero(self, service: str) -> None:
        raise AssertionError(service)

    async def observe(self, service: str) -> ServiceObservation | None:
        if service not in self.services:
            return None
        return ServiceObservation(
            service=service,
            revisions=(),
            min_instances=0,
            max_instances=1,
            stopped=self.services[service],
        )


async def token() -> str:
    return "token"


def files_of(storage: Storage) -> CellFiles:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(storage.handle))
    return CellFiles(BUCKET, token, client=client)


def agent(storage: Storage | None, services: dict[str, bool]) -> httpx2.AsyncClient:
    files = None if storage is None else files_of(storage)
    app = create_app(driver=Runtime(services), files=files, org_id=ORG)
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://agent", headers={ORG_HEADER: ORG}
    )


def test_the_prefix_is_the_environment_s() -> None:
    assert environment_files(LIVE) == "files/env_" + "l" * 20 + "/"
    with pytest.raises(ValueError, match="service name"):
        environment_files("ssc-a-../x")


async def test_a_drop_deletes_every_page_of_the_environment_s_files_and_nothing_else() -> None:
    gone = environment_files(GONE)
    other = environment_files(STOPPED)
    mine = [f"{gone}photos/{i}.png" for i in range(5)] + [f"{gone}a b/c%.txt"]
    storage = Storage([*mine, f"{other}photos/0.png", "snapshots/org_x/1.json"])
    deleted = await files_of(storage).drop(GONE)
    again = await files_of(storage).drop(GONE)
    assert (deleted, again) == (6, 0)
    assert storage.names == [f"{other}photos/0.png", "snapshots/org_x/1.json"]
    lists = [r for r in storage.requests if r.method == "GET"]
    assert {r.url.params["prefix"] for r in lists} == {gone}


async def test_the_agent_drops_only_files_whose_service_is_gone_or_stopped() -> None:
    names = [environment_files(s) + "f.txt" for s in (LIVE, STOPPED, GONE)]
    storage = Storage(names)
    async with agent(storage, {LIVE: False, STOPPED: True}) as http:
        refused = await http.post("/v1/files/drop", json={"service": LIVE})
        stopped = await http.post("/v1/files/drop", json={"service": STOPPED})
        gone = await http.post("/v1/files/drop", json={"service": GONE})
        bad = await http.post("/v1/files/drop", json={"service": "postgres"})
        unknown = await http.post("/v1/files/read", json={"service": GONE})
    assert (refused.status_code, refused.json()["code"]) == (409, "SERVICE_LIVE")
    assert (stopped.status_code, stopped.json()) == (200, {"dropped": STOPPED, "files": 1})
    assert (gone.status_code, gone.json()) == (200, {"dropped": GONE, "files": 1})
    assert (bad.status_code, bad.json()["code"]) == (400, "INVALID_REQUEST")
    assert (unknown.status_code, unknown.json()["code"]) == (404, "NOT_FOUND")
    assert storage.names == [names[0]]


async def test_a_storage_refusal_and_an_agent_without_a_bucket() -> None:
    storage = Storage([])
    storage.fail = True
    async with agent(storage, {}) as http:
        failed = await http.post("/v1/files/drop", json={"service": GONE})
    async with agent(None, {}) as http:
        unconfigured = await http.post("/v1/files/drop", json={"service": GONE})
    assert (failed.status_code, failed.json()["code"]) == (502, "FILES_ERROR")
    assert (unconfigured.status_code, unconfigured.json()["code"]) == (503, "FILES_NOT_CONFIGURED")
