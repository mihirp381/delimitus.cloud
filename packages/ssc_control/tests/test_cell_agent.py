"""SSC-017: the control plane's half of the agent hop: requests, error mapping, ID tokens."""

import json

import httpx2
import pytest

from ssc_control.runtime.cell_agent import (
    CellAgentDriver,
    ImpersonatedIdTokens,
    MetadataIdTokens,
)
from ssc_shared.runtime import (
    ORG_HEADER,
    RevisionNotFoundError,
    RevisionObservation,
    RuntimeDriverError,
    ServiceNotFoundError,
    ServiceObservation,
    ServiceSpec,
    observation_to_wire,
    spec_from_wire,
    spec_to_wire,
)

AGENT = "https://ssc-cell-agent-123.us-central1.run.app"
ORG = "org_aaaaaaaaaaaaaaaaaaaa"
SERVICE = "ssc-a-0123456789abcdefghijkl"
SPEC = ServiceSpec(
    service=SERVICE,
    image_digest="sha256:" + "a" * 64,
    port=8080,
    health_path="/healthz",
    resource_class="small",
    env={"PORT": "8080"},
    billing="instance",
    timeout_seconds=3600,
    concurrency=1000,
    min_instances=0,
    max_instances=1,
    labels={"ssc-env": "e"},
)


async def _id_token(audience: str) -> str:
    return f"id-token-for-{audience}"


def _driver(handler: httpx2.MockTransport) -> CellAgentDriver:
    return CellAgentDriver(
        AGENT + "/", _id_token, org_id=ORG, client=httpx2.AsyncClient(transport=handler)
    )


async def test_each_call_is_one_post_with_an_id_token_for_the_agent() -> None:
    seen: list[httpx2.Request] = []
    observation = ServiceObservation(
        service=SERVICE,
        revisions=(
            RevisionObservation(
                revision=f"{SERVICE}-00001-abcdef",
                spec_fingerprint=SPEC.spec_fingerprint,
                image_digest=SPEC.image_digest,
                ready=True,
                failed=False,
                traffic_percent=100,
            ),
        ),
        min_instances=0,
        max_instances=1,
        stopped=False,
    )

    def agent(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        method = request.url.path.rsplit("/", 1)[1]
        return httpx2.Response(
            200,
            json={
                "apply": {"revision": f"{SERVICE}-00001-abcdef"},
                "observe": {"observation": observation_to_wire(observation)},
            }.get(method, {}),
        )

    driver = _driver(httpx2.MockTransport(agent))
    assert await driver.apply(SPEC) == f"{SERVICE}-00001-abcdef"
    await driver.set_traffic(SERVICE, f"{SERVICE}-00001-abcdef")
    await driver.scale_to_zero(SERVICE)
    assert await driver.observe(SERVICE) == observation

    assert [r.url.path for r in seen] == [
        f"/v1/runtime/{m}" for m in ("apply", "set_traffic", "scale_to_zero", "observe")
    ]
    assert all(r.method == "POST" for r in seen)
    assert {r.headers["Authorization"] for r in seen} == {f"Bearer id-token-for-{AGENT}"}
    assert {r.headers[ORG_HEADER] for r in seen} == {ORG}
    assert spec_from_wire(json.loads(seen[0].content)["spec"]) == SPEC
    assert json.loads(seen[1].content) == {
        "service": SERVICE,
        "revision": f"{SERVICE}-00001-abcdef",
    }


def test_the_wire_spec_carries_billing_timeout_and_concurrency() -> None:
    wire = spec_to_wire(SPEC)
    assert (wire["billing"], wire["timeout_seconds"], wire["concurrency"]) == (
        "instance",
        3600,
        1000,
    )
    assert spec_from_wire(wire) == SPEC
    for bad in (
        {"billing": "always"},
        {"timeout_seconds": "3600"},
        {"timeout_seconds": 300},
        {"concurrency": 80},
        {"concurrency": 1001},
    ):
        with pytest.raises(ValueError):
            spec_from_wire(wire | bad)
    with pytest.raises(ValueError, match="malformed"):
        spec_from_wire({k: v for k, v in wire.items() if k != "billing"})


async def test_a_missing_service_observes_as_none() -> None:
    driver = _driver(httpx2.MockTransport(lambda _: httpx2.Response(200, json={})))
    assert await driver.observe(SERVICE) is None


@pytest.mark.parametrize(
    ("response", "error", "match"),
    [
        (
            httpx2.Response(404, json={"code": "SERVICE_NOT_FOUND", "message": "gone"}),
            ServiceNotFoundError,
            "HTTP 404 SERVICE_NOT_FOUND gone",
        ),
        (
            httpx2.Response(404, json={"code": "REVISION_NOT_FOUND", "message": "x"}),
            RevisionNotFoundError,
            "REVISION_NOT_FOUND",
        ),
        (httpx2.Response(502, text="<html>"), RuntimeDriverError, "HTTP 502"),
        (httpx2.Response(200, json={}), RuntimeDriverError, "no revision"),
        (httpx2.Response(200, json={"observation": {"service": 1}}), RuntimeDriverError, "agent"),
    ],
)
async def test_agent_errors_map_to_driver_errors(
    response: httpx2.Response, error: type[RuntimeDriverError], match: str
) -> None:
    driver = _driver(httpx2.MockTransport(lambda _: response))
    call = driver.observe(SERVICE) if "observation" in response.text else driver.apply(SPEC)
    with pytest.raises(error, match=match):
        await call


async def test_transport_failures_name_no_secret() -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(RuntimeDriverError, match="cell agent observe: ConnectError") as caught:
        await _driver(httpx2.MockTransport(down)).observe(SERVICE)
    assert "id-token" not in str(caught.value)


async def test_metadata_id_tokens_cache_per_audience() -> None:
    calls: list[httpx2.Request] = []

    def metadata(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, text=f"tok-{request.url.params['audience']}\n")

    tokens = MetadataIdTokens(httpx2.AsyncClient(transport=httpx2.MockTransport(metadata)))
    assert await tokens("https://a") == "tok-https://a"
    assert await tokens("https://a") == "tok-https://a"
    assert await tokens("https://b") == "tok-https://b"
    assert len(calls) == 2
    assert calls[0].headers["Metadata-Flavor"] == "Google"
    assert calls[0].url.params["format"] == "full"


async def test_metadata_id_token_failure() -> None:
    tokens = MetadataIdTokens(
        httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _: httpx2.Response(404)))
    )
    with pytest.raises(RuntimeDriverError, match="no ID token: HTTPStatusError"):
        await tokens("https://a")


async def test_impersonated_id_tokens_ask_iam_credentials_once() -> None:
    calls: list[httpx2.Request] = []
    control = "ssc-control@ssc-control-staging.iam.gserviceaccount.com"

    def iam(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={"token": "minted"})

    async def access() -> str:
        return "caller-access-token"

    tokens = ImpersonatedIdTokens(
        control, access, client=httpx2.AsyncClient(transport=httpx2.MockTransport(iam))
    )
    assert await tokens(AGENT) == "minted"
    assert await tokens(AGENT) == "minted"
    assert len(calls) == 1
    assert str(calls[0].url) == (
        f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{control}"
        ":generateIdToken"
    )
    assert calls[0].headers["Authorization"] == "Bearer caller-access-token"
    assert json.loads(calls[0].content) == {"audience": AGENT, "includeEmail": True}


async def test_impersonation_refused() -> None:
    async def access() -> str:
        return "caller-access-token"

    tokens = ImpersonatedIdTokens(
        "sa@p.iam.gserviceaccount.com",
        access,
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _: httpx2.Response(403))),
    )
    with pytest.raises(RuntimeDriverError, match="no ID token for sa@p") as caught:
        await tokens(AGENT)
    assert "caller-access-token" not in str(caught.value)
