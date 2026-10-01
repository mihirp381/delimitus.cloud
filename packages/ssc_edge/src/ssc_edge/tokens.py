"""Google ID and access tokens for the gateway's own service account, from the metadata server.

An app's Cloud Run service checks ``X-Serverless-Authorization`` (only the gateway holds
``run.invoker``) and strips the token's signature before the app sees it, so app code never holds
a verifiable Google token (SSC-017). ID tokens are cached per audience until 5 minutes before
they expire.
"""

import asyncio
import base64
import json
import time
from collections.abc import Callable
from typing import Final, cast

import httpx2

METADATA: Final = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default"
)
REFRESH_BEFORE: Final = 300


class TokenError(RuntimeError):
    pass


def _exp(token: str) -> float:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (IndexError, ValueError, KeyError, TypeError) as exc:
        raise TokenError("the metadata server returned an unreadable ID token") from exc


class MetadataTokens:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._clock = clock
        self._ids: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()
        self._client = httpx2.AsyncClient(
            timeout=5, headers={"metadata-flavor": "Google"}, transport=transport
        )

    async def identity(self, audience: str) -> str:
        cached = self._ids.get(audience)
        if cached is not None and cached[1] - REFRESH_BEFORE > self._clock():
            return cached[0]
        async with self._lock:
            cached = self._ids.get(audience)
            if cached is not None and cached[1] - REFRESH_BEFORE > self._clock():
                return cached[0]
            token = await self._get("/identity", {"audience": audience, "format": "full"})
            self._ids[audience] = (token, _exp(token))
            return token

    async def access(self) -> str:
        body = await self._get("/token", {})
        return str(cast(dict[str, object], json.loads(body))["access_token"])

    async def _get(self, path: str, params: dict[str, str]) -> str:
        try:
            r = await self._client.get(METADATA + path, params=params)
        except httpx2.HTTPError as exc:
            raise TokenError(f"metadata server unreachable: {type(exc).__name__}") from exc
        if r.status_code != 200:  # noqa: PLR2004
            raise TokenError(f"metadata server answered HTTP {r.status_code}")
        return r.text

    async def aclose(self) -> None:
        await self._client.aclose()
