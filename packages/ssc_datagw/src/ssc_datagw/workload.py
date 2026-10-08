"""Which app environment is calling: the app's own Google ID token, checked here (SSC-050).

An app's Cloud Run service runs as ``ssc-a-<20>@<cell project>.iam.gserviceaccount.com`` and asks
its metadata server for an ID token with the data gateway's URL as audience. Cloud Run IAM is
not what admits it: any service account anywhere can mint a token naming this URL, so the
gateway checks the signature against Google's keys, the issuer, the audience, a verified email
and that the email is an app account of this cell's project. The email names the environment.

The cell agent's own account (``SSC_DATAGW_AGENT_ACCOUNT``, GA-5.8) is admitted on the schema
route alone, where it names the environment it describes for in ``X-SSC-Environment`` and is
then admitted as that environment's app; anywhere else it is refused as any other account is.
"""

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, cast

import httpx2
import jwt
from jwt import PyJWK, PyJWKSet

GOOGLE_CERTS: Final = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS: Final = frozenset({"https://accounts.google.com", "accounts.google.com"})
CERTS_SECONDS: Final = 3600.0
REFETCH_SECONDS: Final = 30.0
"""The least time between two fetches of Google's keys for an unknown ``kid``, so forged
tokens with made-up key ids cannot turn the gateway into a fetch loop."""
LEEWAY_SECONDS: Final = 5
ALGORITHM: Final = "RS256"
ENVIRONMENT: Final = re.compile(r"env_[a-z0-9]{20}")
"""An ``X-SSC-Environment`` value: the id of the environment the cell agent describes for."""


class WorkloadRefusedError(Exception):
    """The token is missing or does not prove an app of this cell. The message is for logs."""


class WorkloadKeysUnavailableError(Exception):
    """Google's keys could not be fetched, so nothing can be verified."""


@dataclass(frozen=True, slots=True)
class Workload:
    env_id: str
    account: str
    agent: bool = False
    """The cell agent, admitted for ``env_id`` by its ``X-SSC-Environment`` header."""


class GoogleWorkloads:
    """One per process. ``transport`` and ``certs_url`` are for tests."""

    def __init__(  # noqa: PLR0913  (keyword-only collaborators)
        self,
        *,
        audience: str,
        project_id: str,
        agent_account: str | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        certs_url: str = GOOGLE_CERTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._audience = audience
        self._agent_account = agent_account
        self._account = re.compile(
            r"ssc-a-([a-z0-9]{20})@" + re.escape(project_id) + r"\.iam\.gserviceaccount\.com"
        )
        self._http = httpx2.AsyncClient(timeout=5, transport=transport)
        self._certs_url = certs_url
        self._clock = clock
        self._keys: PyJWKSet | None = None
        self._fetched = 0.0
        self._lock = asyncio.Lock()

    async def _certs(self, *, unknown: bool) -> PyJWKSet:
        async with self._lock:
            keys = self._keys
            age = self._clock() - self._fetched
            if keys is None or age > CERTS_SECONDS or (unknown and age > REFETCH_SECONDS):
                try:
                    r = await self._http.get(self._certs_url)
                    r.raise_for_status()
                    keys = PyJWKSet.from_dict(cast("dict[str, Any]", r.json()))
                    self._keys = keys
                except (httpx2.HTTPError, ValueError, jwt.PyJWKSetError) as exc:
                    if keys is None:
                        raise WorkloadKeysUnavailableError(str(exc)) from exc
                self._fetched = self._clock()
            return keys

    async def _key(self, kid: str) -> PyJWK:
        keys = await self._certs(unknown=False)
        found = next((k for k in keys.keys if k.key_id == kid), None)
        if found is None:
            keys = await self._certs(unknown=True)
            found = next((k for k in keys.keys if k.key_id == kid), None)
        if found is None:
            raise WorkloadRefusedError(f"no Google key {kid!r}")
        return found

    async def verify(
        self, authorization: str | None, *, agent: bool = False, environment: str | None = None
    ) -> Workload:
        """The calling environment, or :class:`WorkloadRefusedError`. With ``agent`` (the
        schema route) the cell agent's account is admitted too, for the environment
        ``environment`` (its ``X-SSC-Environment`` header) names."""
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise WorkloadRefusedError("no bearer token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise WorkloadRefusedError(f"malformed: {exc}") from exc
        kid = header.get("kid")
        if header.get("alg") != ALGORITHM or not isinstance(kid, str) or not kid:
            raise WorkloadRefusedError(f"alg {header.get('alg')!r}, kid {kid!r}")
        key = await self._key(kid)
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                options={"require": ["iss", "aud", "sub", "exp", "iat", "email"]},
                leeway=LEEWAY_SECONDS,
            )
        except jwt.PyJWTError as exc:
            raise WorkloadRefusedError(f"{type(exc).__name__}: {exc}") from exc
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise WorkloadRefusedError(f"issuer {claims.get('iss')!r}")
        if claims.get("email_verified") is not True:
            raise WorkloadRefusedError("email not verified")
        email = claims.get("email")
        if agent and self._agent_account is not None and email == self._agent_account:
            if not environment:
                raise WorkloadRefusedError("the cell agent sent no X-SSC-Environment")
            if not ENVIRONMENT.fullmatch(environment):
                raise WorkloadRefusedError("the cell agent's X-SSC-Environment is malformed")
            return Workload(env_id=environment, account=self._agent_account, agent=True)
        match = self._account.fullmatch(email) if isinstance(email, str) else None
        if match is None:
            raise WorkloadRefusedError(f"{email!r} is not an app of this cell")
        return Workload(env_id="env_" + match.group(1), account=match.group(0))

    async def aclose(self) -> None:
        await self._http.aclose()
