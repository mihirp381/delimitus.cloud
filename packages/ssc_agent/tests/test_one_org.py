"""One agent serves one org (decision 029): a call without that org's ``X-SSC-Org``, or a spec
labelled for another org, is refused ``WRONG_CELL`` before anything in the cell changes, and
``/healthz`` stays open for the platform's probes."""

import httpx2
import pytest

from ssc_agent.__main__ import ENV, ORG_ENV, ConfigError, main, org_from_env
from ssc_agent.app import WRONG_CELL, create_app
from ssc_shared.runtime import (
    ORG_HEADER,
    ORG_LABEL,
    ServiceObservation,
    ServiceSpec,
    spec_to_wire,
)

ORG = "org_" + "a" * 20
OTHER = "org_" + "b" * 20
SERVICE = "ssc-a-" + "c" * 20
DIGEST = "sha256:" + "0" * 64


class Recording:
    """A runtime that records what reached it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def apply(self, spec: ServiceSpec) -> str:
        self.calls.append(f"apply {spec.service}")
        return spec.service + "-00001"

    async def set_traffic(self, service: str, revision: str) -> None:
        self.calls.append(f"set_traffic {service}")

    async def scale_to_zero(self, service: str) -> None:
        self.calls.append(f"scale_to_zero {service}")

    async def observe(self, service: str) -> ServiceObservation | None:
        self.calls.append(f"observe {service}")


def spec(labels: dict[str, str]) -> dict[str, object]:
    return spec_to_wire(
        ServiceSpec(
            service=SERVICE,
            image_digest=DIGEST,
            port=8080,
            health_path="/healthz",
            resource_class="small",
            env={"PORT": "8080"},
            billing="request",
            timeout_seconds=300,
            concurrency=80,
            min_instances=0,
            max_instances=1,
            labels=labels,
        )
    )


def agent(runtime: Recording, headers: dict[str, str]) -> httpx2.AsyncClient:
    app = create_app(driver=runtime, org_id=ORG)
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://agent", headers=headers
    )


@pytest.mark.parametrize("headers", [{}, {ORG_HEADER: OTHER}, {ORG_HEADER: ""}])
@pytest.mark.parametrize(
    "path", ["/v1/runtime/observe", "/v1/runtime/apply", "/v1/egress/info", "/v1/nowhere"]
)
async def test_a_call_for_another_org_is_refused(headers: dict[str, str], path: str) -> None:
    runtime = Recording()
    async with agent(runtime, headers) as client:
        response = await client.post(path, json={"service": SERVICE})
    assert response.status_code == 403
    assert response.json()["code"] == WRONG_CELL
    assert runtime.calls == []


async def test_healthz_needs_no_org() -> None:
    async with agent(Recording(), {}) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200


async def test_the_org_s_own_calls_reach_the_runtime() -> None:
    runtime = Recording()
    async with agent(runtime, {ORG_HEADER: ORG}) as client:
        observed = await client.post("/v1/runtime/observe", json={"service": SERVICE})
        applied = await client.post("/v1/runtime/apply", json={"spec": spec({ORG_LABEL: ORG})})
    assert observed.status_code == 200
    assert applied.status_code == 200
    assert runtime.calls == [f"observe {SERVICE}", f"apply {SERVICE}"]


@pytest.mark.parametrize("labels", [{ORG_LABEL: OTHER}, {}, {"ssc-env": "env_" + "c" * 20}])
async def test_apply_refuses_a_spec_not_labelled_with_the_org(labels: dict[str, str]) -> None:
    runtime = Recording()
    async with agent(runtime, {ORG_HEADER: ORG}) as client:
        response = await client.post("/v1/runtime/apply", json={"spec": spec(labels)})
    assert response.status_code == 403
    assert response.json()["code"] == WRONG_CELL
    assert runtime.calls == []


def test_create_app_needs_an_org_id() -> None:
    with pytest.raises(ValueError, match="org id"):
        create_app(driver=Recording(), org_id="env_" + "a" * 20)


def test_org_from_env_needs_an_org_id() -> None:
    assert org_from_env({ORG_ENV: ORG}) == ORG
    with pytest.raises(ConfigError, match=ORG_ENV):
        org_from_env({})
    for bad in ("org_short", "env_" + "a" * 20, "ORG_" + "A" * 20):
        with pytest.raises(ConfigError, match=ORG_ENV):
            org_from_env({ORG_ENV: bad})


def test_the_agent_exits_2_without_its_org(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV.values():
        monkeypatch.setenv(name, "value")
    monkeypatch.delenv(ORG_ENV, raising=False)
    assert main() == 2
    monkeypatch.setenv(ORG_ENV, "org_bad")
    assert main() == 2
