"""console.delimitus.com: the built console, read-only, behind the control entry (SSC gap 1).

The load balancer sends ``/v1`` and ``/v1/*`` on this host to the API; everything else comes
here. What answers:

- ``/healthz``: ``ok``.
- ``/v1``, ``/mcp`` and anything under them: 404. They belong to the API, never to this host,
  even if the load balancer is ever misconfigured.
- A path with a ``..`` or ``.`` segment, a backslash or a NUL: 404.
- A file of ``dist/``: that file. ``/assets/*`` are content-hashed by Vite and kept a year; a
  missing asset is a 404, so a stale chunk fails loudly instead of loading the page as script.
- Any other path (``/apps/x``, ``/auth/callback``): ``index.html`` with ``no-store``, so a new
  build is picked up on the next load and the router takes it from there.

Every answer carries the security headers below. The policy names the auth host's origin for
``/token`` and ``/revoke`` (decision 029) and nothing else outside this origin; the build has no
inline script or style, so neither is allowed.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Final

from fastapi import FastAPI, Request, Response

from ssc_console_host.site import INDEX, File, Site

ASSETS: Final = "/assets/"
ASSET_CACHE: Final = "public, max-age=31536000, immutable"
INDEX_CACHE: Final = "no-store"
FILE_CACHE: Final = "no-cache"
"""Files other than the hashed assets and ``index.html``: revalidated by ETag on every use."""
API_PREFIXES: Final = ("/v1", "/mcp")
HEALTH: Final = "/healthz"
READ_METHODS: Final = frozenset({"GET", "HEAD"})
ALL_METHODS: Final = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def content_security_policy(auth_origin: str) -> str:
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            f"connect-src 'self' {auth_origin}",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            f"form-action 'self' {auth_origin}",
            "object-src 'none'",
        ]
    )


def security_headers(auth_origin: str) -> dict[str, str]:
    return {
        "content-security-policy": content_security_policy(auth_origin),
        # Covers only *.console.delimitus.com, of which there are none.
        "strict-transport-security": "max-age=31536000; includeSubDomains",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "cross-origin-opener-policy": "same-origin",
        "cross-origin-resource-policy": "same-origin",
        "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
    }


def is_api_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in API_PREFIXES)


def is_unsafe_path(path: str) -> bool:
    if "\\" in path or "\x00" in path:
        return True
    return any(segment in {".", ".."} for segment in path.split("/"))


def _plain(status: int, text: str, headers: Mapping[str, str] = {}) -> Response:  # noqa: B006
    return Response(
        text,
        status_code=status,
        media_type="text/plain; charset=utf-8",
        headers={"cache-control": "no-store", **headers},
    )


def _file(request: Request, file: File, cache: str) -> Response:
    headers = {"etag": file.etag, "cache-control": cache}
    if cache != INDEX_CACHE and file.etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    if request.method == "HEAD":
        headers["content-length"] = str(len(file.body))
        return Response(media_type=file.content_type, headers=headers)
    return Response(file.body, media_type=file.content_type, headers=headers)


def create_app(site: Site, *, auth_origin: str) -> FastAPI:
    """``auth_origin`` is the auth host's, ``https://auth.delimitus.com``."""
    app = FastAPI(title="ssc-console-host", docs_url=None, redoc_url=None, openapi_url=None)
    headers = security_headers(auth_origin)

    @app.middleware("http")
    async def secure(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(headers)
        return response

    @app.api_route("/{rest:path}", methods=ALL_METHODS)
    def serve(request: Request, rest: str) -> Response:  # noqa: ARG001, PLR0911
        path: str = request.scope["path"]  # decoded, so %2e%2e is seen as ..
        if is_api_path(path) or is_unsafe_path(path):
            return _plain(404, "Not found\n")
        if request.method not in READ_METHODS:
            return _plain(405, "Method not allowed\n", {"allow": "GET, HEAD"})
        if path == HEALTH:
            return _plain(200, "ok\n")
        found = site.files.get(path)
        if path.startswith(ASSETS):
            return _file(request, found, ASSET_CACHE) if found else _plain(404, "Not found\n")
        if found is not None and path != INDEX:
            return _file(request, found, FILE_CACHE)
        return _file(request, site.index, INDEX_CACHE)

    return app
