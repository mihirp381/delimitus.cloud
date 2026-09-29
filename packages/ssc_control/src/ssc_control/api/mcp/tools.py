"""The agent's tools, phase 1: read apps and their status, and roll back.

Every tool calls ``/v1`` on this same application, in process, with the caller's own bearer. The
route handlers therefore decide everything, exactly as for the command line: authorisation,
row-level security, the ``Idempotency-Key`` claim, the per-credential rate limit and audit (with
``via_agent`` and ``client_id`` taken from the credential). There is no second code path.

A refusal becomes a tool error (``isError``) whose structured content is ``{"error": {...}}``, the
same members ``ssc --json`` prints: the problem's for an API refusal, ``status: null`` for one
found here. Absent on purpose: approving (decision 016 refuses agent sessions), promote, secrets,
logs and connections.
"""

import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Final, Literal, cast
from uuid import uuid4

import httpx2
from fastapi import FastAPI
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.api.problems import REQUEST_ID_HEADER, REQUEST_ID_SCOPE_KEY

TOOLS: Final = ("list_apps", "get_app", "get_status", "rollback")
BASE_URL: Final = "http://ssc.internal"
APP_PREFIX: Final = "app_"

AppRef = Annotated[
    str,
    Field(
        pattern=r"^(app_[a-z0-9]{20}|[a-z]([a-z0-9-]{0,38}[a-z0-9])?)$",
        description="The app's id (`app_...`) or its slug.",
    ),
]
ReleaseId = Annotated[
    str, Field(pattern=r"^rel_[a-z0-9]{20}$", description="The release to go back to.")
]
OperationId = Annotated[
    str, Field(pattern=r"^dep_[a-z0-9]{20}$", description="An operation id from `rollback`.")
]
IdempotencyKey = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        description="Send the same key to retry safely; a new one is made when absent.",
    ),
]

type Body = dict[str, Any]


class Refused(Exception):  # noqa: N818  (a refusal to report, not a programming error)
    def __init__(self, error: Body) -> None:
        super().__init__(error.get("code"))
        self.error = error


def local_error(code: str, title: str, detail: str, status: int | None = None) -> Body:
    return {
        "code": code,
        "title": title,
        "detail": detail,
        "status": status,
        "request_id": None,
        "instance": None,
        "type": None,
    }


def _checked(r: httpx2.Response) -> httpx2.Response:
    if r.is_success:
        return r
    try:
        body: object = r.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and "code" in body:
        raise Refused(cast("Body", body))
    raise Refused(
        local_error(
            "BAD_RESPONSE",
            "Unexpected answer.",
            f"The API answered {r.status_code} without a problem.",
            r.status_code,
        )
    )


class V1:
    """``/v1`` on this application, as the calling agent."""

    def __init__(self, client: httpx2.AsyncClient) -> None:
        self._client = client

    async def get(self, path: str) -> Body:
        body: Body = _checked(await self._client.get(path)).json()
        return body

    async def post(self, path: str, body: Body, key: str) -> httpx2.Response:
        return _checked(await self._client.post(path, json=body, headers={IDEMPOTENCY_HEADER: key}))


def _request_id(ctx: Context) -> str | None:
    """The id of the MCP request, so a refusal's ``request_id`` matches its ``X-Request-Id``."""
    scope = cast("dict[str, object] | None", getattr(ctx.request_context.request, "scope", None))
    rid = scope.get(REQUEST_ID_SCOPE_KEY) if scope is not None else None
    return rid if isinstance(rid, str) else None


@asynccontextmanager
async def v1(api: FastAPI, ctx: Context) -> AsyncGenerator[V1]:
    token = get_access_token()
    if token is None:  # the SDK's auth middleware has already refused such a request
        raise ToolError("Not authenticated.")
    headers = {"Authorization": f"Bearer {token.token}"}
    rid = _request_id(ctx)
    if rid is not None:
        headers[REQUEST_ID_HEADER] = rid
    transport = httpx2.ASGITransport(app=api, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL, headers=headers) as c:
        yield V1(c)


def ok(body: Body) -> CallToolResult:
    text = json.dumps(body, sort_keys=True)
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=body)


def refused(error: Body) -> CallToolResult:
    text = f"{error['code']}: {error['title']} {error['detail']}"
    if error.get("request_id"):
        text += f" Request id: {error['request_id']}."
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content={"error": error},
        is_error=True,
    )


async def run(api: FastAPI, ctx: Context, work: Callable[[V1], Awaitable[Body]]) -> CallToolResult:
    try:
        async with v1(api, ctx) as c:
            return ok(await work(c))
    except Refused as e:
        return refused(e.error)


async def resolve_app(c: V1, ref: str) -> Body:
    """An ``app_`` id, or a slug looked up in the org's app list (as ``ssc`` does)."""
    if ref.startswith(APP_PREFIX):
        return await c.get(f"/v1/apps/{ref}")
    for app in (await c.get("/v1/apps"))["apps"]:
        if app["slug"] == ref:
            return await c.get(f"/v1/apps/{app['id']}")
    raise Refused(
        local_error(
            "APP_NOT_FOUND",
            "No such app.",
            f"No app with slug or id {ref!r} is visible to you. Call list_apps to see them.",
        )
    )


def environment_id(app: Body, name: str) -> str:
    for env in app["environments"]:
        if env["name"] == name:
            return str(env["id"])
    raise Refused(
        local_error(
            "ENVIRONMENT_NOT_FOUND",
            "No such environment.",
            f"App {app['slug']} has no {name!r} environment.",
        )
    )


def register(server: MCPServer, api: FastAPI) -> None:
    read = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    async def list_apps(ctx: Context) -> CallToolResult:
        """List the apps in your org that you can see: id, slug, owner and status."""
        return await run(api, ctx, lambda c: c.get("/v1/apps"))

    async def get_app(app: AppRef, ctx: Context) -> CallToolResult:
        """One app with its environments (`prod` and `preview`), their config and sharing
        versions and the deployment each one runs."""
        return await run(api, ctx, lambda c: resolve_app(c, app))

    async def get_status(
        app: AppRef, ctx: Context, operation: OperationId | None = None
    ) -> CallToolResult:
        """What an app runs now: the app, and for each environment the operation that deployed
        its current release (null when nothing runs). Pass `operation` to follow one started by
        `rollback`: it reports pending, running, healthy, failed or superseded."""

        async def work(c: V1) -> Body:
            found = await resolve_app(c, app)
            current: dict[str, Body | None] = {}
            for env in found["environments"]:
                dep = env["current_deployment_id"]
                current[env["name"]] = await c.get(f"/v1/operations/{dep}") if dep else None
            out: Body = {"app": found, "current": current}
            if operation is not None:
                out["operation"] = await c.get(f"/v1/operations/{operation}")
            return out

        return await run(api, ctx, work)

    async def rollback(
        app: AppRef,
        release: ReleaseId,
        env: Literal["prod", "preview"],
        ctx: Context,
        idempotency_key: IdempotencyKey | None = None,
    ) -> CallToolResult:
        """Put an earlier release back in one environment. This starts an operation and returns
        its id at once; follow it with get_status(app, operation=...). Only one deployment runs
        per environment at a time. To retry after an error, send the same idempotency_key: the
        same operation comes back and nothing starts twice."""
        key = idempotency_key or uuid4().hex

        async def work(c: V1) -> Body:
            found = await resolve_app(c, app)
            path = f"/v1/apps/{found['id']}/environments/{environment_id(found, env)}/deployments"
            r = await c.post(path, {"release_id": release, "kind": "rollback"}, key)
            body: Body = r.json()
            return {**body, "location": r.headers["Location"], "idempotency_key": key}

        return await run(api, ctx, work)

    server.add_tool(list_apps, annotations=read)
    server.add_tool(get_app, annotations=read)
    server.add_tool(get_status, annotations=read)
    server.add_tool(
        rollback,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
