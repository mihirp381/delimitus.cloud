"""SSC-019: which cell is calling ``/internal/redeem`` — Google ID tokens of the gateway account."""

import json
import time
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from ssc_control.identity.cell_callers import (
    GOOGLE_CERTS,
    CellCaller,
    GoogleCallers,
    gateway_project,
)

AUDIENCE = "https://auth.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(kid: str = "g1") -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(KEY.public_key()))
    return {"keys": [{**jwk, "kid": kid, "alg": "RS256", "use": "sig"}]}


def id_token(key: Any = KEY, kid: str = "g1", **changes: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 3600,
        "email": "ssc-gateway@ssc-c-bcdfghjklmnp.iam.gserviceaccount.com",
        "email_verified": True,
        "sub": "1234",
        **changes,
    }
    return jwt.encode(
        {k: v for k, v in claims.items() if v is not None}, key, "RS256", {"kid": kid}
    )


def callers(fetches: list[str], kid: str = "g1") -> GoogleCallers:
    def handler(request: httpx2.Request) -> httpx2.Response:
        fetches.append(str(request.url))
        return httpx2.Response(200, json=jwks(kid))

    return GoogleCallers(AUDIENCE, transport=httpx2.MockTransport(handler))


async def test_the_gateway_account_names_its_project() -> None:
    fetches: list[str] = []
    c = callers(fetches)
    assert await c.caller(id_token()) == CellCaller("ssc-c-bcdfghjklmnp")
    assert await c.caller(id_token()) == CellCaller("ssc-c-bcdfghjklmnp")
    assert fetches == [GOOGLE_CERTS], "certificates are cached"
    await c.aclose()


@pytest.mark.parametrize(
    "token",
    [
        id_token(key=OTHER),
        id_token(aud="https://elsewhere.test"),
        id_token(iss="https://evil.test"),
        id_token(email_verified=False),
        id_token(email_verified=None),
        id_token(email="someone@ssc-c-x.iam.gserviceaccount.com"),
        id_token(email="ssc-gateway@example.com"),
        id_token(email=None),
        id_token(exp=int(time.time()) - 60),
        id_token(kid="unknown"),
        "not-a-jwt",
    ],
)
async def test_anything_else_is_no_caller(token: str) -> None:
    c = callers([])
    assert await c.caller(token) is None
    await c.aclose()


async def test_an_unknown_key_refetches_once() -> None:
    fetches: list[str] = []
    c = callers(fetches, kid="rotated")
    assert await c.caller(id_token(kid="g1")) is None
    assert len(fetches) == 2
    await c.aclose()


def test_gateway_project() -> None:
    assert gateway_project("ssc-gateway@p-1.iam.gserviceaccount.com") == "p-1"
    for bad in ("ssc-gateway@.iam.gserviceaccount.com", "x@p.iam.gserviceaccount.com", "p", ""):
        assert gateway_project(bad) is None
