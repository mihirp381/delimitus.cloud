"""The cell agent's HTTP surface: the ``RuntimeDriver`` protocol, the ``CellBuilder``
protocol (SSC-015), ``SecretCustody`` (SSC-026) and app databases (SSC-040), one POST per method.

Cloud Run lets only the control plane's service account invoke the agent (decision 022), so
every request here already passed IAM. The agent still refuses any service or secret name that
is not an SSC app's, because its own IAM cannot limit a create by name. Secrets have one method,
``ensure``; nothing here reads, returns or receives a secret value. App databases have
``ensure``, ``rotate``, ``usage``, ``recovery_point`` and ``drop``; their passwords stay in the
agent and the cell's Secret Manager, and the answers carry secret versions only. ``drop`` runs
only while the service is gone or stopped (``SERVICE_LIVE`` otherwise), so no running app loses
its database.
Logs (SSC-024) have ``read``, ``follow`` and ``health`` of one service; the agent builds the
filter, redacts every line, and keeps the cell under Cloud Logging's quota.

Errors are ``{"code", "message"}``: 404 ``SERVICE_NOT_FOUND``, ``REVISION_NOT_FOUND``,
``BUILD_NOT_FOUND`` or ``DATABASE_NOT_FOUND``, 400 ``INVALID_REQUEST``, 409 ``DB_TIER_FULL`` or
``SERVICE_LIVE``, 502 ``RUNTIME_ERROR``, ``BUILD_ERROR``, ``SECRETS_ERROR`` or
``DATABASE_ERROR``, 503 ``BUILD_NOT_CONFIGURED``, ``SECRETS_NOT_CONFIGURED`` or
``DATABASES_NOT_CONFIGURED`` when the agent runs without a builder, secret custody or a Cloud SQL
instance. Logs add 429 ``LOGS_RATE_LIMITED`` (with ``retry_after``), 502 ``LOGS_ERROR`` and 503
``LOGS_NOT_CONFIGURED`` without a log view; health still answers then, from the service alone.
Usage (SSC-028) has ``read``: counts and durations for every app service in the cell over whole
hours, from Cloud Monitoring, never from an app; 502 ``USAGE_ERROR``, and 503
``USAGE_NOT_CONFIGURED`` without a usage source or the right to read it.
Egress (SSC-053) has ``issue``, a new proxy credential written to an environment's
``HTTPS_PROXY`` secret (the answer holds its id, its token's digest and the secret version,
never the token), and ``info``, the proxy's address and the cell's fixed outbound address; 502
``SECRETS_ERROR``, and 503 ``EGRESS_NOT_CONFIGURED`` for ``issue`` without a proxy address.
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
from ssc_agent.cloud_logging import CellLogHub
from ssc_agent.cloud_monitoring import CellUsageReader
from ssc_agent.egress import EgressNotConfiguredError, ProxyCredentials
from ssc_agent.secret_manager import SecretCustody, SecretsError
from ssc_shared.build import (
    BuildDriverError,
    BuildNotFoundError,
    CellBuilder,
    build_from_wire,
    status_to_wire,
)
from ssc_shared.logs import (
    CellLogs,
    LogsError,
    LogsNotConfiguredError,
    LogsRateLimitedError,
    check_caller,
    health_to_wire,
    page_to_wire,
    query_from_wire,
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
from ssc_shared.usage import (
    CellUsage,
    UsageError,
    UsageNotConfiguredError,
    report_to_wire,
    window_from_wire,
)

log = logging.getLogger(__name__)

PREFIX: Final = "/v1/runtime"
BUILD_PREFIX: Final = "/v1/build"
SECRETS_PREFIX: Final = "/v1/secrets"
DATABASES_PREFIX: Final = "/v1/databases"
LOGS_PREFIX: Final = "/v1/logs"
USAGE_PREFIX: Final = "/v1/usage"
EGRESS_PREFIX: Final = "/v1/egress"

type Handler = Callable[[dict[str, Any]], Awaitable[dict[str, object]]]


def create_app(  # noqa: PLR0913  (usage is keyword-only)
    driver: RuntimeDriver,
    builder: CellBuilder | None = None,
    secrets: SecretCustody | None = None,
    databases: CellAppDatabases | None = None,
    logs: CellLogs | None = None,
    *,
    usage: CellUsage | None = None,
    egress: ProxyCredentials | None = None,
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
    _database_routes(app, driver, databases)
    _log_routes(app, CellLogHub(None, driver) if logs is None else logs)
    _usage_routes(app, CellUsageReader(None) if usage is None else usage)
    _egress_routes(app, egress)
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


def _database_routes(
    app: FastAPI, driver: RuntimeDriver, databases: CellAppDatabases | None
) -> None:
    """``ensure``, ``rotate``, ``usage``, ``recovery_point`` and ``drop`` of one service's
    database; no answer holds a value."""

    @app.post(DATABASES_PREFIX + "/{method}")
    async def database(method: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]  # noqa: PLR0911  (one return per refusal)
        if method not in ("ensure", "rotate", "usage", "recovery_point", "drop"):
            return _error(404, "NOT_FOUND", f"no method {method}")
        if databases is None:
            return _error(503, "DATABASES_NOT_CONFIGURED", "this agent has no Cloud SQL instance")
        try:
            body: object = await request.json()
            if not isinstance(body, dict):
                raise TypeError("the body is not a JSON object")
            service = _service(cast("dict[str, Any]", body))
            if method == "drop":
                seen = await driver.observe(service)
                if seen is not None and not seen.stopped:
                    return _error(409, "SERVICE_LIVE", f"{service} still runs; stop it first")
                await databases.drop(service)
                return JSONResponse({"dropped": service})
            result = await _database_answer(databases, method, service)
        except (ValueError, TypeError, KeyError) as exc:
            return _error(400, "INVALID_REQUEST", str(exc))
        except TierFullError as exc:
            return _error(409, "DB_TIER_FULL", str(exc))
        except DatabaseMissingError as exc:
            return _error(404, "DATABASE_NOT_FOUND", str(exc))
        except RuntimeDriverError as exc:
            log.warning("database call failed", extra={"method": method, "error": str(exc)})
            return _error(502, "RUNTIME_ERROR", str(exc))
        except (AppDatabaseError, AdminSqlError, SecretsError) as exc:
            log.warning("database call failed", extra={"method": method, "error": str(exc)})
            return _error(502, "DATABASE_ERROR", str(exc))
        return JSONResponse(result)


async def _database_answer(
    databases: CellAppDatabases, method: str, service: str
) -> dict[str, object]:
    """The answer to ``ensure``, ``rotate``, ``usage`` or ``recovery_point``; none holds a value."""
    if method == "recovery_point":
        point = await databases.recovery_point(service)
        return {"at": point.at, "lsn": point.lsn}
    if method == "usage":
        seen = await databases.usage(service)
        return {
            "present": seen.present,
            "size_bytes": seen.size_bytes,
            "connection_limit": seen.connection_limit,
            "connections": seen.connections,
            "environments": seen.environments,
            "ceiling": seen.ceiling,
        }
    made = await (databases.ensure if method == "ensure" else databases.rotate)(service)
    return _database_to_wire(made)


def _log_routes(app: FastAPI, logs: CellLogs) -> None:
    """``read``, ``follow`` and ``health`` of one service's logs."""

    @app.post(LOGS_PREFIX + "/{method}")
    async def log_call(method: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]  # noqa: PLR0911  (one return per refusal)
        if method not in ("read", "follow", "health"):
            return _error(404, "NOT_FOUND", f"no method {method}")
        try:
            body: object = await request.json()
            if not isinstance(body, dict):
                raise TypeError("the body is not a JSON object")
            fields = cast("dict[str, Any]", body)
            caller = check_caller(_str(fields, "caller"))
            if method == "health":
                result = health_to_wire(await logs.health(_service(fields), caller=caller))
            elif method == "read":
                page = await logs.read(
                    query_from_wire(fields["query"]),
                    since_seconds=_int(fields, "since_seconds"),
                    limit=_int(fields, "limit"),
                    caller=caller,
                )
                result = page_to_wire(page)
            else:
                cursor = fields["cursor"]
                page = await logs.follow(
                    query_from_wire(fields["query"]),
                    cursor=None if cursor is None else _str(fields, "cursor"),
                    wait_seconds=_int(fields, "wait_seconds"),
                    caller=caller,
                )
                result = page_to_wire(page)
        except (ValueError, TypeError, KeyError) as exc:
            return _error(400, "INVALID_REQUEST", str(exc))
        except LogsRateLimitedError as exc:
            response = _error(429, "LOGS_RATE_LIMITED", str(exc), retry_after=exc.retry_after)
            response.headers["Retry-After"] = str(exc.retry_after)
            return response
        except LogsNotConfiguredError as exc:
            return _error(503, "LOGS_NOT_CONFIGURED", str(exc))
        except LogsError as exc:
            log.warning("logs call failed", extra={"method": method, "error": str(exc)})
            return _error(502, "LOGS_ERROR", str(exc))
        except RuntimeDriverError as exc:
            log.warning("logs call failed", extra={"method": method, "error": str(exc)})
            return _error(502, "RUNTIME_ERROR", str(exc))
        return JSONResponse(result)


def _usage_routes(app: FastAPI, usage: CellUsage) -> None:
    """``read``: the usage of every app service in the cell over whole hours."""

    @app.post(USAGE_PREFIX + "/{method}")
    async def usage_call(method: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if method != "read":
            return _error(404, "NOT_FOUND", f"no method {method}")
        try:
            body: object = await request.json()
            if not isinstance(body, dict):
                raise TypeError("the body is not a JSON object")
            window = window_from_wire(cast("dict[str, Any]", body)["window"])
            result = report_to_wire(await usage.read(window))
        except (ValueError, TypeError, KeyError) as exc:
            return _error(400, "INVALID_REQUEST", str(exc))
        except UsageNotConfiguredError as exc:
            return _error(503, "USAGE_NOT_CONFIGURED", str(exc))
        except UsageError as exc:
            log.warning("usage call failed", extra={"error": str(exc)})
            return _error(502, "USAGE_ERROR", str(exc))
        return JSONResponse(result)


def _egress_routes(app: FastAPI, egress: ProxyCredentials | None) -> None:
    """``issue`` and ``info``; no answer holds a token."""

    @app.post(EGRESS_PREFIX + "/{method}")
    async def egress_call(method: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]  # noqa: PLR0911  (one return per refusal)
        if method not in ("issue", "info"):
            return _error(404, "NOT_FOUND", f"no method {method}")
        if method == "info":
            return JSONResponse(
                {
                    "proxy_address": None if egress is None else egress.proxy_address,
                    "outbound_ip": None if egress is None else egress.outbound_ip,
                }
            )
        if egress is None:
            return _error(503, "EGRESS_NOT_CONFIGURED", "this agent has no proxy address")
        try:
            body: object = await request.json()
            if not isinstance(body, dict):
                raise TypeError("the body is not a JSON object")
            issued = await egress.issue(_str(cast("dict[str, Any]", body), "environment_id"))
        except (ValueError, TypeError, KeyError) as exc:
            return _error(400, "INVALID_REQUEST", str(exc))
        except EgressNotConfiguredError as exc:
            return _error(503, "EGRESS_NOT_CONFIGURED", str(exc))
        except SecretsError as exc:
            log.warning("egress issue failed", extra={"error": str(exc)})
            return _error(502, "SECRETS_ERROR", str(exc))
        return JSONResponse(
            {
                "credential_id": issued.credential_id,
                "sha1": issued.sha1,
                "version": issued.version,
            }
        )


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


def _int(body: dict[str, Any], key: str) -> int:
    value = body[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{key} must be an integer")
    return value


def _error(status: int, code: str, message: str, **extra: object) -> JSONResponse:
    return JSONResponse({"code": code, "message": redact(message), **extra}, status_code=status)
