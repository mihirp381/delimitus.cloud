"""Which cell is calling the auth host's ``/internal/redeem`` (SSC-019, decision 024).

Production: the gateway presents its service account's Google ID token (``MetadataTokens``), with
the auth host's origin as audience. It is checked against Google's keys; the account must be
``ssc-gateway@<project>.iam.gserviceaccount.com`` and ``<project>`` must be the org's
``cell_project``, so one cell's gateway can never redeem another org's codes. No new secret.

Dev and test only: ``Bearer dev.<secret>`` with the rig's shared secret, refused in production.
"""

import asyncio
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

import httpx2
import jwt
from jwt import PyJWKSet

GOOGLE_CERTS: Final = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS: Final = ("https://accounts.google.com", "accounts.google.com")
GATEWAY_ACCOUNT: Final = "ssc-gateway"
SA_DOMAIN: Final = ".iam.gserviceaccount.com"
CERTS_SECONDS: Final = 3600
DEV_PREFIX: Final = "dev."


@dataclass(frozen=True, slots=True)
class CellCaller:
    """``project`` is the caller's GCP project; None for the dev rig (any org)."""

    project: str | None


class CallerCheck(Protocol):
    async def caller(self, bearer: str) -> CellCaller | None: ...


def gateway_project(email: str) -> str | None:
    local, at, domain = email.partition("@")
    if not at or local != GATEWAY_ACCOUNT or not domain.endswith(SA_DOMAIN):
        return None
    project = domain.removesuffix(SA_DOMAIN)
    return project or None


class GoogleCallers:
    def __init__(
        self,
        audience: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._audience = audience
        self._http = httpx2.AsyncClient(timeout=5, transport=transport)
        self._clock = clock
        self._keys: PyJWKSet | None = None
        self._fetched = 0.0
        self._lock = asyncio.Lock()

    async def _certs(self, *, force: bool = False) -> PyJWKSet:
        async with self._lock:
            stale = self._clock() - self._fetched > CERTS_SECONDS
            if self._keys is None or stale or force:
                r = await self._http.get(GOOGLE_CERTS)
                r.raise_for_status()
                self._keys = PyJWKSet.from_dict(cast(dict[str, Any], r.json()))
                self._fetched = self._clock()
            return self._keys

    async def caller(self, bearer: str) -> CellCaller | None:
        try:
            kid = jwt.get_unverified_header(bearer).get("kid")
            keys = await self._certs()
            if not any(k.key_id == kid for k in keys.keys):
                keys = await self._certs(force=True)
            key = next(k for k in keys.keys if k.key_id == kid)
            claims = jwt.decode(
                bearer,
                key.key,
                algorithms=["RS256"],
                audience=self._audience,
                options={"require": ["iss", "aud", "exp", "iat", "email"]},
                leeway=5,
            )
        except jwt.PyJWTError, StopIteration, httpx2.HTTPError, ValueError:
            return None
        if claims.get("iss") not in GOOGLE_ISSUERS or claims.get("email_verified") is not True:
            return None
        project = gateway_project(str(claims.get("email", "")))
        return None if project is None else CellCaller(project)

    async def aclose(self) -> None:
        await self._http.aclose()


class DevCallers:
    """The local rig's shared secret. Never constructed outside dev and test."""

    def __init__(self, secret: str) -> None:
        if len(secret) < 32:  # noqa: PLR2004
            raise ValueError("the dev cell secret must be at least 32 characters")
        self._secret = secret

    async def caller(self, bearer: str) -> CellCaller | None:
        given = bearer.removeprefix(DEV_PREFIX) if bearer.startswith(DEV_PREFIX) else ""
        if given and hmac.compare_digest(given.encode(), self._secret.encode()):
            return CellCaller(None)
        return None
