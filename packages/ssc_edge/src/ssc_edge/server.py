"""The gateway's authorisation service: Envoy's HTTP ``ext_authz`` check, on loopback beside
Envoy in the gateway container (SSC-018, decision 023).

Envoy sends ``<path_prefix><original path and query>`` with the original method, ``Host`` and the
headers in ``envoy.ALLOWED_HEADERS``. A ``200`` lets the request through with the identity note,
the upstream host and Cloud Run's ``X-Serverless-Authorization``; any other answer goes to the
browser as it is. An exception is ``503`` here, and Envoy answers ``503`` when this service is
down or slow, so the gateway fails closed.

The gateway runs request-billed from zero (decision 023 amendment), so nothing runs between
requests: the snapshot is read once before the first request is accepted and then on demand by
the checks themselves (``OnDemandView``) and, while a stream is open, by the stream watch
(``ssc_edge.streams``). There is no background poll.
"""

import asyncio
import logging
import os
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Final, Protocol

import httpx2
import jwt
import uvicorn
from fastapi import FastAPI, Request, Response

from ssc_contracts.identity import IdentityNote
from ssc_edge import pages
from ssc_edge.gate import (
    STREAM_HEADER,
    Allow,
    Facts,
    Gate,
    GateConfig,
    Redeemer,
    new_nonce,
    streaming,
)
from ssc_edge.identity_note import sign_note
from ssc_edge.keys import Keyring, KeyringError, check_published, kms_decrypt, parse_keyring
from ssc_edge.redeemer import HttpRedeemer
from ssc_edge.schedule_token import ScheduleKeys, ScheduleKeysError, parse_timer_jwks
from ssc_edge.session import SessionCodec
from ssc_edge.streams import Streams
from ssc_edge.tokens import MetadataTokens
from ssc_shared.access import AccessView, ViewHolder
from ssc_shared.blobstore import BlobStore
from ssc_shared.blobstore_gcs import GcsBlobStore, bucket_of
from ssc_shared.snapshot_feed import POLL_SECONDS, SnapshotFeed

log = logging.getLogger(__name__)

AUTHZ_PREFIX: Final = "/authz"
LENGTH_HEADER: Final = "x-ssc-content-length"
"""The request's ``Content-Length``, copied by Envoy before the check (which carries no body)."""
SERVERLESS_AUTH: Final = "x-serverless-authorization"
DEFAULT_MAX_BODY: Final = 32 * 1024 * 1024
DEFAULT_MAX_STALE: Final = 300.0
RECHECK_SECONDS: Final = POLL_SECONDS
FRESH_WAIT: Final = 0.3
SETTLED_SECONDS: Final = 3.0
STALE_WAIT: Final = 4.0
FIRST_READ_WAIT: Final = 10.0
DEV_ENVS: Final = frozenset({"dev", "test"})
STREAM_PORT: Final = 9002


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
    dev_cell_secret: str | None = None
    """Dev and test only: redeem login codes with the rig's shared secret, not an ID token."""
    published_jwks: str | None = None
    """The JWKS the cell hands to apps; the gateway refuses to start with other identity keys."""
    stream_port: int = STREAM_PORT
    timer_keys: jwt.PyJWKSet | None = None
    """The control plane's public timer keys (``SSC_TIMER_JWKS``); unset refuses every timer
    call."""


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
    dev_secret = env.get("SSC_AUTH_DEV_CELL_SECRET") or None
    if dev_secret is not None and environment not in DEV_ENVS:
        raise SettingsError("SSC_AUTH_DEV_CELL_SECRET is for SSC_ENV dev or test only")
    if plain is None and (cipher is None or kms_key is None):
        raise SettingsError("SSC_GATEWAY_KEYRING and SSC_GATEWAY_KMS_KEY are required")
    try:
        max_body = int(env.get("SSC_GATEWAY_MAX_BODY", DEFAULT_MAX_BODY))
        max_stale = float(env.get("SSC_SNAPSHOT_MAX_AGE", DEFAULT_MAX_STALE))
        stream_port = int(env.get("SSC_STREAM_PORT", STREAM_PORT))
    except ValueError as exc:
        raise SettingsError(
            "SSC_GATEWAY_MAX_BODY, SSC_SNAPSHOT_MAX_AGE and SSC_STREAM_PORT are numbers"
        ) from exc
    timer_jwks = env.get("SSC_TIMER_JWKS") or None
    try:
        timer_keys = None if timer_jwks is None else parse_timer_jwks(timer_jwks)
    except ScheduleKeysError as exc:
        raise SettingsError(str(exc)) from exc
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
        dev_cell_secret=dev_secret,
        published_jwks=env.get("SSC_IDENTITY_JWKS") or None,
        stream_port=stream_port,
        timer_keys=timer_keys,
    )


def redeemer_for(
    settings: Settings, tokens: IdTokens, transport: httpx2.AsyncBaseTransport | None = None
) -> HttpRedeemer:
    auth_url, secret = settings.gate.auth_url, settings.dev_cell_secret

    async def bearer() -> str:
        if secret is not None:
            return f"dev.{secret}"
        return await tokens.identity(auth_url)

    return HttpRedeemer(
        auth_url=auth_url, org_id=settings.gate.org_id, bearer=bearer, transport=transport
    )


def gate_for(  # noqa: PLR0913  (keyword-only collaborators)
    config: GateConfig,
    keyring: Keyring,
    *,
    view: Callable[[], AccessView | None],
    clock: Callable[[], int] = lambda: int(time.time()),
    redeemer: Redeemer | None = None,
    nonce: Callable[[], str] = new_nonce,
    refresh: Callable[[], Awaitable[None]] | None = None,
    timer_keys: jwt.PyJWKSet | None = None,
) -> Gate:
    """The gate over ``keyring``; ``timer_keys`` admit timer calls (``ScheduleKeys``, one per
    gate, on the gate's clock)."""

    def sign(note: IdentityNote) -> str:
        return sign_note(note, private_key=keyring.signing_key, kid=keyring.identity_kid)

    codec = SessionCodec(keyring.session, active=keyring.session_kid)
    return Gate(
        config,
        codec=codec,
        view=view,
        sign=sign,
        clock=clock,
        redeemer=redeemer,
        nonce=nonce,
        refresh=refresh,
        schedule_keys=None if timer_keys is None else ScheduleKeys(timer_keys, clock=clock),
    )


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


class OnDemandView:
    """The snapshot as a check needs it. A check that gets as far as the snapshot re-reads
    ``latest.json`` when no read has confirmed the view for ``RECHECK_SECONDS``, one read at a
    time. It waits for that read up to ``FRESH_WAIT`` when a read confirmed the view within
    ``SETTLED_SECONDS`` and up to ``STALE_WAIT`` otherwise, so a check after idle decides on the
    new snapshot; a read still running finishes for the next check. A read confirms the view as
    of the moment it asked for ``latest.json``, so while the bucket answers within
    ``STALE_WAIT`` no check decides on a view older than ``SETTLED_SECONDS + FRESH_WAIT``."""

    def __init__(self, feed: SnapshotFeed, holder: ViewHolder, *, max_stale: float) -> None:
        self._feed = feed
        self._holder = holder
        self._max_stale = max_stale
        self._read: asyncio.Task[None] | None = None

    async def _poll(self) -> None:
        try:
            await self._feed.poll_once()
        except Exception:
            log.exception("snapshot read failed")

    async def first_read(self) -> bool:
        """The read before the first request, waited for up to ``FIRST_READ_WAIT``; False leaves
        every check at ``503`` until a later read succeeds."""
        self._read = asyncio.create_task(self._poll())
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._read), FIRST_READ_WAIT)
        if self.view() is None:
            log.warning("no snapshot at start: every request is 503 until one is read")
            return False
        return True

    async def refresh(self) -> None:
        if self._feed.fresh(RECHECK_SECONDS):
            return
        if self._read is None or self._read.done():
            self._read = asyncio.create_task(self._poll())
        wait = FRESH_WAIT if self._feed.fresh(SETTLED_SECONDS) else STALE_WAIT
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._read), wait)

    def view(self) -> AccessView | None:
        return self._holder.view if self._feed.fresh(self._max_stale) else None

    async def aclose(self) -> None:
        if self._read is not None:
            self._read.cancel()
            await asyncio.gather(self._read, return_exceptions=True)


def create_app(
    gate: Callable[[], Gate | None],
    *,
    tokens: IdTokens | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    streams: Streams | None = None,
) -> FastAPI:
    """``gate`` returns None until start-up has loaded the keys. ``tokens`` mints the Google ID
    token for the app's service; None leaves ``X-Serverless-Authorization`` off (dev, tests).
    ``streams`` admits an allowed WebSocket or event stream to the stream relay."""
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
                if streams is not None and streaming(facts):
                    headers[STREAM_HEADER] = streams.admit(outcome)
                allowed = Response(status_code=200, headers=headers)
                for name, value in outcome.client_headers:
                    allowed.headers.append(name, value)
                return allowed
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
        keyring = parse_keyring(settings.keyring_plain.encode())
    elif settings.keyring_cipher is None or settings.kms_key is None:
        raise KeyringError("no keyring configured")
    else:
        plain = await kms_decrypt(
            settings.kms_key, settings.keyring_cipher, access_token=await tokens.access()
        )
        keyring = parse_keyring(plain)
    if settings.published_jwks is not None:
        check_published(keyring, settings.published_jwks)
    return keyring


def production_app(
    env: Mapping[str, str] | None = None, *, store: BlobStore | None = None
) -> FastAPI:
    """``store`` replaces the cell bucket (tests)."""
    settings = settings_from_env(os.environ if env is None else env)
    tokens = MetadataTokens()
    holder = ViewHolder(settings.gate.org_id)
    feed = SnapshotFeed(store or GcsBlobStore(bucket_of(settings.bucket)), holder)
    snapshot = OnDemandView(feed, holder, max_stale=settings.max_stale)
    state: dict[str, Gate] = {}
    redeemer = redeemer_for(settings, tokens)
    streams = Streams(lambda: state.get("gate"), refresh=snapshot.refresh)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        keyring = await load_keyring(settings, tokens)
        await snapshot.first_read()
        state["gate"] = gate_for(
            settings.gate,
            keyring,
            view=snapshot.view,
            redeemer=redeemer,
            refresh=snapshot.refresh,
            timer_keys=settings.timer_keys,
        )
        relay = await streams.serve("127.0.0.1", settings.stream_port)
        try:
            yield
        finally:
            relay.close()
            await streams.aclose()
            await relay.wait_closed()
            await snapshot.aclose()
            await redeemer.aclose()
            await tokens.aclose()

    dev = settings.environment in DEV_ENVS
    return create_app(
        lambda: state.get("gate"),
        tokens=None if dev else tokens,
        lifespan=lifespan,
        streams=streams,
    )


def main() -> None:
    """Serve until SIGTERM, with no access log: a callback's query string carries a one-time
    login code. Once the server and its lifespan have finished, the process ends without joining
    a worker thread still in a blocking bucket read, which the storage client retries for up to
    two minutes when the bucket cannot be reached; ``uvicorn.run`` would wait for it, past the
    supervisor's grace, so the loop is our own."""
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("SSC_AUTHZ_PORT", "9001"))
    config = uvicorn.Config(
        production_app(), host="127.0.0.1", port=port, log_config=None, access_log=False
    )
    asyncio.new_event_loop().run_until_complete(uvicorn.Server(config).serve())
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()
