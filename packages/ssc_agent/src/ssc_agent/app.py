"""The cell agent's HTTP surface: the ``RuntimeDriver`` protocol, the ``CellBuilder``
protocol (SSC-015), ``SecretCustody`` (SSC-026) and app databases (SSC-040), one POST per method.

Cloud Run lets only the control plane's service account invoke the agent (decision 022), so
every request here already passed IAM. The agent still refuses any service or secret name that
is not an SSC app's, because its own IAM cannot limit a create by name. Secrets have one method,
``ensure``; nothing here reads, returns or receives a secret value. App databases have
``ensure``, ``rotate`` and ``usage``; their passwords stay in the agent and the cell's Secret
Manager, and the answers carry secret versions only.

Errors are ``{"code", "message"}``: 404 ``SERVICE_NOT_FOUND``, ``REVISION_NOT_FOUND``,
``BUILD_NOT_FOUND`` or ``DATABASE_NOT_FOUND``, 400 ``INVALID_REQUEST``, 409 ``DB_TIER_FULL``,
502 ``RUNTIME_ERROR``, ``BUILD_ERROR``, ``SECRETS_ERROR`` or ``DATABASE_ERROR``, 503
``BUILD_NOT_CONFIGURED``, ``SECRETS_NOT_CONFIGURED`` or ``DATABASES_NOT_CONFIGURED`` when the
agent runs without a builder, secret custody or a Cloud SQL instance.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Final, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ssc_agent.app_database import (
    AdminSqlError,
    AppDatabase,
    AppDatabaseError,
    CellAppDatabases,
    DatabaseMissingError,
    TierFullError,
)
from ssc_agent.secret_manager import SecretCustody, SecretsError
from ssc_shared.build import (
    BuildDriverError,
    BuildNotFoundError,
    CellBuilder,
    build_from_wire,
    status_to_wire,
)
from ssc_shared.redaction import redact
from ssc_shared.runtime import (
    SERVICE_NAME,
    RevisionNotFoundError,
    RuntimeDriver,
    RuntimeDriverError,
    ServiceNotFoundError,
    observation_to_wire,
    spec_from_wire,
)

log = logging.getLogger(__name__)

PREFIX: Final = "/v1/runtime"
BUILD_PREFIX: Final = "/v1/build"
SECRETS_PREFIX: Final = "/v1/secrets"
DATABASES_PREFIX: Final = "/v1/databases"

type Handler = Callable[[dict[str, Any]], Awaitable[dict[str, object]]]


def create_app(
    driver: RuntimeDriver,
    builder: CellBuilder | None = None,
    secrets: SecretCustody | None = None,
    databases: CellAppDatabases | None = None,
) -> FastAPI:
    app = FastAPI(title="ssc-cell-agent", docs_url=None, redoc_url=None, openapi_url=None)

    async def apply(body: dict[str, Any]) -> dict[str, object]:
        return {"revision": await driver.apply(spec_from_wire(body["spec"]))}

    async def set_traffic(body: dict[str, Any]) -> dict[str, object]:
        await driver.set_traffic(_service(body), _str(body, "revision"))
        return {}

    async def scale_to_zero(body: dict[str, Any]) -> dict[str, object]:
        await driver.scale_to_zero(_service(body))
        return {}

    async def observe(body: dict[str, Any]) -> dict[str, object]:
        seen = await driver.observe(_service(body))
        return {"observation": None if seen is None else observation_to_wire(seen)}

    handlers: dict[str, Handler] = {
        "apply": apply,
        "set_traffic": set_traffic,
        "scale_to_zero": scale_to_zero,
        "observe": observe,
    }

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.post(PREFIX + "/{method}")
    async def call(method: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        handler = handlers.get(method)
        if handler is None:
            return _error(404, "NOT_FOUND", f"no method {method}")
        try:
            body: object = await request.json()
            if not isinstance(body, dict):
                raise TypeError("the body is not a JSON object")
            result = await handler(cast("dict[str, Any]", body))
        except (ValueError, TypeError, KeyError) as exc:
            return _error(400, "INVALID_REQUEST", str(exc))
        except ServiceNotFoundError as exc:
            return _error(404, "SERVICE_NOT_FOUND", str(exc))
        except RevisionNotFoundError as exc:
            return _error(404, "REVISION_NOT_FOUND", str(exc))
        except RuntimeDriverError as exc:
            log.warning("runtime call failed", extra={"method": method, "error": str(exc)})
            return _error(502, "RUNTIME_ERROR", str(exc))
        return JSONResponse(result)

    @app.post(BUILD_PREFIX + "/{method}")
    async def build(method: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if method not in ("start", "poll"):
            return _error(404, "NOT_FOUND", f"no method {method}")
        if builder is None:
            return _error(503, "BUILD_NOT_CONFIGURED", "this agent runs no builds")
        try:
            body: object = await request.json()
            if not isinstance(body, dict):
                raise TypeError("the body is not a JSON object")
            fields = cast("dict[str, Any]", body)
            if method == "start":
                result: dict[str, object] = {
                    "ref": await builder.start(build_from_wire(fields["build"]))
                }
            else:
                result = {"status": status_to_wire(await builder.poll(_str(fields, "ref")))}
        except (ValueError, TypeError, KeyError) as exc:
            return _error(400, "INVALID_REQUEST", str(exc))
        except BuildNotFoundError as exc:
            return _error(404, "BUILD_NOT_FOUND", str(exc))
        except BuildDriverError as exc:
            log.warning("build call failed", extra={"method": method, "error": str(exc)})
            return _error(502, "BUILD_ERROR", str(exc))
        return JSONResponse(result)

    _secret_routes(app, secrets)
    _database_routes(app, databases)
    return app


def _secret_routes(app: FastAPI, secrets: SecretCustody | None) -> None:
    """``ensure`` and nothing else: no route reads, returns or receives a secret value."""

    @app.post(SECRETS_PREFIX + "/{method}")
    async def secret(method: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if method != "ensure":
            return _error(404, "NOT_FOUND", f"no method {method}")
        if secrets is None:
            return _error(503, "SECRETS_NOT_CONFIGURED", "this agent keeps no secrets")
        try:
            body: object = await request.json()
            if not isinstance(body, dict):
                raise TypeError("the body is not a JSON object")
            name = _str(cast("dict[str, Any]", body), "secret")
            await secrets.ensure(name)
        except (ValueError, TypeError, KeyError) as exc:
            return _error(400, "INVALID_REQUEST", str(exc))
        except SecretsError as exc:
            log.warning("secret call failed", extra={"method": method, "error": str(exc)})
            return _error(502, "SECRETS_ERROR", str(exc))
        return JSONResponse({"secret": name})


def _database_routes(app: FastAPI, databases: CellAppDatabases | None) -> None:
    """``ensure``, ``rotate`` and ``usage`` of one service's database; no answer holds a value."""

    @app.post(DATABASES_PREFIX + "/{method}")
    async def database(method: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]  # noqa: PLR0911  (one return per refusal)
        if method not in ("ensure", "rotate", "usage"):
            return _error(404, "NOT_FOUND", f"no method {method}")
        if databases is None:
            return _error(503, "DATABASES_NOT_CONFIGURED", "this agent has no Cloud SQL instance")
        try:
            body: object = await request.json()
            if not isinstance(body, dict):
                raise TypeError("the body is not a JSON object")
            service = _service(cast("dict[str, Any]", body))
            if method == "usage":
                seen = await databases.usage(service)
                result: dict[str, object] = {
                    "present": seen.present,
                    "size_bytes": seen.size_bytes,
                    "connection_limit": seen.connection_limit,
                    "connections": seen.connections,
                    "environments": seen.environments,
                    "ceiling": seen.ceiling,
                }
            else:
                made = await (databases.ensure if method == "ensure" else databases.rotate)(service)
                result = _database_to_wire(made)
        except (ValueError, TypeError, KeyError) as exc:
            return _error(400, "INVALID_REQUEST", str(exc))
        except TierFullError as exc:
            return _error(409, "DB_TIER_FULL", str(exc))
        except DatabaseMissingError as exc:
            return _error(404, "DATABASE_NOT_FOUND", str(exc))
        except (AppDatabaseError, AdminSqlError, SecretsError) as exc:
            log.warning("database call failed", extra={"method": method, "error": str(exc)})
            return _error(502, "DATABASE_ERROR", str(exc))
        return JSONResponse(result)


def _database_to_wire(made: AppDatabase) -> dict[str, object]:
    return {
        "database": made.database,
        "user": made.user,
        "host": made.host,
        "port": made.port,
        "connection_limit": made.connection_limit,
        "versions": dict(made.versions),
    }


def _service(body: dict[str, Any]) -> str:
    service = _str(body, "service")
    if SERVICE_NAME.fullmatch(service) is None:
        raise ValueError(f"not an SSC app service name: {service!r}")
    return service


def _str(body: dict[str, Any], key: str) -> str:
    value = body[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": redact(message)}, status_code=status)
