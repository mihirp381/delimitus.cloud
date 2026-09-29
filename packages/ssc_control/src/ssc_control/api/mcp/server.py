"""Build the MCP server and put its routes on the API's application.

Stateless streamable HTTP with JSON replies at ``/mcp``, plus the protected-resource metadata at
``/.well-known/oauth-protected-resource/mcp``. The routes are added to the FastAPI router, not
mounted, so ``/mcp`` gets no trailing-slash redirect and the metadata sits at the root. They are
Starlette routes, so ``openapi.json`` does not list them. Building does no I/O; the session
manager runs inside the application's lifespan.
"""

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Final
from urllib.parse import urlsplit

from fastapi import FastAPI
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from ssc_control.api.mcp.auth import AgentTokenVerifier
from ssc_control.api.mcp.tools import register
from ssc_control.api.runtime import Runtime

MCP_PATH: Final = "/mcp"
INSTRUCTIONS: Final = (
    "Small Software Cloud: see the apps in your org, what each environment runs, and roll an "
    "environment back to an earlier release. Every call is recorded as made by your agent on "
    "behalf of the person whose credential it holds."
)


@contextmanager
def _root_logging_kept() -> Generator[None]:
    """``MCPServer()`` calls ``logging.basicConfig``; the API leaves the root logger to its host."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def build_mcp(rt: Runtime, api: FastAPI) -> MCPServer:
    s = rt.settings
    with _root_logging_kept():
        server = MCPServer(
            name="ssc",
            instructions=INSTRUCTIONS,
            token_verifier=AgentTokenVerifier(rt.verifier, s.user_audience),
            # Validated from strings so a path-less issuer keeps its exact spelling (no "/").
            auth=AuthSettings.model_validate(
                {
                    "issuer_url": s.issuer,
                    "resource_server_url": f"{s.public_url.rstrip('/')}{MCP_PATH}",
                    # The verifier checks the audience itself; True once login issues
                    # resource-bound tokens (SSC-019).
                    "validate_token_resource": False,
                }
            ),
        )
    register(server, api)
    return server


def install_mcp(app: FastAPI, rt: Runtime) -> MCPServer:
    """Add the MCP routes to ``app``. The caller runs ``server.session_manager.run()``."""
    server = build_mcp(rt, app)
    public = urlsplit(rt.settings.public_url)
    asgi = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        max_request_body_size=rt.settings.max_body_bytes,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[public.netloc, "localhost:*", "127.0.0.1:*", "[::1]:*"],
            allowed_origins=[f"{public.scheme}://{public.netloc}"],
        ),
    )
    # Each path is served by the whole SDK application, so its auth middleware applies.
    app.router.routes.extend(Route(r.path, asgi) for r in asgi.routes if isinstance(r, Route))
    return server
