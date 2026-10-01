"""Tokens from the metadata server of the Cloud Run instance this code runs on."""

import asyncio
import time
from typing import Final

import httpx2

METADATA: Final = "http://metadata.google.internal/computeMetadata/v1"
HEADERS: Final = {"Metadata-Flavor": "Google"}
EARLY_SECONDS: Final = 300


class MetadataError(RuntimeError):
    pass


class MetadataAccessTokens:
    """The instance identity's access token, refreshed five minutes before it expires."""

    def __init__(self, client: httpx2.AsyncClient | None = None) -> None:
        self._client = client or httpx2.AsyncClient(timeout=5.0)
        self._token = ""
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            if time.monotonic() >= self._expires:
                url = f"{METADATA}/instance/service-accounts/default/token"
                try:
                    response = await self._client.get(url, headers=HEADERS)
                    response.raise_for_status()
                    body = response.json()
                except (httpx2.HTTPError, ValueError) as exc:
                    raise MetadataError(f"no access token: {type(exc).__name__}") from None
                self._token = str(body["access_token"])
                self._expires = time.monotonic() + int(body["expires_in"]) - EARLY_SECONDS
            return self._token
