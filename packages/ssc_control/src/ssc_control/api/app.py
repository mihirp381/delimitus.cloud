"""Build the FastAPI application. ``create_app(settings)`` is the only constructor."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI, Request, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from ssc_contracts.errors import Problem
from ssc_control.api import problems
from ssc_control.api.auth import Verifier
from ssc_control.api.idempotency import REPLAYED_HEADER, Replay
from ssc_control.api.mcp.server import install_mcp
from ssc_control.api.problems import REQUEST_ID_HEADER, RequestIdMiddleware, request_id_of
from ssc_control.api.ratelimit import RateLimiter
from ssc_control.api.routes.blobs import blob_store_for, cell_stores_for, check_fs_allowed
from ssc_control.api.routes.blobs import router as blobs_router
from ssc_control.api.routes.internal import router as internal_router
from ssc_control.api.routes.v1 import router as v1_router
from ssc_control.api.runtime import Runtime, runtime_of
from ssc_control.api.settings import Settings
from ssc_control.db.engine import make_engine
from ssc_control.github.client import GitHubApp
from ssc_control.metrics import metrics_port
from ssc_control.runtime.cell_agent import MetadataIdTokens
from ssc_control.runtime.cells import CellPorts, CellRouter
from ssc_control.storage import CellStores
from ssc_control.timers.service import Timers
from ssc_shared import redaction
from ssc_shared.blobstore import BlobStore
from ssc_shared.blobstore_fs import FsBlobStore

_log = logging.getLogger(__name__)

API_TITLE: Final = "Small Software Cloud API"
API_VERSION: Final = "1"

DESCRIPTION: Final = """\
One API for the command line, the GitHub Action, the console and the agent interface.

* Every refusal is an RFC 9457 problem (`application/problem+json`) with a `code` from the error
  catalogue and a `request_id`. The text is fixed; evidence stays in our logs under that id.
* Every `POST` requires an `Idempotency-Key` header. A retry with the same key and body returns
  the first answer with `Idempotency-Replayed: true`.
* Edits to sharing rules require `If-Match` with the `ETag` from the last read.
* A deployment is a long-running operation: `202` with a `Location` to poll.
* Requests are rate-limited per credential; `429` carries `Retry-After`.
"""


async def _on_replay(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, Replay)
    return exc.reply.to_response(
        {REPLAYED_HEADER: "true", REQUEST_ID_HEADER: request_id_of(request)}
    )


def create_app(  # noqa: PLR0913  (the ports a test replaces, by keyword)
    settings: Settings,
    engine: AsyncEngine | None = None,
    blob_store: BlobStore | None = None,
    *,
    cells: CellPorts | None = None,
    github: GitHubApp | None = None,
    cell_stores: CellStores | None = None,
) -> FastAPI:
    """The API. ``cells`` None reaches each org's cell through ``settings.cells`` (none when
    that is empty), and ``cell_stores`` None its bucket through
    ``settings.cell_bucket_template``; tests pass their own."""
    store = blob_store if blob_store is not None else blob_store_for(settings)
    buckets = cell_stores if cell_stores is not None else cell_stores_for(settings)
    check_fs_allowed(store, settings)
    redaction.install()
    owned_github = github_for(settings) if github is None else None
    db = engine if engine is not None else make_engine(settings.database_dsn)
    owned_cells = cells_for(settings, db) if cells is None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        if settings.metrics_key is None:
            _log.warning("SSC_METRICS_KEY is not set: no metrics events will be recorded")
        async with agent_interface.session_manager.run():
            yield
        rt = app.state.runtime
        if owned_github is not None:
            await owned_github.aclose()
        if owned_cells is not None:
            await owned_cells.aclose()
        if isinstance(rt, Runtime) and rt.owns_engine:
            await rt.engine.dispose()

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
        openapi_tags=[
            {"name": "v1", "description": "For people and their tools."},
            {
                "name": "internal",
                "description": "For cell services and the directory sync. Workload or operator "
                "credentials, never a person's.",
            },
            {"name": "health", "description": "Unauthenticated liveness."},
        ],
    )
    app.state.runtime = rt = Runtime(
        settings=settings,
        engine=db,
        verifier=Verifier(dict(settings.jwks), settings.issuer),
        limiter=RateLimiter(settings.rate_capacity, settings.rate_refill_per_second),
        owns_engine=engine is None,
        metrics=metrics_port(settings.metrics_key),
        blob_store=store,
        cell_stores=buckets,
        timers=Timers(),
        cells=cells if cells is not None else owned_cells,
        github=github if github is not None else owned_github,
    )
    app.add_middleware(RequestIdMiddleware)
    problems.install(app)
    app.add_exception_handler(Replay, _on_replay)

    @app.get("/healthz", tags=["health"], response_model=dict[str, str])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(v1_router)
    app.include_router(internal_router)
    agent_interface = install_mcp(app, rt)
    if isinstance(store, FsBlobStore):
        app.include_router(blobs_router)
    _install_openapi(app)
    return app


def cells_for(settings: Settings, engine: AsyncEngine) -> CellRouter | None:
    """Each org's cell among ``settings.cells`` (``SSC_CELLS``), through its agent with this
    instance's ID tokens; ``None`` when there are none. No I/O."""
    if not settings.cells:
        return None
    return CellRouter(
        engine,
        settings.cells,
        apps_domain=settings.apps_domain,
        id_tokens=MetadataIdTokens(),
        grant_tokens=MetadataIdTokens(cache=False),
    )


def github_for(settings: Settings) -> GitHubApp | None:
    """The GitHub App when its id and key are configured, else ``None``. No I/O."""
    if not settings.github_app_id:
        return None
    return GitHubApp(
        app_id=settings.github_app_id,
        private_key=settings.github_private_key,
        base=settings.github_api_base,
    )


def _install_openapi(app: FastAPI) -> None:
    """Add the ``Problem`` component every refusal response refers to."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        spec = generate()
        schema = Problem.model_json_schema(ref_template="#/components/schemas/{model}")
        defs: dict[str, Any] = schema.pop("$defs", {})
        schemas: dict[str, Any] = spec.setdefault("components", {}).setdefault("schemas", {})
        schemas.update(defs)
        schemas["Problem"] = schema
        return spec

    app.openapi = openapi


__all__ = ["API_TITLE", "API_VERSION", "create_app", "runtime_of"]
