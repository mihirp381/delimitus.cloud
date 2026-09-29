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
from ssc_control.api.problems import REQUEST_ID_HEADER, RequestIdMiddleware, request_id_of
from ssc_control.api.ratelimit import RateLimiter
from ssc_control.api.routes.internal import router as internal_router
from ssc_control.api.routes.v1 import router as v1_router
from ssc_control.api.runtime import Runtime, runtime_of
from ssc_control.api.settings import Settings
from ssc_control.db.engine import make_engine
from ssc_control.metrics import metrics_port

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


def create_app(settings: Settings, engine: AsyncEngine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        if settings.metrics_key is None:
            _log.warning("SSC_METRICS_KEY is not set: no metrics events will be recorded")
        yield
        rt = app.state.runtime
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
            {"name": "internal", "description": "For cell services. Workload credentials only."},
            {"name": "health", "description": "Unauthenticated liveness."},
        ],
    )
    app.state.runtime = Runtime(
        settings=settings,
        engine=engine if engine is not None else make_engine(settings.database_dsn),
        verifier=Verifier(dict(settings.jwks), settings.issuer),
        limiter=RateLimiter(settings.rate_capacity, settings.rate_refill_per_second),
        owns_engine=engine is None,
        metrics=metrics_port(settings.metrics_key),
    )
    app.add_middleware(RequestIdMiddleware)
    problems.install(app)
    app.add_exception_handler(Replay, _on_replay)

    @app.get("/healthz", tags=["health"], response_model=dict[str, str])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(v1_router)
    app.include_router(internal_router)
    _install_openapi(app)
    return app


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
