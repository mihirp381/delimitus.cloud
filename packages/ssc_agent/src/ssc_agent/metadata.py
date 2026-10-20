"""Tokens from the metadata server of the Cloud Run instance this code runs on."""

import asyncio
import time
from collections.abc import Callable
from typing import Final

import httpx2
import jwt

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


ID_TOKEN_SECONDS: Final = 3600
MIN_CACHE_SECONDS: Final = 5


def _seconds_left(token: str, now: float) -> float | None:
    """Seconds until the token's own ``exp`` (read, not verified), or None when unreadable."""
    try:
        exp = jwt.decode(token, options={"verify_signature": False}).get("exp")
    except jwt.PyJWTError:
        return None
    if not isinstance(exp, int | float) or isinstance(exp, bool):
        return None
    return float(exp) - now


class MetadataIdTokens:
    """Google ID tokens for one audience, the instance identity's, refreshed five minutes before
    they expire: by the token's own ``exp``, at most an hour after it was fetched, and never
    sooner than ``MIN_CACHE_SECONDS``, so a token that is nearly spent or unreadable is not
    fetched again on every call."""

    def __init__(
        self,
        audience: str,
        client: httpx2.AsyncClient | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._audience = audience
        self._client = client or httpx2.AsyncClient(timeout=5.0)
        self._monotonic = monotonic
        self._wall = wall
        self._token = ""
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            if self._monotonic() >= self._expires:
                url = f"{METADATA}/instance/service-accounts/default/identity"
                try:
                    response = await self._client.get(
                        url,
                        params={"audience": self._audience, "format": "full"},
                        headers=HEADERS,
                    )
                    response.raise_for_status()
                except httpx2.HTTPError as exc:
                    raise MetadataError(f"no ID token: {type(exc).__name__}") from None
                self._token = response.text.strip()
                left = _seconds_left(self._token, self._wall())
                keep = (
                    MIN_CACHE_SECONDS
                    if left is None
                    else min(left, ID_TOKEN_SECONDS) - EARLY_SECONDS
                )
                self._expires = self._monotonic() + max(keep, MIN_CACHE_SECONDS)
            return self._token
