"""``RuntimeDriver`` through a cell's agent (SSC-017, decisions 014 and 022).

The control plane holds no Cloud Run role in a cell; it may only invoke the cell agent, which
runs ``ssc_agent.cloud_run.CloudRunDriver``. Each call is one POST with a Google ID token for
the agent's URL. ``MetadataIdTokens`` gets it on Cloud Run; ``ImpersonatedIdTokens`` lets an
operator or the nightly probe run act as the control plane's service account.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final, cast

import httpx2

from ssc_shared.runtime import (
    RevisionNotFoundError,
    RuntimeDriver,
    RuntimeDriverError,
    ServiceNotFoundError,
    ServiceObservation,
    ServiceSpec,
    observation_from_wire,
    spec_to_wire,
)

type IdTokens = Callable[[str], Awaitable[str]]
type AccessTokens = Callable[[], Awaitable[str]]

METADATA: Final = "http://metadata.google.internal/computeMetadata/v1"
IAM_CREDENTIALS: Final = "https://iamcredentials.googleapis.com/v1"
TOKEN_SECONDS: Final = 3000  # Google ID tokens last an hour; refresh well before
CALL_TIMEOUT_SECONDS: Final = 240.0  # set_traffic waits for Cloud Run to finish
_ERRORS: Final[dict[str, type[RuntimeDriverError]]] = {
    "SERVICE_NOT_FOUND": ServiceNotFoundError,
    "REVISION_NOT_FOUND": RevisionNotFoundError,
}


class CellAgentDriver(RuntimeDriver):
    def __init__(
        self, agent_url: str, id_tokens: IdTokens, *, client: httpx2.AsyncClient | None = None
    ) -> None:
        self._url = agent_url.rstrip("/")
        self._id_tokens = id_tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def apply(self, spec: ServiceSpec) -> str:
        body = await self._call("apply", {"spec": spec_to_wire(spec)})
        revision = body.get("revision")
        if not isinstance(revision, str):
            raise RuntimeDriverError("cell agent: apply returned no revision")
        return revision

    async def set_traffic(self, service: str, revision: str) -> None:
        await self._call("set_traffic", {"service": service, "revision": revision})

    async def scale_to_zero(self, service: str) -> None:
        await self._call("scale_to_zero", {"service": service})

    async def observe(self, service: str) -> ServiceObservation | None:
        body = await self._call("observe", {"service": service})
        seen = body.get("observation")
        if seen is None:
            return None
        try:
            return observation_from_wire(cast("dict[str, Any]", seen))
        except ValueError as exc:
            raise RuntimeDriverError(f"cell agent: {exc}") from None

    async def _call(self, method: str, body: dict[str, object]) -> dict[str, Any]:
        token = await self._id_tokens(self._url)
        try:
            response = await self._client.post(
                f"{self._url}/v1/runtime/{method}",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx2.HTTPError as exc:
            raise RuntimeDriverError(f"cell agent {method}: {type(exc).__name__}") from None
        try:
            payload: object = response.json()
        except ValueError:
            payload = None
        result = cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}
        if response.is_success:
            return result
        code = str(result.get("code") or "")
        message = str(result.get("message") or response.reason_phrase)
        error = _ERRORS.get(code, RuntimeDriverError)
        raise error(f"cell agent {method}: HTTP {response.status_code} {code} {message}".strip())


class MetadataIdTokens:
    """ID tokens for this instance's identity, one cache entry per audience."""

    def __init__(self, client: httpx2.AsyncClient | None = None) -> None:
        self._client = client or httpx2.AsyncClient(timeout=5.0)
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def __call__(self, audience: str) -> str:
        async with self._lock:
            cached = self._cache.get(audience)
            if cached and time.monotonic() < cached[1]:
                return cached[0]
            try:
                response = await self._client.get(
                    f"{METADATA}/instance/service-accounts/default/identity",
                    params={"audience": audience, "format": "full"},
                    headers={"Metadata-Flavor": "Google"},
                )
                response.raise_for_status()
            except httpx2.HTTPError as exc:
                raise RuntimeDriverError(f"no ID token: {type(exc).__name__}") from None
            token = response.text.strip()
            self._cache[audience] = (token, time.monotonic() + TOKEN_SECONDS)
            return token


class ImpersonatedIdTokens:
    """ID tokens for ``service_account``, minted by the IAM Credentials API with the caller's
    own access token (which needs ``iam.serviceAccounts.getOpenIdToken`` on it)."""

    def __init__(
        self,
        service_account: str,
        access_tokens: AccessTokens,
        *,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._sa = service_account
        self._access_tokens = access_tokens
        self._client = client or httpx2.AsyncClient(timeout=10.0)
        self._cache: dict[str, tuple[str, float]] = {}

    async def __call__(self, audience: str) -> str:
        cached = self._cache.get(audience)
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        url = f"{IAM_CREDENTIALS}/projects/-/serviceAccounts/{self._sa}:generateIdToken"
        try:
            response = await self._client.post(
                url,
                json={"audience": audience, "includeEmail": True},
                headers={"Authorization": f"Bearer {await self._access_tokens()}"},
            )
            response.raise_for_status()
            token = str(response.json()["token"])
        except (httpx2.HTTPError, ValueError, KeyError) as exc:
            raise RuntimeDriverError(f"no ID token for {self._sa}: {type(exc).__name__}") from None
        self._cache[audience] = (token, time.monotonic() + TOKEN_SECONDS)
        return token


__all__ = [
    "AccessTokens",
    "CellAgentDriver",
    "IdTokens",
    "ImpersonatedIdTokens",
    "MetadataIdTokens",
]
