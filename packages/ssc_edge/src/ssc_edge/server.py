"""The gateway's authorisation service: Envoy's HTTP ``ext_authz`` check, on loopback beside
Envoy in the gateway container (SSC-018, decision 023).

Envoy sends ``<path_prefix><original path and query>`` with the original method, ``Host`` and the
headers in ``envoy.ALLOWED_HEADERS``. A ``200`` lets the request through with the identity note,
the upstream host and Cloud Run's ``X-Serverless-Authorization``; any other answer goes to the
browser as it is. An exception is ``503`` here, and Envoy answers ``503`` when this service is
down or slow, so the gateway fails closed.
"""

import asyncio
import logging
import os
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Final, Protocol

import uvicorn
from fastapi import FastAPI, Request, Response

from ssc_contracts.identity import IdentityNote
from ssc_edge import pages
from ssc_edge.gate import Allow, Facts, Gate, GateConfig, Redeemer
from ssc_edge.identity_note import sign_note
from ssc_edge.keys import Keyring, KeyringError, kms_decrypt, parse_keyring
from ssc_edge.session import SessionCodec
from ssc_edge.tokens import MetadataTokens
from ssc_shared.access import AccessView, ViewHolder
from ssc_shared.blobstore_gcs import GcsBlobStore, bucket_of
from ssc_shared.snapshot_feed import SnapshotFeed

log = logging.getLogger(__name__)

AUTHZ_PREFIX: Final = "/authz"
LENGTH_HEADER: Final = "x-ssc-content-length"
"""The request's ``Content-Length``, copied by Envoy before the check (which carries no body)."""
SERVERLESS_AUTH: Final = "x-serverless-authorization"
DEFAULT_MAX_BODY: Final = 32 * 1024 * 1024
DEFAULT_MAX_STALE: Final = 300.0
DEV_ENVS: Final = frozenset({"dev", "test"})


class SettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    gate: GateConfig
    environment: str
    bucket: str
    max_stale: float
    keyring_plain: str | None
    keyring_cipher: str | None
    kms_key: str | None


def _need(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise SettingsError(f"{name} is required")
    return value


def settings_from_env(env: Mapping[str, str]) -> Settings:
    environment = env.get("SSC_ENV", "prod")
    label = _need(env, "SSC_CELL_LABEL")
    plain = env.get("SSC_GATEWAY_KEYRING_PLAIN") or None
    cipher = env.get("SSC_GATEWAY_KEYRING") or None
    kms_key = env.get("SSC_GATEWAY_KMS_KEY") or None
    if plain is not None and environment not in DEV_ENVS:
        raise SettingsError("SSC_GATEWAY_KEYRING_PLAIN is for SSC_ENV dev or test only")
    if plain is None and (cipher is None or kms_key is None):
        raise SettingsError("SSC_GATEWAY_KEYRING and SSC_GATEWAY_KMS_KEY are required")
    try:
        max_body = int(env.get("SSC_GATEWAY_MAX_BODY", DEFAULT_MAX_BODY))
        max_stale = float(env.get("SSC_SNAPSHOT_MAX_AGE", DEFAULT_MAX_STALE))
    except ValueError as exc:
        raise SettingsError("SSC_GATEWAY_MAX_BODY and SSC_SNAPSHOT_MAX_AGE are numbers") from exc
    gate = GateConfig(
        org_id=_need(env, "SSC_ORG_ID"),
        cell_label=label,
        apps_domain=env.get("SSC_APPS_DOMAIN", "delimitusapps.com"),
        auth_url=env.get("SSC_AUTH_URL", "https://auth.delimitus.com").rstrip("/"),
        issuer=env.get("SSC_IDENTITY_ISSUER", f"https://keys.delimitus.com/{label}"),
        project_number=_need(env, "SSC_PROJECT_NUMBER"),
        region=env.get("SSC_REGION", "us-central1"),
        max_body_bytes=max_body,
    )
    return Settings(
        gate=gate,
        environment=environment,
        bucket=_need(env, "SSC_CELL_BUCKET"),
        max_stale=max_stale,
        keyring_plain=plain,
        keyring_cipher=cipher,
        kms_key=kms_key,
    )


def gate_for(  # noqa: PLR0913  (keyword-only collaborators)
    config: GateConfig,
    keyring: Keyring,
    *,
    view: Callable[[], AccessView | None],
    clock: Callable[[], int] = lambda: int(time.time()),
    redeemer: Redeemer | None = None,
) -> Gate:
    def sign(note: IdentityNote) -> str:
        return sign_note(note, private_key=keyring.signing_key, kid=keyring.identity_kid)

    codec = SessionCodec(keyring.session, active=keyring.session_kid)
    return Gate(config, codec=codec, view=view, sign=sign, clock=clock, redeemer=redeemer)


def facts_of(request: Request) -> Facts:
    raw = bytes(request.scope.get("raw_path") or request.url.path.encode()).decode("latin-1")
    path = raw.removeprefix(AUTHZ_PREFIX) or "/"
    query = bytes(request.scope.get("query_string", b"")).decode("latin-1")
    headers = {k.lower(): v for k, v in request.headers.items()}
    headers.pop("content-length", None)
    length = headers.pop(LENGTH_HEADER, None)
    if length is not None:
        headers["content-length"] = length
    return Facts(
        method=request.method,
        host=headers.get("host", ""),
        path=f"{path}?{query}" if query else path,
        headers=headers,
    )


class IdTokens(Protocol):
    async def identity(self, audience: str) -> str: ...


def _unavailable() -> Response:
    return Response(pages.UNAVAILABLE, status_code=503, headers=dict(pages.HEADERS))


def create_app(
    gate: Callable[[], Gate | None],
    *,
    tokens: IdTokens | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """``gate`` returns None until start-up has loaded the keys. ``tokens`` mints the Google ID
    token for the app's service; None leaves ``X-Serverless-Authorization`` off (dev, tests)."""
    app = FastAPI(
        title="ssc-edge", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route(
        AUTHZ_PREFIX + "{rest:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def authz(request: Request) -> Response:
        current = gate()
        if current is None:
            return _unavailable()
        facts = facts_of(request)
        try:
            outcome = await current.check(facts)
            if isinstance(outcome, Allow):
                headers = dict(outcome.headers)
                if tokens is not None:
                    token = await tokens.identity(f"https://{outcome.upstream}")
                    headers[SERVERLESS_AUTH] = f"Bearer {token}"
                return Response(status_code=200, headers=headers)
        except Exception:
            log.exception("authz check failed for %s", facts.host)
            return _unavailable()
        log.info("gateway refused %s %s %s", outcome.status, outcome.reason, facts.host)
        response = Response(outcome.body, status_code=outcome.status)
        for name, value in outcome.headers:
            response.headers.append(name, value)
        return response

    return app


async def load_keyring(settings: Settings, tokens: MetadataTokens) -> Keyring:
    if settings.keyring_plain is not None:
        return parse_keyring(settings.keyring_plain.encode())
    if settings.keyring_cipher is None or settings.kms_key is None:
        raise KeyringError("no keyring configured")
    plain = await kms_decrypt(
        settings.kms_key, settings.keyring_cipher, access_token=await tokens.access()
    )
    return parse_keyring(plain)


def production_app(env: Mapping[str, str] | None = None) -> FastAPI:
    settings = settings_from_env(os.environ if env is None else env)
    tokens = MetadataTokens()
    holder = ViewHolder(settings.gate.org_id)
    feed = SnapshotFeed(GcsBlobStore(bucket_of(settings.bucket)), holder)
    state: dict[str, Gate] = {}

    def view() -> AccessView | None:
        return holder.view if feed.fresh(settings.max_stale) else None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        state["gate"] = gate_for(settings.gate, await load_keyring(settings, tokens), view=view)
        stop = asyncio.Event()
        task = asyncio.create_task(feed.run(stop))
        try:
            yield
        finally:
            stop.set()
            task.cancel()  # a poll can be mid-retry; Cloud Run allows 10 s after SIGTERM
            await asyncio.gather(task, return_exceptions=True)
            await tokens.aclose()

    dev = settings.environment in DEV_ENVS
    return create_app(lambda: state.get("gate"), tokens=None if dev else tokens, lifespan=lifespan)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("SSC_AUTHZ_PORT", "9001"))
    # No access log: a callback's query string carries a one-time login code.
    uvicorn.run(production_app(), host="127.0.0.1", port=port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
