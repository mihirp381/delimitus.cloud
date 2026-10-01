"""ID tokens for Cloud Run from the metadata server (SSC-018)."""

import base64
import json

import httpx2
import pytest

from ssc_edge.tokens import MetadataTokens, TokenError

AUD = "https://ssc-a-" + "p" * 20 + "-1.us-central1.run.app"


def token(exp: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


async def test_id_tokens_are_cached_until_five_minutes_before_expiry() -> None:
    now = [1000.0]
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, text=token(int(now[0]) + 3600))

    tokens = MetadataTokens(clock=lambda: now[0], transport=httpx2.MockTransport(handler))
    first = await tokens.identity(AUD)
    assert await tokens.identity(AUD) == first and len(calls) == 1
    assert calls[0].headers["metadata-flavor"] == "Google"
    assert calls[0].url.params["audience"] == AUD
    now[0] += 3600 - 299
    await tokens.identity(AUD)
    assert len(calls) == 2
    await tokens.identity(AUD + "x")
    assert len(calls) == 3
    await tokens.aclose()


@pytest.mark.parametrize("response", [httpx2.Response(404), httpx2.Response(200, text="not-a-jwt")])
async def test_a_bad_answer_is_a_token_error(response: httpx2.Response) -> None:
    tokens = MetadataTokens(transport=httpx2.MockTransport(lambda _: response))
    with pytest.raises(TokenError):
        await tokens.identity(AUD)


async def test_an_unreachable_metadata_server_is_a_token_error() -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(TokenError, match="unreachable"):
        await MetadataTokens(transport=httpx2.MockTransport(down)).identity(AUD)
