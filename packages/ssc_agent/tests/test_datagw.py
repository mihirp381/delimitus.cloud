"""GA-5.8: the agent asks the cell's data gateway for a connection's tables and columns with its
own ID token, names the environment, and hands back the gateway's status and JSON as they are."""

import logging

import httpx2
import pytest

from ssc_agent.__main__ import ConfigError, datagw_from_env
from ssc_agent.app import WRONG_CELL, create_app
from ssc_agent.datagw import ENVIRONMENT_HEADER, DataGateway
from ssc_agent.metadata import MetadataError, MetadataIdTokens
from ssc_shared.runtime import ORG_HEADER, ServiceObservation, ServiceSpec

ORG = "org_" + "a" * 20
ENV = "env_" + "b" * 20
GATEWAY = "https://ssc-datagw-123.us-central1.run.app"
AUDIENCE = "https://ssc-datagw-123.us-central1.run.app"
SCHEMA = {
    "connection": "finance",
    "kind": "postgres",
    "tables": [{"name": "public.orders", "columns": [{"name": "id", "type": "integer"}]}],
    "snapshot_version": 7,
    "request_id": "r1",
}


class Idle:
    async def apply(self, spec: ServiceSpec) -> str:
        raise AssertionError(spec)

    async def set_traffic(self, service: str, revision: str) -> None:
        raise AssertionError(service)

    async def scale_to_zero(self, service: str) -> None:
        raise AssertionError(service)

    async def observe(self, service: str) -> ServiceObservation | None:
        raise AssertionError(service)


def gateway(handler: httpx2.MockTransport, tokens: list[str] | None = None) -> DataGateway:
    async def id_token() -> str:
        if tokens is not None:
            tokens.append("minted")
        return "id-token"

    return DataGateway(GATEWAY + "/", id_token, httpx2.AsyncClient(transport=handler))


def agent(datagw: DataGateway | None, org: str = ORG) -> httpx2.AsyncClient:
    app = create_app(driver=Idle(), org_id=ORG, datagw=datagw)
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://agent", headers={ORG_HEADER: org}
    )


async def test_schema_asks_the_gateway_as_the_environment_and_passes_its_answer_back() -> None:
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=SCHEMA)

    async with agent(gateway(httpx2.MockTransport(answer))) as client:
        r = await client.post(
            "/v1/datagw/schema", json={"connection": "finance", "environment_id": ENV}
        )
    assert r.status_code == 200
    assert r.json() == {"status": 200, "body": SCHEMA}
    (request,) = seen
    assert str(request.url) == f"{GATEWAY}/v1/connections/finance/schema"
    assert request.method == "GET"
    assert request.headers["Authorization"] == "Bearer id-token"
    assert request.headers[ENVIRONMENT_HEADER] == ENV


async def test_a_gateway_refusal_comes_back_with_its_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    refusal = {"error": {"code": "CONNECTION_NOT_GRANTED", "stage": "admission"}}
    handler = httpx2.MockTransport(lambda _: httpx2.Response(403, json=refusal))
    caplog.set_level(logging.INFO)
    async with agent(gateway(handler)) as client:
        r = await client.post(
            "/v1/datagw/schema", json={"connection": "finance", "environment_id": ENV}
        )
    assert r.json() == {"status": 403, "body": refusal}
    assert "CONNECTION_NOT_GRANTED" not in caplog.text
    assert [getattr(x, "status", None) for x in caplog.records if x.message == "datagw call"] == [
        403
    ]


async def test_a_body_that_is_not_json_comes_back_as_null() -> None:
    handler = httpx2.MockTransport(lambda _: httpx2.Response(502, text="<html>bad gateway"))
    async with agent(gateway(handler)) as client:
        r = await client.post(
            "/v1/datagw/schema", json={"connection": "finance", "environment_id": ENV}
        )
    assert r.json() == {"status": 502, "body": None}


async def test_a_gateway_that_cannot_be_reached_is_datagw_error() -> None:
    def lost(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectTimeout("slow", request=request)

    async with agent(gateway(httpx2.MockTransport(lost))) as client:
        r = await client.post(
            "/v1/datagw/schema", json={"connection": "finance", "environment_id": ENV}
        )
    assert r.status_code == 502
    assert r.json()["code"] == "DATAGW_ERROR"


async def test_no_id_token_is_datagw_error() -> None:
    async def refused() -> str:
        raise MetadataError("no ID token: HTTPStatusError")

    datagw = DataGateway(
        GATEWAY,
        refused,
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json=SCHEMA))
        ),
    )
    async with agent(datagw) as client:
        r = await client.post(
            "/v1/datagw/schema", json={"connection": "finance", "environment_id": ENV}
        )
    assert (r.status_code, r.json()["code"]) == (502, "DATAGW_ERROR")


async def test_without_a_gateway_address_the_agent_refuses() -> None:
    async with agent(None) as client:
        r = await client.post(
            "/v1/datagw/schema", json={"connection": "finance", "environment_id": ENV}
        )
        other = await client.post("/v1/datagw/query", json={})
    assert (r.status_code, r.json()["code"]) == (503, "DATAGW_NOT_CONFIGURED")
    assert other.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"connection": "Finance", "environment_id": ENV},
        {"connection": "../x", "environment_id": ENV},
        {"connection": "finance", "environment_id": "app_" + "b" * 20},
        {"connection": "finance"},
        {"connection": 3, "environment_id": ENV},
    ],
)
async def test_a_bad_name_or_environment_is_refused_before_the_gateway(
    body: dict[str, object],
) -> None:
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=SCHEMA)

    async with agent(gateway(httpx2.MockTransport(answer))) as client:
        r = await client.post("/v1/datagw/schema", json=body)
    assert (r.status_code, r.json()["code"]) == (400, "INVALID_REQUEST")
    assert seen == []


async def test_a_schema_call_for_another_org_is_refused() -> None:
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=SCHEMA)

    async with agent(gateway(httpx2.MockTransport(answer)), org="org_" + "z" * 20) as client:
        r = await client.post(
            "/v1/datagw/schema", json={"connection": "finance", "environment_id": ENV}
        )
    assert (r.status_code, r.json()["code"]) == (403, WRONG_CELL)
    assert seen == []


async def test_the_id_token_names_the_audience_and_is_cached() -> None:
    calls: list[httpx2.Request] = []

    def metadata(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, text="id-token\n")

    tokens = MetadataIdTokens(
        AUDIENCE, httpx2.AsyncClient(transport=httpx2.MockTransport(metadata))
    )
    assert await tokens() == "id-token"
    assert await tokens() == "id-token"
    (call,) = calls
    assert call.url.path.endswith("/instance/service-accounts/default/identity")
    assert call.url.params["audience"] == AUDIENCE
    assert call.headers["Metadata-Flavor"] == "Google"


async def test_an_id_token_failure_names_no_token() -> None:
    tokens = MetadataIdTokens(
        AUDIENCE,
        httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _: httpx2.Response(403))),
    )
    with pytest.raises(MetadataError, match="no ID token"):
        await tokens()


def test_the_gateway_address_needs_both_settings() -> None:
    assert datagw_from_env({}) is None
    both = {"SSC_DATAGW_URL": GATEWAY, "SSC_DATAGW_AUDIENCE": AUDIENCE}
    assert datagw_from_env(both) == (GATEWAY, AUDIENCE)
    for half in ({"SSC_DATAGW_URL": GATEWAY}, {"SSC_DATAGW_AUDIENCE": AUDIENCE}):
        with pytest.raises(ConfigError, match="both"):
            datagw_from_env(half)
    with pytest.raises(ConfigError, match="https"):
        datagw_from_env({**both, "SSC_DATAGW_URL": "http://ssc-datagw"})
