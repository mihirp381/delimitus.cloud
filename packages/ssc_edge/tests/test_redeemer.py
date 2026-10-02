"""Redeeming a login code at the auth host (SSC-019): what is sent; anything odd is no session."""

import json

import httpx2
import pytest
from edge_world import HOST, LABEL, NONCE, NOW, ORG
from test_server import Tokens, env

from ssc_edge.redeemer import HttpRedeemer, session_of
from ssc_edge.server import SettingsError, redeemer_for, settings_from_env
from ssc_edge.tokens import TokenError

ADA = "usr_" + "a" * 20
GOOD = {
    "sub": ADA,
    "org": ORG,
    "name": "Ada",
    "email": "ada@example.test",
    "iat": NOW,
    "exp": NOW + 3600,
}


def redeemer(handler, bearer: str = "tok") -> HttpRedeemer:  # noqa: ANN001
    async def token() -> str:
        return bearer

    return HttpRedeemer(
        auth_url="https://auth.example.test",
        org_id=ORG,
        bearer=token,
        transport=httpx2.MockTransport(handler),
    )


async def test_the_code_host_and_nonce_go_to_the_auth_host() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=GOOD)

    r = redeemer(handler)
    session = await r.redeem("c0de", HOST, NONCE)
    await r.aclose()
    assert session is not None and session.sub == ADA and session.exp == NOW + 3600
    (request,) = seen
    assert str(request.url) == "https://auth.example.test/internal/redeem"
    assert request.headers["authorization"] == "Bearer tok"
    assert json.loads(request.content) == {"org": ORG, "code": "c0de", "host": HOST, "nonce": NONCE}


async def test_every_session_gets_its_own_id() -> None:
    r = redeemer(lambda _: httpx2.Response(200, json=GOOD))
    first, second = await r.redeem("a", HOST, NONCE), await r.redeem("b", HOST, NONCE)
    assert first is not None and second is not None and first.sid != second.sid


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(403, json={"error": "refused"}),
        httpx2.Response(500),
        httpx2.Response(200, content=b"not json"),
        httpx2.Response(200, json=[GOOD]),
        httpx2.Response(200, json={**GOOD, "sub": "someone"}),
        httpx2.Response(200, json={**GOOD, "iat": "1"}),
        httpx2.Response(200, json={**GOOD, "iat": True}),
        httpx2.Response(200, json={**GOOD, "exp": NOW + 13 * 3600}),
        httpx2.Response(200, json={k: v for k, v in GOOD.items() if k != "email"}),
    ],
)
async def test_anything_unexpected_is_no_session(response: httpx2.Response) -> None:
    assert await redeemer(lambda _: response).redeem("c0de", HOST, NONCE) is None


async def test_a_network_or_token_failure_is_no_session() -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down", request=request)

    assert await redeemer(down).redeem("c0de", HOST, NONCE) is None

    async def no_token() -> str:
        raise TokenError("metadata server down")

    r = HttpRedeemer(
        auth_url="https://auth.example.test",
        org_id=ORG,
        bearer=no_token,
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json=GOOD)),
    )
    assert await r.redeem("c0de", HOST, NONCE) is None


def test_session_of_requires_every_field() -> None:
    assert session_of(json.dumps(GOOD).encode()) is not None
    assert session_of(b"null") is None


async def test_production_presents_an_id_token_for_the_auth_host() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["authorization"])
        return httpx2.Response(200, json=GOOD)

    tokens = Tokens()
    r = redeemer_for(settings_from_env(env()), tokens, httpx2.MockTransport(handler))
    await r.redeem("c0de", HOST, NONCE)
    assert tokens.audiences == ["https://auth.delimitus.com"]
    assert seen == ["Bearer google.id.token"]


async def test_dev_presents_the_rig_secret_and_only_in_dev_or_test() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["authorization"])
        return httpx2.Response(200, json=GOOD)

    secret = "s" * 32
    settings = settings_from_env(
        env(SSC_ENV="dev", SSC_AUTH_DEV_CELL_SECRET=secret, SSC_CELL_LABEL=LABEL)
    )
    tokens = Tokens()
    await redeemer_for(settings, tokens, httpx2.MockTransport(handler)).redeem("c", HOST, NONCE)
    assert seen == [f"Bearer dev.{secret}"] and tokens.audiences == []
    with pytest.raises(SettingsError, match="dev or test"):
        settings_from_env(env(SSC_AUTH_DEV_CELL_SECRET=secret))
