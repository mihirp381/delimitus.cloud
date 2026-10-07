"""The auth host, ``auth.delimitus.com`` (SSC-019, decision 024).

Browser sign-in for app hosts: the gateway sends the person to ``/login?org&return_to&binding``.
The auth host checks ``return_to`` is an app host of the org's cell, sends the person to WorkOS SSO
for the org's WorkOS organisation, and on ``/callback`` checks the profile (organisation,
connection, connection type, subject) and finds the person (``join``). It then opens a browser
session (12 hours, never extended) and hands the app host a one-time code at
``https://<host>/.ssc/callback?code&next``; the gateway redeems it over ``/internal/redeem``. A
person with a live browser session skips WorkOS on the next app host.

Command line: RFC 8628 device authorisation (``/device/authorize``, ``/device``, ``/token``) and
RFC 7009 revocation (``/revoke``). Tokens: ``tokens``. A login for a coding agent (SSC-048) names
it in ``/device/authorize`` (``agent``); the person confirms that agent by name before single
sign-on, and every access token of the session carries ``agent: true`` and its ``client_id``.

OAuth 2.1 for remote MCP clients and the console (decision 029): ``authorize``. Its sign-in
rides the same ``/callback`` with ``flow: oauth``, and ``/token`` takes its
``authorization_code`` grant. A refreshed access token keeps its session's audience. The
console, on its own origin, may call ``/token`` and ``/revoke`` cross-origin (CORS for exactly
``console_url``, no credentials); nothing else on this host answers CORS.

Every refused sign-in shows the same page (``pages.REFUSED``); the reason is logged and audited
as ``login.failed``. Login state lives in a signed, ten-minute cookie; the WorkOS ``state`` is only
its random check value.
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, cast
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_control.audit.chain import Actor, NewEvent, append_event
from ssc_control.db import bound_org, check_org_id
from ssc_control.identity import connections, join, pages, sessions, tokens
from ssc_control.identity.authorize import DEVICE_GRANT, Kit, OAuthFlow
from ssc_control.identity.cell_callers import CallerCheck
from ssc_control.identity.rules import LoginRefusal, ProfileError, parse_return_to
from ssc_control.identity.settings import AuthSettings
from ssc_control.identity.workos import WorkOSClient, WorkOSError
from ssc_shared.hosts import cell_project

log = logging.getLogger("ssc.auth")

LOGIN_SECONDS: Final = 600
_BINDING_LENGTH: Final = 43
_WORKOS_ERROR: Final = re.compile(r"[a-z_]{1,64}")
CORS_PATHS: Final = frozenset({"/token", "/revoke"})
"""The only paths the console calls cross-origin."""
_ORG_CELL = text("select cell_label from ssc.org where id = :org")

Flow = Literal["browser", "device", "oauth"]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class Sealer:
    """HMAC-SHA256 signed JSON with an expiry, one purpose per use."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def _mac(self, purpose: str, body: str) -> str:
        return _b64(hmac.new(self._key, f"{purpose}.{body}".encode(), hashlib.sha256).digest())

    def seal(self, purpose: str, payload: Mapping[str, object], exp: int) -> str:
        body = _b64(json.dumps({**payload, "exp": exp}, separators=(",", ":")).encode())
        return f"{body}.{self._mac(purpose, body)}"

    def open(self, purpose: str, value: str, now: int) -> dict[str, Any] | None:
        body, _, mac = value.partition(".")
        if not body or not hmac.compare_digest(mac, self._mac(purpose, body)):
            return None
        try:
            data = json.loads(_unb64(body))
        except ValueError, binascii.Error:
            return None
        if not isinstance(data, dict):
            return None
        payload = cast(dict[str, Any], data)
        exp = payload.get("exp")
        return payload if isinstance(exp, int) and exp > now else None


@dataclass(frozen=True, slots=True)
class AuthHost:
    settings: AuthSettings
    engine: AsyncEngine
    workos: WorkOSClient
    signer: tokens.Signer
    callers: CallerCheck
    clock: Callable[[], float] = time.time


def _binding(raw: str) -> bytes | None:
    if len(raw) != _BINDING_LENGTH:
        return None
    try:
        value = _unb64(raw)
    except ValueError, binascii.Error:
        return None
    return value if len(value) == 32 else None  # noqa: PLR2004


def _org(raw: str) -> str | None:
    try:
        return check_org_id(raw)
    except ValueError:
        return None


def _form(body: bytes) -> dict[str, str]:
    try:
        parsed = parse_qs(body.decode(), max_num_fields=10)
    except UnicodeDecodeError, ValueError:
        return {}
    return {k: v[0] for k, v in parsed.items() if len(v) == 1}


def _oauth_error(error: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status, headers={"cache-control": "no-store"})


def create_auth_app(host: AuthHost) -> FastAPI:  # noqa: C901, PLR0915  (one route table)
    s = host.settings
    sealer = Sealer(s.state_key)
    secure = s.secure_cookies
    login_cookie = "__Host-ssc-login" if secure else "ssc-login"
    session_cookie = "__Host-ssc-auth" if secure else "ssc-auth"
    headers = pages.headers(s.workos_base)
    app = FastAPI(title="ssc-auth", docs_url=None, redoc_url=None, openapi_url=None)

    def now() -> int:
        return int(host.clock())

    def html(body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(body, status_code=status, headers=headers)

    def refused() -> HTMLResponse:
        return html(pages.REFUSED, 403)

    def bad_request() -> HTMLResponse:
        return html(pages.BAD_REQUEST, 400)

    def set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name, value, max_age=max_age, path="/", secure=secure, httponly=True, samesite="lax"
        )

    def clear(response: Response, name: str) -> None:
        response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="lax")

    async def audit_failure(
        conn: AsyncConnection, connection: connections.DirectoryConnection, reason: str
    ) -> None:
        await append_event(
            conn,
            NewEvent(
                org_id=connection.org_id,
                action=AuditAction.LOGIN_FAILED,
                actor=Actor(kind=ActorKind.INTEGRATION, id=connection.id),
                target_kind="directory_connection",
                target_id=connection.id,
                after={"reason": reason},
            ),
        )

    async def hand_back(
        conn: AsyncConnection, org_id: str, session_id: str, login: Mapping[str, Any]
    ) -> RedirectResponse:
        host_name, path = str(login["host"]), str(login["path"])
        code = await sessions.issue_code(
            conn,
            org_id,
            session_id=session_id,
            host=host_name,
            binding_hash=_unb64(login["binding"]),
        )
        query = urlencode({"code": code, "next": path})
        return RedirectResponse(f"https://{host_name}/.ssc/callback?{query}", status_code=302)

    def to_workos(
        connection: connections.DirectoryConnection, login: dict[str, Any]
    ) -> RedirectResponse:
        check = secrets.token_urlsafe(24)
        url = host.workos.authorize_url(
            organization=connection.workos_organization_id,
            redirect_uri=f"{s.auth_url}/callback",
            state=check,
        )
        response = RedirectResponse(url, status_code=302, headers={"cache-control": "no-store"})
        sealed = sealer.seal("login", {**login, "check": check}, now() + LOGIN_SECONDS)
        set_cookie(response, login_cookie, sealed, LOGIN_SECONDS)
        return response

    def token_reply(  # noqa: PLR0913, PLR0917  (the token's claims)
        org_id: str, user_id: str, sid: str, refresh: str, agent: str | None, audience: str
    ) -> JSONResponse:
        access = host.signer.access_token(
            org_id=org_id,
            user_id=user_id,
            session_id=sid,
            audience=audience,
            now=tokens.utcnow(),
            agent_client_id=agent,
        )
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": tokens.ACCESS_SECONDS,
                "refresh_token": refresh,
            },
            headers={"cache-control": "no-store"},
        )

    oauth_flow = OAuthFlow(
        Kit(
            settings=s,
            engine=host.engine,
            workos=host.workos,
            sealer=sealer,
            clock=host.clock,
            to_workos=to_workos,
            set_cookie=set_cookie,
            clear=clear,
            token_reply=token_reply,
            session_cookie=session_cookie,
            consent_cookie="__Host-ssc-consent" if secure else "ssc-consent",
        )
    )
    oauth_flow.add_routes(app)

    @app.middleware("http")
    async def console_cors(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """The console calls ``/token`` and ``/revoke`` from its own origin (decision 029): CORS
        for exactly ``console_url``, without credentials, on those two paths only."""
        if request.url.path not in CORS_PATHS:
            return await call_next(request)
        allowed = request.headers.get("origin") == s.console_url
        if request.method == "OPTIONS":
            asked = request.headers.get("access-control-request-method", "")
            if not allowed or asked != "POST":
                return Response(status_code=400, headers={"vary": "Origin"})
            return Response(
                status_code=204,
                headers={
                    "access-control-allow-origin": s.console_url,
                    "access-control-allow-methods": "POST",
                    "access-control-allow-headers": "content-type",
                    "access-control-max-age": "600",
                    "vary": "Origin",
                },
            )
        response = await call_next(request)
        response.headers.append("vary", "Origin")
        if allowed:
            response.headers["access-control-allow-origin"] = s.console_url
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/.well-known/jwks.json")
    def jwks() -> JSONResponse:
        return JSONResponse(host.signer.jwks(), headers={"cache-control": "max-age=300"})

    @app.get("/login")
    async def login(request: Request) -> Response:
        q = request.query_params
        org_id, binding = _org(q.get("org", "")), q.get("binding", "")
        if org_id is None or _binding(binding) is None:
            return bad_request()
        async with bound_org(host.engine, org_id) as conn:
            connection = await connections.load(conn, org_id)
            cell = (await conn.execute(_ORG_CELL, {"org": org_id})).one_or_none()
            if connection is None or connection.frozen or cell is None or cell[0] is None:
                log.info("login refused for %s: no active connection or cell", org_id)
                return bad_request()
            target = parse_return_to(
                q.get("return_to", ""), apps_domain=s.apps_domain, cell_label=str(cell[0])
            )
            if target is None:
                return bad_request()
            flow = {
                "flow": "browser",
                "org": org_id,
                "host": target.host,
                "path": target.path,
                "binding": binding,
            }
            existing = sealer.open("session", request.cookies.get(session_cookie, ""), now())
            if existing is not None and existing.get("org") == org_id:
                live = await sessions.live_session(conn, org_id, str(existing.get("sid")))
                if live is not None and live.kind == "browser":
                    return await hand_back(conn, org_id, live.id, flow)
        return to_workos(connection, flow)

    @app.get("/callback")
    async def callback(request: Request) -> Response:
        login = sealer.open("login", request.cookies.get(login_cookie, ""), now())
        state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
        error = request.query_params.get("error", "")
        if login is not None and error and hmac.compare_digest(state, str(login.get("check"))):
            shown = error if _WORKOS_ERROR.fullmatch(error) else "unrecognised"
            log.warning("WorkOS refused the sign-in for %s: %s", login.get("org"), shown)
            response = refused()
            clear(response, login_cookie)
            return response
        if login is None or not code or not hmac.compare_digest(state, str(login.get("check"))):
            return bad_request()
        org_id = _org(str(login.get("org", "")))
        if org_id is None:
            return bad_request()
        try:
            profile = await host.workos.profile(code)
        except (WorkOSError, ProfileError) as e:
            log.warning("WorkOS profile exchange failed for %s: %s", org_id, e)
            return refused()
        async with bound_org(host.engine, org_id) as conn:
            connection = await connections.load(conn, org_id)
            if connection is None or connection.frozen:
                return refused()
            found = await join.find_person(conn, connection, profile)
            reason: LoginRefusal | None = found if isinstance(found, str) else None
            if not isinstance(found, str) and not found.active:
                reason = "not_active"
            if reason is not None or isinstance(found, str):
                await audit_failure(conn, connection, reason or "no_match")
                log.info("login refused for %s: %s", org_id, reason)
                response: Response = refused()
            elif login.get("flow") == "oauth":
                response = await oauth_flow.finish(
                    conn,
                    org_id,
                    user_id=found.user_id,
                    connection_id=profile.connection_id,
                    login=login,
                )
            else:
                kind = "cli" if login.get("flow") == "device" else "browser"
                agent = login.get("agent") if kind == "cli" else None
                session_id = await sessions.open_session(
                    conn,
                    org_id,
                    user_id=found.user_id,
                    kind=kind,
                    connection_id=profile.connection_id,
                    actor=Actor(kind=ActorKind.USER, id=found.user_id),
                    agent_client_id=agent if isinstance(agent, str) else None,
                )
                if kind == "cli":
                    approved = await tokens.decide_grant(
                        conn, org_id, str(login.get("grant")), session_id=session_id
                    )
                    response = html(pages.DEVICE_DONE) if approved else bad_request()
                else:
                    response = await hand_back(conn, org_id, session_id, login)
                    sealed = sealer.seal(
                        "session",
                        {"org": org_id, "sid": session_id},
                        now() + sessions.SESSION_SECONDS,
                    )
                    set_cookie(response, session_cookie, sealed, sessions.SESSION_SECONDS)
        clear(response, login_cookie)
        return response

    @app.get("/logout")
    async def logout(request: Request) -> Response:
        existing = sealer.open("session", request.cookies.get(session_cookie, ""), now())
        org_id = None if existing is None else _org(str(existing.get("org", "")))
        if existing is not None and org_id is not None:
            sid = str(existing.get("sid"))
            async with bound_org(host.engine, org_id) as conn:
                live = await sessions.live_session(conn, org_id, sid)
                if live is not None:
                    await sessions.revoke_session(
                        conn, org_id, sid, "logout", actor=Actor(ActorKind.USER, live.user_id)
                    )
        response = html(pages.SIGNED_OUT)
        clear(response, session_cookie)
        return response

    # ── command line (RFC 8628, RFC 7009) ─────────────────────────────────────

    @app.post("/device/authorize")
    async def device_authorize(request: Request) -> JSONResponse:
        form = _form(await request.body())
        org_id, agent = _org(form.get("org", "")), form.get("agent")
        if org_id is None or (agent is not None and not tokens.AGENT_CLIENT.fullmatch(agent)):
            return _oauth_error("invalid_request")
        async with bound_org(host.engine, org_id) as conn:
            connection = await connections.load(conn, org_id)
            if connection is None or connection.frozen:
                return _oauth_error("invalid_request")
            start = await tokens.start_device(conn, org_id, agent)
        verify = f"{s.auth_url}/device?{urlencode({'org': org_id})}"
        complete = f"{verify}&{urlencode({'user_code': start.user_code})}"
        return JSONResponse(
            {
                "device_code": start.device_code,
                "user_code": start.user_code,
                "verification_uri": verify,
                "verification_uri_complete": complete,
                "expires_in": start.expires_in,
                "interval": start.interval,
            },
            headers={"cache-control": "no-store"},
        )

    @app.get("/device")
    def device_page(request: Request) -> Response:
        org_id = _org(request.query_params.get("org", ""))
        if org_id is None:
            return bad_request()
        code = tokens.normal_user_code(request.query_params.get("user_code", ""))
        return html(pages.device_form(org_id, code))

    @app.post("/device")
    async def device_continue(request: Request) -> Response:
        if request.headers.get("sec-fetch-site", "same-origin") not in {"same-origin", "none"}:
            return bad_request()
        form = _form(await request.body())
        org_id = _org(form.get("org", ""))
        if org_id is None:
            return bad_request()
        async with bound_org(host.engine, org_id) as conn:
            connection = await connections.load(conn, org_id)
            grant = await tokens.pending_grant(conn, org_id, form.get("user_code", ""))
            agent = None if grant is None else await tokens.grant_agent(conn, org_id, grant)
        if connection is None or connection.frozen or grant is None:
            return bad_request()
        if agent is not None and form.get("agent") != agent:
            code = tokens.normal_user_code(form.get("user_code", ""))
            return html(pages.agent_consent(org_id, code, agent))
        login = {"flow": "device", "org": org_id, "grant": grant, "agent": agent}
        return to_workos(connection, login)

    async def device_token(device_code: str) -> JSONResponse:
        parsed = tokens.org_of(device_code)
        if parsed is None:
            return _oauth_error("invalid_grant")
        org_id = parsed[0]
        async with bound_org(host.engine, org_id) as conn:
            polled = await tokens.poll_device(conn, org_id, device_code)
            live = None
            if polled.startswith("ses_"):
                live = await sessions.live_session(conn, org_id, polled)
            if live is None:
                return _oauth_error(polled if not polled.startswith("ses_") else "access_denied")
            issued: dict[str, Any] = {"kind": "cli", "via": "device"}
            if live.agent_client_id is not None:
                issued["client_id"] = live.agent_client_id
            await append_event(
                conn,
                NewEvent(
                    org_id=org_id,
                    action=AuditAction.TOKEN_ISSUED,
                    actor=Actor(ActorKind.USER, live.user_id),
                    target_kind="auth_session",
                    target_id=live.id,
                    after=issued,
                ),
            )
            refresh = await tokens.issue_refresh(conn, org_id, live.id)
        return token_reply(
            org_id, live.user_id, live.id, refresh, live.agent_client_id, s.api_audience
        )

    async def refresh_token(raw: str) -> JSONResponse:
        parsed = tokens.org_of(raw, tokens.REFRESH_PREFIX)
        if parsed is None:
            return _oauth_error("invalid_grant")
        org_id = parsed[0]
        async with bound_org(host.engine, org_id) as conn:
            done = await tokens.rotate_refresh(
                conn, org_id, raw, actor=Actor(ActorKind.INTEGRATION, "refresh")
            )
        if done is None:
            return _oauth_error("invalid_grant")
        return token_reply(
            org_id,
            done.user_id,
            done.session_id,
            done.refresh_token,
            done.agent_client_id,
            done.token_audience or s.api_audience,
        )

    @app.post("/token")
    async def token(request: Request) -> JSONResponse:
        form = _form(await request.body())
        grant_type = form.get("grant_type")
        if grant_type == DEVICE_GRANT:
            return await device_token(form.get("device_code", ""))
        if grant_type == "refresh_token":
            return await refresh_token(form.get("refresh_token", ""))
        if grant_type == "authorization_code":
            return await oauth_flow.code_token(form)
        return _oauth_error("unsupported_grant_type")

    @app.post("/revoke")
    async def revoke(request: Request) -> Response:
        raw = _form(await request.body()).get("token", "")
        parsed = tokens.org_of(raw, tokens.REFRESH_PREFIX)
        if parsed is not None:
            org_id = parsed[0]
            async with bound_org(host.engine, org_id) as conn:
                done = await tokens.rotate_refresh(
                    conn, org_id, raw, actor=Actor(ActorKind.INTEGRATION, "refresh")
                )
                if done is not None:
                    await sessions.revoke_session(
                        conn,
                        org_id,
                        done.session_id,
                        "logout",
                        actor=Actor(ActorKind.USER, done.user_id),
                    )
        return Response(status_code=200, headers={"cache-control": "no-store"})

    # ── the gateway (SSC-018) ─────────────────────────────────────────────────

    @app.post("/internal/redeem")
    async def redeem(request: Request) -> JSONResponse:
        scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
        caller = await host.callers.caller(bearer.strip()) if scheme.lower() == "bearer" else None
        if caller is None:
            return _oauth_error("unauthorized", 401)
        try:
            body = cast(dict[str, object], json.loads(await request.body()))
            org_id = _org(str(body["org"]))
            code, host_name, nonce = str(body["code"]), str(body["host"]), str(body["nonce"])
        except ValueError, KeyError, TypeError:
            return _oauth_error("invalid_request")
        if org_id is None or not code or not nonce:
            return _oauth_error("invalid_request")
        async with bound_org(host.engine, org_id) as conn:
            cell = (await conn.execute(_ORG_CELL, {"org": org_id})).one_or_none()
            if cell is None or (
                caller.project is not None and cell_project(str(cell[0])) != caller.project
            ):
                log.warning("redeem for %s refused: caller project mismatch", org_id)
                return _oauth_error("unauthorized", 401)
            done = await sessions.redeem_code(conn, org_id, code=code, host=host_name, nonce=nonce)
        if done is None:
            return _oauth_error("invalid_grant")
        return JSONResponse(
            {
                "sub": done.user_id,
                "org": org_id,
                "name": done.display_name,
                "email": done.email,
                "iat": done.iat,
                "exp": done.exp,
            },
            headers={"cache-control": "no-store"},
        )

    return app
