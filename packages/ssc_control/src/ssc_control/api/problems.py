"""The one renderer of refusals, plus the request id every refusal carries.

Delimitus had three renderers and one of them put SQL text in a user-facing body. Here there is
:func:`render`, and everything that can go wrong in a request ends up in it: our own
:class:`Refusal`, FastAPI's validation errors, Starlette's ``HTTPException`` (404, 405, ...),
SQLAlchemy's ``DBAPIError`` (through ``dberrors.refusal_for``) and any other exception.

Evidence is a mapping the raiser attaches; :func:`render` logs it as one JSON line keyed by the
request id and drops it. The body is exactly the members of ``ssc_contracts.errors.Problem``.
"""

import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any, Final

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException

from ssc_contracts.errors import (
    CATALOGUE,
    PROBLEM_MEDIA_TYPE,
    ErrorCode,
    Problem,
    problem_type,
)
from ssc_control.api import dberrors

log = logging.getLogger("ssc.api")

REQUEST_ID_HEADER: Final = "X-Request-Id"
REQUEST_ID_SCOPE_KEY: Final = "ssc.request_id"

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class Refusal(Exception):  # noqa: N818  (a named refusal, not a programming error)
    """A catalogue code plus evidence for the log. Raise it anywhere in a request."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        evidence: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.evidence: dict[str, object] = dict(evidence or {})
        self.headers: dict[str, str] = dict(headers or {})


def new_request_id() -> str:
    return secrets.token_hex(12)


def request_id_of(request: Request) -> str:
    rid = request.scope.get(REQUEST_ID_SCOPE_KEY)
    return rid if isinstance(rid, str) and rid else "-"


_INCOMING_ID: Final = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def incoming_request_id(scope: Scope) -> str | None:
    """The gateway's ``X-Request-Id`` when it looks like one; anything else is ignored."""
    for name, value in scope.get("headers") or ():
        if name.lower() == b"x-request-id":
            text = value.decode("latin-1")
            return text if _INCOMING_ID.match(text) else None
    return None


class RequestIdMiddleware:
    """Gives every request an id and echoes it in ``X-Request-Id`` on every response.

    A well-formed id from the gateway is kept so one id follows the request across hops.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        rid = incoming_request_id(scope) or new_request_id()
        scope[REQUEST_ID_SCOPE_KEY] = rid

        async def send_with_id(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers") or [])
                if not any(k.lower() == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", rid.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_id)


class _BodyTooLargeError(Exception):
    """Raised into the app by :class:`BodyLimitMiddleware` once the body passes the limit."""


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers") or ():
        if name.lower() == b"content-length":
            text = value.decode("latin-1").strip()
            return int(text) if text.isdigit() else None
    return None


class BodyLimitMiddleware:
    """Refuses a request body over ``max_bytes`` with ``REQUEST_TOO_LARGE``.

    A declared ``Content-Length`` over the limit is refused before the app runs; otherwise the
    body is counted as the app reads it, and past the limit the app's reads fail, whatever it
    answers is dropped and the problem is sent instead (if the app had already started its
    response, the response is cut short). Paths under ``exempt`` carry their own limits. Add it
    before ``RequestIdMiddleware`` so it runs inside it and the problem carries the request id.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int, exempt: tuple[str, ...] = ()) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.exempt = exempt

    def _is_exempt(self, path: str) -> bool:
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.exempt)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send, reason: str) -> None:
        response = render(
            Request(scope),
            ErrorCode.REQUEST_TOO_LARGE,
            evidence={"reason": reason, "limit": self.max_bytes},
        )
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or self._is_exempt(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return
        declared = _declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._refuse(scope, receive, send, "content_length")
            return
        received = 0
        over = False
        started = False

        async def counted_receive() -> Message:
            nonlocal received, over
            if over:
                raise _BodyTooLargeError
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > self.max_bytes:
                    over = True
                    raise _BodyTooLargeError
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if over:
                return
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except Exception:
            if not over:
                raise
        if over and not started:
            await self._refuse(scope, receive, send, "streamed")


def render(
    request: Request,
    code: ErrorCode,
    *,
    evidence: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """The only function that builds a problem body."""
    entry = CATALOGUE[code]
    rid = request_id_of(request)
    log.warning(
        "refusal %s",
        json.dumps(
            {
                "request_id": rid,
                "code": code.value,
                "status": entry.status,
                "method": request.method,
                "path": request.url.path,
                "evidence": dict(evidence or {}),
            },
            default=str,
            sort_keys=True,
        ),
    )
    body = Problem(
        type=problem_type(code),
        title=entry.title,
        status=entry.status,
        detail=entry.detail,
        instance=request.url.path,
        code=code,
        request_id=rid,
    )
    out = {REQUEST_ID_HEADER: rid, **(headers or {})}
    return Response(
        content=body.model_dump_json(),
        status_code=entry.status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=out,
    )


_STATUS_CODES: Final[Mapping[int, ErrorCode]] = {
    400: ErrorCode.VALIDATION_FAILED,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    422: ErrorCode.VALIDATION_FAILED,
    429: ErrorCode.RATE_LIMITED,
}


def code_for_status(status: int) -> ErrorCode:
    if status in _STATUS_CODES:
        return _STATUS_CODES[status]
    return ErrorCode.INTERNAL if status >= 500 else ErrorCode.VALIDATION_FAILED


async def _on_refusal(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, Refusal)
    return render(request, exc.code, evidence=exc.evidence, headers=exc.headers)


async def _on_http_exception(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    return render(
        request,
        code_for_status(exc.status_code),
        evidence={"detail": exc.detail},
        headers=exc.headers or {},
    )


async def _on_validation(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    # Where and why only: the input, and a message or context that may quote it, stay out.
    errors = [{"loc": e["loc"], "type": e["type"]} for e in exc.errors()]
    return render(request, ErrorCode.VALIDATION_FAILED, evidence={"errors": errors})


async def _on_dbapi(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, DBAPIError)
    code, evidence = dberrors.classify(exc)
    headers = (
        {"Retry-After": str(dberrors.RETRY_AFTER_SECONDS)}
        if code is ErrorCode.TRANSIENT_CONFLICT
        else None
    )
    return render(request, code, evidence=evidence, headers=headers)


async def _on_unexpected(request: Request, exc: Exception) -> Response:
    log.exception("unhandled error in request %s", request_id_of(request))
    return render(request, ErrorCode.INTERNAL, evidence={"exception": type(exc).__name__})


def install(app: FastAPI) -> None:
    app.add_exception_handler(Refusal, _on_refusal)
    app.add_exception_handler(HTTPException, _on_http_exception)
    app.add_exception_handler(RequestValidationError, _on_validation)
    app.add_exception_handler(DBAPIError, _on_dbapi)
    app.add_exception_handler(Exception, _on_unexpected)
