"""The agent's tools: read apps, releases and status, deploy to preview, roll back, and ask
for sharing or a data connection.

Every tool calls ``/v1`` on this same application, in process, with the caller's own bearer. The
route handlers therefore decide everything, exactly as for the command line: authorisation,
row-level security, the ``Idempotency-Key`` claim, the per-credential rate limit and audit (with
``via_agent`` and ``client_id`` taken from the credential). There is no second code path.

A refusal becomes a tool error (``isError``) whose structured content is ``{"error": {...}}``, the
same members ``ssc --json`` prints: the problem's for an API refusal, ``status: null`` for one
found here. Anything that would widen what an app can do only opens an approval request. Absent on
purpose: approving (decision 016 refuses agent sessions), promote, secrets, logs and connections.
"""

import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Final, Literal, cast
from urllib.parse import urlencode
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
from ssc_control.api.settings import Settings
from ssc_control.domain import grant_rules
from ssc_control.domain.approval_rules import GrantKey

TOOLS: Final = (
    "list_apps",
    "get_app",
    "get_status",
    "list_releases",
    "rollback",
    "deploy",
    "request_share",
    "request_connection",
)
BASE_URL: Final = "http://ssc.internal"
APP_PREFIX: Final = "app_"
SHARE_ATTEMPTS: Final = 3
RELEASE_PAGE: Final = 100

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
    str,
    Field(
        pattern=r"^dep_[a-z0-9]{20}$", description="An operation id from `rollback` or `deploy`."
    ),
]
BuildId = Annotated[
    str, Field(pattern=r"^bld_[a-z0-9]{20}$", description="A build id from `deploy`.")
]
Digest = Annotated[
    str,
    Field(
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="sha256 of the `.tar.gz` bytes, as `sha256:<64 hex>`.",
    ),
]
SizeBytes = Annotated[int, Field(gt=0, le=2**53 - 1, description="Length of the `.tar.gz`.")]
Who = Annotated[
    str,
    Field(
        pattern=r"^(usr_[a-z0-9]{20}|grp_[a-z0-9]{20}|org)$",
        description="A user (`usr_...`), a group (`grp_...`), or `org` for everyone in the org.",
    ),
]
ConnectionName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=300,
        description="The data connection's name, as the app's `ssc.toml` names it.",
    ),
]
Limit = Annotated[int, Field(ge=1, le=100, description="At most this many releases.")]
Before = Annotated[int, Field(ge=1, le=2**31 - 1, description="The previous page's `next_before`.")]
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


def fresh_key() -> str:
    return uuid4().hex


def derived_key(step: str, key: str) -> str:
    """The key of one step of ``deploy``, so re-calling with the same key replays that step."""
    return f"{step}-{hashlib.sha256(key.encode()).hexdigest()}"


def environment(app: Body, env_id: str) -> Body:
    """The app's environment with this id."""
    return next(e for e in app["environments"] if e["id"] == env_id)


async def releases_page(c: V1, app_id: str, limit: int, before: int | None) -> Body:
    """One page of ``GET /v1/apps/{id}/releases``."""
    query: dict[str, int] = {"limit": limit}
    if before is not None:
        query["before"] = before
    return await c.get(f"/v1/apps/{app_id}/releases?{urlencode(query)}")


async def list_releases_of(c: V1, ref: str, limit: int, before: int | None) -> Body:
    return await releases_page(c, (await resolve_app(c, ref))["id"], limit, before)


def pack_instructions(settings: Settings) -> Body:
    """How to pack a folder so ``complete`` accepts it."""
    return {
        "format": "gzip-compressed tar (.tar.gz) of the app folder",
        "rules": [
            "ssc.toml at the top level of the archive",
            "relative paths, regular files and directories only (no links)",
            "no .env or .env.* files, and no secret values in any file",
            "leave out node_modules/ and .git",
        ],
        "max_bytes": settings.bundle_max_bytes,
        "max_unpacked_bytes": settings.bundle_max_unpacked_bytes,
        "max_files": settings.bundle_max_files,
    }


async def release_for(c: V1, app_id: str, digest: str, preview_id: str) -> Body | None:
    """The newest release built from ``digest`` that preview may run: built for preview, or by
    no build at all. A release built for prod never is (``RELEASE_ENVIRONMENT_MISMATCH``)."""
    before: int | None = None
    while True:
        page = await releases_page(c, app_id, RELEASE_PAGE, before)
        for release in page["items"]:
            built_for = release["built_for_environment_id"]
            if release["source_digest"] == digest and built_for in (preview_id, None):
                return cast("Body", release)
        before = page["next_before"]
        if before is None:
            return None


async def deploy_release(c: V1, app: Body, preview_id: str, release: Body, key: str) -> Body:
    """``live`` when preview already runs ``release``, else start deploying it."""
    current = environment(app, preview_id)["current_deployment_id"]
    if current is not None:
        op = await c.get(f"/v1/operations/{current}")
        if op["release_id"] == release["release_id"] and op["state"] == "healthy":
            return {
                "stage": "live",
                "release": release,
                "operation": op,
                "next": "Preview runs this bundle now. Nothing more to do.",
            }
    path = f"/v1/apps/{app['id']}/environments/{preview_id}/deployments"
    body = {"release_id": release["release_id"], "kind": "deploy"}
    r = await c.post(path, body, derived_key("deploy", key))
    op_id = r.json()["operation_id"]
    return {
        "stage": "deploying",
        "release": release,
        "operation_id": op_id,
        "location": r.headers["Location"],
        "next": f"Follow it with get_status(app, operation={op_id!r}) until it is healthy.",
    }


async def upload_and_build(  # noqa: PLR0913  (keyword-only)
    c: V1, *, app_id: str, preview_id: str, digest: str, size: int, key: str
) -> Body:
    """Record and complete the bundle, then build it on preview; ``upload`` until the bytes
    are there."""
    # Fresh keys: a replayed answer would carry an upload URL that has expired.
    bundles = f"/v1/apps/{app_id}/bundles"
    r = await c.post(bundles, {"digest": digest, "size_bytes": size}, fresh_key())
    bundle: Body = r.json()
    if bundle["state"] == "pending":
        try:
            done = await c.post(f"{bundles}/{bundle['bundle_id']}/complete", {}, fresh_key())
        except Refused as e:
            if e.error["code"] != "BUNDLE_NOT_UPLOADED":
                raise
            return {
                "stage": "upload",
                "bundle_id": bundle["bundle_id"],
                "upload": bundle["upload"],
                "next": "PUT the .tar.gz bytes to upload.url with exactly upload.headers before "
                "upload.expires_at (the URL is a credential: do not log or share it), then call "
                "deploy again with the same arguments.",
            }
        bundle = done.json()
    path = f"/v1/apps/{app_id}/environments/{preview_id}/builds"
    again = (
        "call deploy again with the same arguments. If it failed, fix the source; to retry "
        "the same bundle, send a new idempotency_key."
    )
    try:
        built = await c.post(path, {"bundle_id": bundle["bundle_id"]}, derived_key("build", key))
    except Refused as e:
        if e.error["code"] != "BUILD_IN_FLIGHT":
            raise
        return {
            "stage": "building",
            "bundle_id": bundle["bundle_id"],
            "build_id": None,
            "next": f"A build of this bundle is already running. In a minute, {again}",
        }
    build: Body = built.json()
    return {
        "stage": "building",
        "bundle_id": bundle["bundle_id"],
        "build_id": build["build_id"],
        "capability_diff": build["capability_diff"],
        "next": f"Follow it with get_status(app, build={build['build_id']!r}). Once it has "
        f"succeeded, {again}",
    }


async def deploy_to_preview(  # noqa: PLR0913  (keyword-only)
    c: V1, settings: Settings, *, ref: str, digest: str | None, size: int | None, key: str
) -> Body:
    """One step of deploying a bundle to preview; the agent calls again until it is live."""
    found = await resolve_app(c, ref)
    preview_id = environment_id(found, "preview")
    base: Body = {
        "app_id": found["id"],
        "environment_id": preview_id,
        "bundle_digest": digest,
        "idempotency_key": key,
    }
    if digest is None:
        return {
            **base,
            "stage": "pack",
            "bundle": pack_instructions(settings),
            "next": "Pack the folder, compute the sha256 and length of the .tar.gz, then call "
            "deploy(app, bundle_digest='sha256:<hex>', size_bytes=<length>, "
            f"idempotency_key={key!r}).",
        }
    if size is None:
        raise Refused(
            local_error(
                "VALIDATION_FAILED",
                "Size missing.",
                "Pass size_bytes, the length of the .tar.gz, with bundle_digest.",
            )
        )
    release = await release_for(c, found["id"], digest, preview_id)
    if release is not None:
        return {**base, **await deploy_release(c, found, preview_id, release, key)}
    step = await upload_and_build(
        c, app_id=found["id"], preview_id=preview_id, digest=digest, size=size, key=key
    )
    return {**base, **step}


def subject_of(who: str) -> tuple[str, str | None]:
    """``(subject_kind, subject_id)`` for a ``Who``."""
    if who == "org":
        return "org", None
    return ("user" if who.startswith("usr_") else "group"), who


def grant_body(key: GrantKey) -> Body:
    role, kind, subject = key
    return {"role": role, "subject_kind": kind, "subject_id": subject}


async def ask_share(  # noqa: PLR0913  (keyword-only)
    c: V1, *, ref: str, env: str, who: str, role: str | None, key: str
) -> Body:
    """Ask for ``who`` to get ``role`` on ``env``: an ``agent_share`` approval request for the
    current grants plus that one. Never changes the grants."""
    found = await resolve_app(c, ref)
    env_id = environment_id(found, env)
    kind, subject = subject_of(who)
    wanted: GrantKey = (role or grant_rules.floor_of(env), kind, subject)
    problems = grant_rules.validate(env, [wanted])
    if problems:
        raise Refused(
            local_error(
                "VALIDATION_FAILED",
                "Below the environment's floor.",
                f"A {wanted[0]!r} grant on {env} gives no access; preview is for builders.",
            )
        )
    attempt = 0
    while True:
        current = await c.get(f"/v1/apps/{found['id']}/environments/{env_id}/grants")
        version = current["grants_version"]
        existing: list[GrantKey] = [
            (g["role"], g["subject_kind"], g["subject_id"]) for g in current["grants"]
        ]
        if wanted in existing:
            return {
                "requested": False,
                "environment_id": env_id,
                "grants_version": version,
                "next": f"{who} already has {wanted[0]} on {env}. Nothing to ask for.",
            }
        desired = [g for g in existing if (g[1], g[2]) != (kind, subject)] + [wanted]
        body = {
            "environment_id": env_id,
            "kind": "agent_share",
            "payload": {"grants_version": version, "grants": [grant_body(g) for g in desired]},
        }
        try:
            r = await c.post("/v1/approvals", body, key)
        except Refused as e:
            attempt += 1
            if e.error["code"] != "PRECONDITION_STALE" or attempt == SHARE_ATTEMPTS:
                raise
            continue
        return {
            "requested": True,
            "created": r.status_code == 201,
            "approval": r.json(),
            "grants_version": version,
            "not_requested": ["widen_audience"],
            "next": "Pending: another active admin of the org must approve; SSC staff record "
            "the decision. Nothing has changed. Once approved, the change is applied by "
            f"`ssc share` (or PUT grants) at grants_version {version}. If the app uses a data "
            "connection, widening its audience also needs a widen_audience approval, which "
            "applying the change asks for.",
        }


async def ask_connection(c: V1, *, ref: str, connection: str, key: str) -> Body:
    """Ask for the app's prod environment to use one data connection."""
    found = await resolve_app(c, ref)
    body = {
        "environment_id": environment_id(found, "prod"),
        "kind": "connect_data_source",
        "subject_key": connection,
    }
    r = await c.post("/v1/approvals", body, key)
    return {
        "requested": True,
        "created": r.status_code == 201,
        "approval": r.json(),
        "next": "Pending: another active admin of the org must approve; SSC staff record the "
        "decision. Nothing has changed.",
    }


def register(server: MCPServer, api: FastAPI, settings: Settings) -> None:
    read = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    ask = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )

    async def list_apps(ctx: Context) -> CallToolResult:
        """List the apps in your org that you can see: id, slug, owner and status."""
        return await run(api, ctx, lambda c: c.get("/v1/apps"))

    async def get_app(app: AppRef, ctx: Context) -> CallToolResult:
        """One app with its environments (`prod` and `preview`), their config and sharing
        versions and the deployment each one runs."""
        return await run(api, ctx, lambda c: resolve_app(c, app))

    async def get_status(
        app: AppRef,
        ctx: Context,
        operation: OperationId | None = None,
        build: BuildId | None = None,
    ) -> CallToolResult:
        """What an app runs now: the app, and for each environment the operation that deployed
        its current release (null when nothing runs). Pass `operation` to follow one started by
        `rollback` or `deploy`: pending, running, healthy, failed or superseded. Pass `build` to
        follow one started by `deploy`: queued, running, succeeded (with its release) or
        failed (with a failure code)."""

        async def work(c: V1) -> Body:
            found = await resolve_app(c, app)
            current: dict[str, Body | None] = {}
            for env in found["environments"]:
                dep = env["current_deployment_id"]
                current[env["name"]] = await c.get(f"/v1/operations/{dep}") if dep else None
            out: Body = {"app": found, "current": current}
            if operation is not None:
                out["operation"] = await c.get(f"/v1/operations/{operation}")
            if build is not None:
                out["build"] = await c.get(f"/v1/builds/{build}")
            return out

        return await run(api, ctx, work)

    async def list_releases(
        app: AppRef, ctx: Context, limit: Limit = 50, before: Before | None = None
    ) -> CallToolResult:
        """The app's releases, highest number first: id, number, the bundle digest it was built
        from and the environment it was built for. Pass `next_before` as `before` for more."""
        return await run(api, ctx, lambda c: list_releases_of(c, app, limit, before))

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
        key = idempotency_key or fresh_key()

        async def work(c: V1) -> Body:
            found = await resolve_app(c, app)
            path = f"/v1/apps/{found['id']}/environments/{environment_id(found, env)}/deployments"
            r = await c.post(path, {"release_id": release, "kind": "rollback"}, key)
            body: Body = r.json()
            return {**body, "location": r.headers["Location"], "idempotency_key": key}

        return await run(api, ctx, work)

    async def deploy(
        app: AppRef,
        ctx: Context,
        bundle_digest: Digest | None = None,
        size_bytes: SizeBytes | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> CallToolResult:
        """Deploy a folder to the app's preview environment (never prod), one step per call.
        Without `bundle_digest`: how to pack the folder. With it (and `size_bytes`): uploads,
        builds and deploys, answering with a `stage` (`upload`, `building`, `deploying` or
        `live`) and what to do `next`. Call again with the same arguments and the returned
        `idempotency_key` after each step; nothing starts twice."""
        key = idempotency_key or fresh_key()
        return await run(
            api,
            ctx,
            lambda c: deploy_to_preview(
                c, settings, ref=app, digest=bundle_digest, size=size_bytes, key=key
            ),
        )

    async def request_share(  # noqa: PLR0913, PLR0917  (each parameter is a tool argument)
        app: AppRef,
        env: Literal["prod", "preview"],
        who: Who,
        ctx: Context,
        role: Literal["user", "builder"] | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> CallToolResult:
        """Ask for someone to get access to one environment. `role` defaults to `user` on prod
        and `builder` on preview (a preview `user` grant gives no access). This only opens an
        approval request, which another admin of the org must approve; the sharing rules stay
        as they are until then."""
        key = idempotency_key or fresh_key()
        return await run(
            api, ctx, lambda c: ask_share(c, ref=app, env=env, who=who, role=role, key=key)
        )

    async def request_connection(
        app: AppRef,
        connection: ConnectionName,
        ctx: Context,
        idempotency_key: IdempotencyKey | None = None,
    ) -> CallToolResult:
        """Ask for the app's prod environment to use a data connection. This only opens an
        approval request, which another admin of the org must approve."""
        key = idempotency_key or fresh_key()
        return await run(
            api, ctx, lambda c: ask_connection(c, ref=app, connection=connection, key=key)
        )

    server.add_tool(list_apps, annotations=read)
    server.add_tool(get_app, annotations=read)
    server.add_tool(get_status, annotations=read)
    server.add_tool(list_releases, annotations=read)
    server.add_tool(
        rollback,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    server.add_tool(
        deploy,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    server.add_tool(request_share, annotations=ask)
    server.add_tool(request_connection, annotations=ask)
