"""``python -m ssc_agent.intake``: the cell's secret intake (SSC-026, decision 022).

The one place a secret value enters SSC. ``ssc secret set`` asks the control plane for a write
grant (``ssc_shared.secret_grants``), then PUTs the value here over TLS, and the intake adds it
to Secret Manager as a new version. The value never reaches the control plane, its database,
a job or a log: this service holds it only for the length of the request, and its identity may
add secret versions to ``ssc-a-*`` and do nothing else, not even read one back.

``PUT /v1/secrets/<secret>?grant=<nonce>`` with ``Authorization: Bearer <grant>`` and the raw
value as the body answers 201 ``{"secret", "version"}``. Errors are ``{"code", "message"}``: 400
``INVALID_REQUEST``, 401 ``UNAUTHENTICATED``, 403 ``GRANT_REFUSED``, 413 ``VALUE_TOO_LARGE``, 502
``SECRETS_ERROR``. No error says why a grant was refused.

Configuration, all required and set by the cell stack: ``SSC_CELL_PROJECT``,
``SSC_INTAKE_ORIGIN`` (this service's public origin, the audience every grant names) and
``SSC_CONTROL_SA`` (the only account whose grants count). Exits 2 when one is missing.
"""

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, Final, Protocol, cast

import httpx2
import jwt
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jwt import PyJWKSet

from ssc_agent.metadata import MetadataAccessTokens
from ssc_agent.secret_manager import CellSecretWriter, SecretsError, SecretWriter, service_of
from ssc_shared import redaction
from ssc_shared.secret_grants import GRANT_SECONDS, INTAKE_PATH, MAX_VALUE_BYTES, upload_url

log = logging.getLogger(__name__)

GOOGLE_CERTS: Final = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS: Final = ("https://accounts.google.com", "accounts.google.com")
CERTS_SECONDS: Final = 3600
ENV: Final = {
    "project": "SSC_CELL_PROJECT",
    "origin": "SSC_INTAKE_ORIGIN",
    "control_account": "SSC_CONTROL_SA",
}


class GrantCheck(Protocol):
    async def allows(self, bearer: str, audience: str) -> bool:
        """Whether ``bearer`` is a live grant for exactly ``audience``."""
        ...


class GoogleGrants(GrantCheck):
    """Grants are Google ID tokens of ``control_account``, minted at most ``GRANT_SECONDS`` ago."""

    def __init__(
        self,
        control_account: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._account = control_account
        self._http = httpx2.AsyncClient(timeout=5, transport=transport)
        self._clock = clock
        self._wall = wall
        self._keys: PyJWKSet | None = None
        self._fetched = 0.0
        self._lock = asyncio.Lock()

    async def _certs(self, *, force: bool = False) -> PyJWKSet:
        async with self._lock:
            stale = self._clock() - self._fetched > CERTS_SECONDS
            if self._keys is None or stale or force:
                r = await self._http.get(GOOGLE_CERTS)
                r.raise_for_status()
                self._keys = PyJWKSet.from_dict(cast("dict[str, Any]", r.json()))
                self._fetched = self._clock()
            return self._keys

    async def allows(self, bearer: str, audience: str) -> bool:
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
                audience=audience,
                options={"require": ["iss", "aud", "exp", "iat", "email"]},
                leeway=5,
            )
        except jwt.PyJWTError, StopIteration, httpx2.HTTPError, ValueError:
            return False
        fresh = 0 <= self._wall() - float(claims["iat"]) <= GRANT_SECONDS
        return (
            claims.get("iss") in GOOGLE_ISSUERS
            and claims.get("email_verified") is True
            and claims.get("email") == self._account
            and fresh
        )

    async def aclose(self) -> None:
        await self._http.aclose()


def create_intake(writer: SecretWriter, grants: GrantCheck, origin: str) -> FastAPI:
    app = FastAPI(title="ssc-secret-intake", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.put(INTAKE_PATH + "/{secret}")
    async def put(  # noqa: PLR0911  (one return per refusal)
        secret: str, request: Request
    ) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        try:
            service_of(secret)
            audience = upload_url(origin, secret, request.query_params.get("grant", ""))
        except ValueError as exc:
            return _error(400, "INVALID_REQUEST", str(exc))
        scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not bearer:
            return _error(401, "UNAUTHENTICATED", "send the grant as a bearer token")
        if not await grants.allows(bearer, audience):
            log.warning("grant refused", extra={"secret": secret})
            return _error(403, "GRANT_REFUSED", "the grant is not valid for this secret now")
        value = bytearray()
        async for chunk in request.stream():
            value.extend(chunk)
            if len(value) > MAX_VALUE_BYTES:
                return _error(413, "VALUE_TOO_LARGE", f"at most {MAX_VALUE_BYTES} bytes")
        if not value:
            return _error(400, "INVALID_REQUEST", "the value is empty")
        try:
            version = await writer.add_version(secret, bytes(value))
        except SecretsError as exc:
            log.warning("add version failed", extra={"secret": secret, "error": str(exc)})
            return _error(502, "SECRETS_ERROR", str(exc))
        log.info("secret version added", extra={"secret": secret, "version": version})
        return JSONResponse({"secret": secret, "version": version}, status_code=201)

    return app


def config_from_env(env: Mapping[str, str]) -> dict[str, str]:
    """The intake's settings; ``ValueError`` naming whatever is missing or malformed."""
    missing = [name for name in ENV.values() if not env.get(name)]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    config = {field: env[name] for field, name in ENV.items()}
    if not config["origin"].startswith("https://"):
        raise ValueError(f"{ENV['origin']} must be an https origin")
    return config


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": redaction.redact(message)}, status_code=status)


def main() -> int:
    try:
        config = config_from_env(os.environ)
    except ValueError as exc:
        print(f"ssc-secret-intake: {exc}", file=sys.stderr)  # noqa: T201
        return 2
    logging.basicConfig(level=logging.INFO)
    redaction.install()
    writer = CellSecretWriter(config["project"], MetadataAccessTokens())
    app = create_intake(writer, GoogleGrants(config["control_account"]), config["origin"])
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # noqa: S104
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
