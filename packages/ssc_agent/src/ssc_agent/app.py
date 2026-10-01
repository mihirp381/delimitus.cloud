"""The cell agent's HTTP surface: the ``RuntimeDriver`` protocol, one POST per method.

Cloud Run lets only the control plane's service account invoke the agent (decision 022), so
every request here already passed IAM. The agent still refuses any service name that is not an
SSC app's, because its own IAM cannot limit a create by name.

Errors are ``{"code", "message"}``: 404 ``SERVICE_NOT_FOUND`` or ``REVISION_NOT_FOUND``, 400
``INVALID_REQUEST``, 502 ``RUNTIME_ERROR``.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Final, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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

type Handler = Callable[[dict[str, Any]], Awaitable[dict[str, object]]]


def create_app(driver: RuntimeDriver) -> FastAPI:
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

    return app


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
    return JSONResponse({"code": code, "message": message}, status_code=status)
