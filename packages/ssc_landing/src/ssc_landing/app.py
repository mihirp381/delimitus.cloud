"""delimitus.com: the landing page and the one thing it posts to, ``/pilot-request`` (SSC-065).

Nothing else answers: no docs, no health path, no cookies. ``www.`` and any other name in
``redirect_hosts`` moves permanently to the origin. A pilot request is accepted only from the
page's own origin, small, and not too often from one address; it is stored, and then one log
line without the visitor's details tells the founder that one came in.
"""

import html
import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Final

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from ssc_landing.limits import RateLimit, client_address
from ssc_landing.page import PLAIN_CSP, SECURITY_HEADERS, Page
from ssc_landing.pilot import PilotRefused, is_trapped, parse_body, pilot_request_from
from ssc_landing.store import PilotStore, StoreError

MAX_BODY: Final = 4 * 1024
PAGE_MAX_AGE: Final = 300
FORM_TYPE: Final = "application/x-www-form-urlencoded"

Log = Callable[[Mapping[str, object]], None]


def stdout_log(entry: Mapping[str, object]) -> None:
    """One JSON line; Cloud Run turns it into a structured log entry the alert matches on."""
    sys.stdout.write(json.dumps(entry, sort_keys=True) + "\n")
    sys.stdout.flush()


def _plain(status: int, text: str) -> Response:
    return Response(
        text,
        status_code=status,
        media_type="text/plain; charset=utf-8",
        headers={"content-security-policy": PLAIN_CSP, "cache-control": "no-store"},
    )


def _html_reply(status: int, heading: str, message: str) -> Response:
    """The answer to a form posted without the page's script: a heading, a line, a way back."""
    body = (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(heading)} · Delimitus</title>"
        f"<h1>{html.escape(heading)}</h1><p>{html.escape(message)}</p>"
        '<p><a href="/">Back to delimitus.com</a></p></html>'
    )
    return Response(
        body,
        status_code=status,
        media_type="text/html; charset=utf-8",
        headers={"content-security-policy": PLAIN_CSP, "cache-control": "no-store"},
    )


def _reply(
    plain_form: bool, refusal: PilotRefused | None, headers: Mapping[str, str] = {}
) -> Response:  # noqa: B006
    if refusal is None:
        if plain_form:
            return _html_reply(200, "Thanks", "We have your request and will reply by email.")
        return JSONResponse({"ok": True}, headers={"cache-control": "no-store"})
    if plain_form:
        response = _html_reply(refusal.status, "Not sent", refusal.message)
    else:
        body = {
            "ok": False,
            "error": refusal.code,
            "message": refusal.message,
            "fields": list(refusal.fields),
        }
        response = JSONResponse(body, status_code=refusal.status)
        response.headers["cache-control"] = "no-store"
    response.headers.update(headers)
    return response


async def _read_capped(request: Request, cap: int) -> bytes | None:
    """The body, or None once it passes ``cap`` bytes; ``Content-Length`` is not trusted alone."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > cap):
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(  # noqa: PLR0913  (keyword-only collaborators)
    page: Page,
    store: PilotStore,
    *,
    origin: str,
    redirect_hosts: frozenset[str] = frozenset(),
    trusted_hops: int = 0,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    log: Log = stdout_log,
) -> FastAPI:
    """``origin`` is the page's, ``https://delimitus.com``; posts must come from it."""
    app = FastAPI(title="ssc-landing", docs_url=None, redoc_url=None, openapi_url=None)
    limit = RateLimit(lambda: now().timestamp())

    @app.middleware("http")
    async def headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = request.headers.get("host", "").split(":", 1)[0].lower()
        if host in redirect_hosts:
            target = (
                origin + request.url.path + (f"?{request.url.query}" if request.url.query else "")
            )
            response = Response(status_code=301, headers={"location": target})
        else:
            response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.api_route("/", methods=["GET", "HEAD"])
    def index(request: Request) -> Response:
        cache = {"etag": page.etag, "cache-control": f"public, max-age={PAGE_MAX_AGE}"}
        if page.etag in request.headers.get("if-none-match", ""):
            return Response(status_code=304, headers=cache)
        return Response(
            page.body,
            media_type="text/html; charset=utf-8",
            headers={**cache, "content-security-policy": page.csp},
        )

    @app.post("/pilot-request")
    async def pilot_request(request: Request) -> Response:  # noqa: PLR0911  (one return a check)
        content_type = request.headers.get("content-type", "")
        plain_form = content_type.split(";", 1)[0].strip().lower() == FORM_TYPE
        if request.headers.get("origin") != origin:
            refusal = PilotRefused(403, "WRONG_ORIGIN", "Send the form from delimitus.com.")
            return _reply(plain_form, refusal)
        body = await _read_capped(request, MAX_BODY)
        if body is None:
            return _reply(
                plain_form, PilotRefused(413, "TOO_LARGE", "That is more than the form holds.")
            )
        peer = request.client.host if request.client else None
        address = client_address(request.headers.get("x-forwarded-for"), peer, trusted_hops)
        if not limit.allow(address):
            refusal = PilotRefused(
                429, "TOO_MANY", "That didn't go through. Try again in a minute."
            )
            return _reply(plain_form, refusal, {"retry-after": "60"})
        try:
            fields = parse_body(content_type, body)
            if is_trapped(fields):
                log({"severity": "INFO", "event": "pilot_request_trapped"})
                return _reply(plain_form, None)
            pilot = pilot_request_from(fields, now())
        except PilotRefused as refusal:
            return _reply(plain_form, refusal)
        try:
            await store.add(pilot)
        except StoreError as e:
            log({"severity": "ERROR", "event": "pilot_request_not_stored", "cause": str(e)})
            refusal = PilotRefused(
                503, "NOT_STORED", "That didn't go through. Try again in a minute."
            )
            return _reply(plain_form, refusal)
        log({"severity": "NOTICE", "event": "pilot_request_stored"})
        return _reply(plain_form, None)

    @app.api_route(
        "/{rest:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    )
    def not_found(rest: str) -> Response:  # noqa: ARG001
        return _plain(404, "Not found\n")

    return app
