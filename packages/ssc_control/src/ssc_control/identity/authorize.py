"""The auth host's OAuth 2.1 authorization server, for remote MCP clients and the console
(decision 029). :func:`OAuthFlow.add_routes` puts these on the auth host:

* ``GET /.well-known/oauth-authorization-server``: RFC 8414 metadata. The API's MCP
  protected-resource metadata names this host as its authorization server.
* ``POST /register``: RFC 7591 registration of a public client (no secret), ten an hour per
  address. Its redirect URIs follow ``oauth.redirect_uri_problem``.
* ``GET /authorize``: checks the client and redirect URI first; either wrong shows
  ``pages.UNKNOWN_CLIENT`` and never redirects. Then ``response_type=code``, a ``state``, an S256
  ``code_challenge`` (``plain`` is refused) and the ``resource`` (RFC 8707): the MCP endpoint for
  a registered client, the API's user audience for the console. Anything wrong from here on goes
  back to the client as ``error`` with ``state`` and ``iss`` (RFC 9207).
* Which org: a live auth-host session cookie names it; otherwise the person types a work email.
  Its domain is looked up in WorkOS (verified domains only) and the WorkOS organisation in
  ``ssc.org_for_workos_organization``, which answers only for an active connection. Every miss,
  whatever the reason, gets ``pages.NO_SIGN_IN`` by the same path. Twenty tries an hour per
  address and per domain.
* WorkOS single sign-on through ``/callback``, the pending request riding in the sealed login
  cookie (``flow: oauth``). Then :meth:`OAuthFlow.finish`: the console goes straight back with a
  code; a registered client first gets a consent page (``POST /authorize/consent``, answered
  only with the sealed answer token and the consent cookie it is bound to).
* ``/token`` with ``authorization_code``: :meth:`OAuthFlow.code_token`. A code works once; a code
  presented again revokes the session it opened (``code_reuse``) and is audited.

An approved MCP client gets a ``cli`` session for its own agent name (``oauth.agent_slug``)
whose tokens are for the MCP endpoint only; the console gets a ``console`` session whose tokens
are for ``/v1``.
"""

import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_control.audit.chain import Actor, NewEvent, append_event
from ssc_control.db import bound_org, check_org_id
from ssc_control.identity import connections, oauth, pages, sessions, tokens
from ssc_control.identity.limits import (
    EMAIL_PER_HOUR,
    REGISTER_PER_HOUR,
    RateLimit,
    client_address,
)
from ssc_control.identity.settings import AuthSettings
from ssc_control.identity.workos import WorkOSClient, WorkOSError

log = logging.getLogger("ssc.auth")

AUTHORIZE_SECONDS: Final = 600
CONSENT_SECONDS: Final = 600
MAX_REGISTRATION_BYTES: Final = 16_384
GRANT_TYPES: Final = ("authorization_code", "refresh_token")
DEVICE_GRANT: Final = "urn:ietf:params:oauth:grant-type:device_code"
UNNAMED_CLIENT: Final = "Unnamed MCP client"
PENDING: Final = ("client_id", "redirect_uri", "state", "challenge", "resource")
"""What a pending authorization carries through the email step, WorkOS and consent."""
_NO_ORGANIZATION: Final = "org_none"
"""Looked up when WorkOS knows no organisation, so a miss takes the same path as a hit."""
_DOMAIN: Final = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_FIND_ORG = text("select ssc.org_for_workos_organization(:workos)")
_ORG_NAME = text("select name from ssc.org where id = :org")


class Seals(Protocol):
    def seal(self, purpose: str, payload: Mapping[str, object], exp: int) -> str: ...
    def open(self, purpose: str, value: str, now: int) -> dict[str, Any] | None: ...


class TokenReply(Protocol):
    def __call__(  # noqa: PLR0913, PLR0917  (the token's claims)
        self,
        org_id: str,
        user_id: str,
        sid: str,
        refresh: str,
        agent: str | None,
        audience: str,
    ) -> JSONResponse: ...


class ToWorkos(Protocol):
    def __call__(
        self,
        connection: connections.DirectoryConnection,
        login: dict[str, Any],
        *,
        from_form: bool = False,
    ) -> Response: ...


@dataclass(frozen=True, slots=True)
class Kit:
    """What the auth host lends this flow."""

    settings: AuthSettings
    engine: AsyncEngine
    workos: WorkOSClient
    sealer: Seals
    clock: Callable[[], float]
    to_workos: ToWorkos
    set_cookie: Callable[[Response, str, str, int], None]
    clear: Callable[[Response, str], None]
    token_reply: TokenReply
    session_cookie: str
    consent_cookie: str


def metadata(auth_url: str) -> dict[str, object]:
    """RFC 8414: what a client needs to find every endpoint and what each accepts."""
    return {
        "issuer": auth_url,
        "authorization_endpoint": f"{auth_url}/authorize",
        "token_endpoint": f"{auth_url}/token",
        "registration_endpoint": f"{auth_url}/register",
        "revocation_endpoint": f"{auth_url}/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": [*GRANT_TYPES, DEVICE_GRANT],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "authorization_response_iss_parameter_supported": True,
    }


@dataclass(frozen=True, slots=True)
class Registration:
    client_name: str
    redirect_uris: tuple[str, ...]


def registration(body: object) -> Registration | tuple[str, str]:  # noqa: C901, PLR0911
    """The client a registration asks for, or ``(error, description)`` (RFC 7591 section 3.2.2)."""
    if not isinstance(body, dict):
        return "invalid_client_metadata", "the body is a JSON object"
    fields: dict[str, object] = {str(k): v for k, v in body.items()}  # pyright: ignore[reportUnknownVariableType]
    uris = fields.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= oauth.MAX_REDIRECT_URIS:  # pyright: ignore[reportUnknownArgumentType]
        return "invalid_redirect_uri", "redirect_uris lists 1 to 10 URIs"
    checked: list[str] = []
    for uri in uris:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(uri, str):
            return "invalid_redirect_uri", "a redirect URI is a string"
        problem = oauth.redirect_uri_problem(uri)
        if problem is not None:
            return "invalid_redirect_uri", problem
        checked.append(uri)
    name = fields.get("client_name", UNNAMED_CLIENT)
    if (
        not isinstance(name, str)
        or not 1 <= len(name.strip()) <= oauth.MAX_CLIENT_NAME
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)  # noqa: PLR2004
    ):
        return "invalid_client_metadata", "client_name is 1 to 100 characters"
    if fields.get("token_endpoint_auth_method", "none") != "none":
        return "invalid_client_metadata", "token_endpoint_auth_method is none: a public client"
    grants = fields.get("grant_types", list(GRANT_TYPES))
    if (
        not isinstance(grants, list)
        or "authorization_code" not in grants
        or any(g not in GRANT_TYPES for g in grants)  # pyright: ignore[reportUnknownVariableType]
    ):
        return "invalid_client_metadata", "grant_types is authorization_code and refresh_token"
    if fields.get("response_types", ["code"]) != ["code"]:
        return "invalid_client_metadata", "response_types is code"
    return Registration(name.strip(), tuple(checked))


def email_domain(raw: str) -> str | None:
    """The domain of a work email, lower-cased, or None when it is not one."""
    local, at, domain = raw.strip().lower().rpartition("@")
    if not at or not local or len(local) > 64 or not _DOMAIN.fullmatch(domain):  # noqa: PLR2004
        return None
    return domain


def same_resource(presented: str, expected: str) -> bool:
    """RFC 8707 resources compared as URLs: a trailing slash makes no difference."""
    return bool(presented) and presented.rstrip("/") == expected.rstrip("/")


def with_query(uri: str, params: Mapping[str, str]) -> str:
    return f"{uri}{'&' if '?' in uri else '?'}{urlencode(params)}"


def _form(body: bytes) -> dict[str, str]:
    try:
        parsed = parse_qs(body.decode(), max_num_fields=10)
    except UnicodeDecodeError, ValueError:
        return {}
    return {k: v[0] for k, v in parsed.items() if len(v) == 1}


def _org(raw: object) -> str | None:
    try:
        return check_org_id(str(raw))
    except ValueError:
        return None


def _hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def _token_error(error: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status, headers={"cache-control": "no-store"})


def _same_site(request: Request) -> bool:
    return request.headers.get("sec-fetch-site", "same-origin") in {"same-origin", "none"}


class OAuthFlow:
    def __init__(self, kit: Kit) -> None:
        self._kit = kit
        self._s = kit.settings
        self._registrations = RateLimit(kit.clock, REGISTER_PER_HOUR)
        self._emails_by_address = RateLimit(kit.clock, EMAIL_PER_HOUR)
        self._emails_by_domain = RateLimit(kit.clock, EMAIL_PER_HOUR)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _now(self) -> int:
        return int(self._kit.clock())

    def _html(self, body: str, status: int = 200, *, answer_to: str = "") -> HTMLResponse:
        headers = pages.headers(self._s.workos_base, answer_to=answer_to)
        return HTMLResponse(body, status_code=status, headers=headers)

    def _address(self, request: Request) -> str:
        peer = request.client.host if request.client is not None else None
        forwarded = request.headers.get("x-forwarded-for")
        return client_address(forwarded, peer, self._s.trusted_hops)

    def _back(self, redirect_uri: str, params: Mapping[str, str]) -> RedirectResponse:
        """To the client, with ``iss`` (RFC 9207) on every answer."""
        query = {**{k: v for k, v in params.items() if v}, "iss": self._s.auth_url}
        return RedirectResponse(
            with_query(redirect_uri, query), status_code=302, headers={"cache-control": "no-store"}
        )

    def _resource(self, client: oauth.Client) -> str:
        return self._s.api_audience if client.first_party else self._s.mcp_resource

    async def _client(self, client_id: str) -> oauth.Client | None:
        """``ssc.oauth_client`` is global (no org, no RLS): read without a bind."""
        async with self._kit.engine.connect() as conn:
            client = await oauth.load_client(conn, client_id, self._s.console_url)
            await conn.rollback()
        return client

    async def _remembered(self, request: Request) -> connections.DirectoryConnection | None:
        """The org of a live auth-host session in this browser, if its connection is active."""
        kit = self._kit
        existing = kit.sealer.open(
            "session", request.cookies.get(kit.session_cookie, ""), self._now()
        )
        org_id = None if existing is None else _org(existing.get("org", ""))
        if existing is None or org_id is None:
            return None
        async with bound_org(kit.engine, org_id) as conn:
            live = await sessions.live_session(conn, org_id, str(existing.get("sid")))
            connection = await connections.load(conn, org_id)
        if live is None or connection is None or connection.frozen:
            return None
        return connection

    async def _org_for(
        self, organizations: Sequence[str]
    ) -> connections.DirectoryConnection | None:
        """The active connection of the org one of these WorkOS organisations belongs to."""
        found: str | None = None
        async with self._kit.engine.connect() as conn:
            for workos_id in organizations or [_NO_ORGANIZATION]:
                found = (await conn.execute(_FIND_ORG, {"workos": workos_id})).scalar_one_or_none()
                if found is not None:
                    break
            await conn.rollback()
        if found is None:
            return None
        async with bound_org(self._kit.engine, found) as conn:
            connection = await connections.load(conn, found)
        if connection is None or connection.frozen:
            return None
        return connection

    async def _approve(  # noqa: PLR0913  (keyword-only)
        self,
        conn: AsyncConnection,
        org_id: str,
        *,
        user_id: str,
        session_id: str,
        client: oauth.Client,
        pending: Mapping[str, Any],
    ) -> RedirectResponse:
        redirect_uri = str(pending["redirect_uri"])
        code = await oauth.issue_code(
            conn,
            org_id,
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            code_challenge=str(pending["challenge"]),
            resource=str(pending["resource"]),
            session_id=session_id,
            user_id=user_id,
        )
        await self._audit(
            conn,
            org_id,
            AuditAction.AUTHORIZE_APPROVED,
            user_id,
            client,
            session_id=session_id,
            redirect_uri=redirect_uri,
        )
        await oauth.touch_client(conn, client.client_id)
        return self._back(redirect_uri, {"code": code, "state": str(pending["state"])})

    async def _audit(  # noqa: PLR0913  (keyword-only)
        self,
        conn: AsyncConnection,
        org_id: str,
        action: AuditAction,
        user_id: str,
        client: oauth.Client,
        *,
        session_id: str | None = None,
        redirect_uri: str = "",
    ) -> None:
        after: dict[str, object] = {
            "client_id": client.client_id,
            "client_name": client.client_name,
        }
        if session_id is not None:
            after["session_id"] = session_id
        if redirect_uri:
            after["redirect_host"] = oauth.redirect_host(redirect_uri)
        await append_event(
            conn,
            NewEvent(
                org_id=org_id,
                action=action,
                actor=Actor(ActorKind.USER, user_id),
                target_kind="oauth_client",
                target_id=client.client_id,
                after=after,
            ),
        )

    # ── the steps the auth host calls ─────────────────────────────────────────

    async def finish(
        self,
        conn: AsyncConnection,
        org_id: str,
        *,
        user_id: str,
        connection_id: str,
        login: Mapping[str, Any],
    ) -> Response:
        """After single sign-on: the console goes back with a code; a registered client asks
        the person first."""
        kit = self._kit
        pending = {k: str(login.get(k, "")) for k in PENDING}
        client = await oauth.load_client(conn, pending["client_id"], self._s.console_url)
        if client is None or not oauth.allowed_redirect(client, pending["redirect_uri"]):
            return self._html(pages.UNKNOWN_CLIENT, 400)
        if client.first_party:
            session_id = await sessions.open_session(
                conn,
                org_id,
                user_id=user_id,
                kind="console",
                connection_id=connection_id,
                actor=Actor(ActorKind.USER, user_id),
            )
            return await self._approve(
                conn, org_id, user_id=user_id, session_id=session_id, client=client, pending=pending
            )
        nonce = secrets.token_urlsafe(24)
        answer = kit.sealer.seal(
            "consent",
            {
                **pending,
                "org": org_id,
                "user": user_id,
                "connection": connection_id,
                "nonce": _hash(nonce),
            },
            self._now() + CONSENT_SECONDS,
        )
        org_name = (await conn.execute(_ORG_NAME, {"org": org_id})).scalar_one()
        body = pages.oauth_consent(
            client_name=client.client_name,
            destination=oauth.redirect_host(pending["redirect_uri"]),
            org_name=str(org_name),
            answer=answer,
        )
        response = self._html(body, answer_to=oauth.form_action_source(pending["redirect_uri"]))
        kit.set_cookie(response, kit.consent_cookie, nonce, CONSENT_SECONDS)
        return response

    async def code_token(self, form: Mapping[str, str]) -> JSONResponse:  # noqa: C901, PLR0911
        """``grant_type=authorization_code`` (RFC 6749 section 4.1.3, RFC 7636 section 4.6)."""
        client_id, code = form.get("client_id", ""), form.get("code", "")
        redirect_uri, verifier = form.get("redirect_uri", ""), form.get("code_verifier", "")
        if not (client_id and code and redirect_uri and verifier):
            return _token_error("invalid_request")
        org_id = oauth.code_org(code)
        if org_id is None:
            return _token_error("invalid_grant")
        async with bound_org(self._kit.engine, org_id) as conn:
            client = await oauth.load_client(conn, client_id, self._s.console_url)
            if client is None:
                return _token_error("invalid_client", 401)
            used = await oauth.use_code(conn, org_id, code)
            if isinstance(used, oauth.Reused):
                await self._reused(conn, org_id, used.session_id, client)
                return _token_error("invalid_grant")
            if (
                used is None
                or not hmac.compare_digest(used.client_id, client.client_id)
                or not hmac.compare_digest(used.redirect_uri.encode(), redirect_uri.encode())
                or not oauth.pkce_ok(verifier, used.code_challenge)
                or ("resource" in form and not same_resource(form["resource"], used.resource))
            ):
                return _token_error("invalid_grant")
            live = await sessions.live_session(conn, org_id, used.session_id)
            if live is None:
                return _token_error("invalid_grant")
            await append_event(
                conn,
                NewEvent(
                    org_id=org_id,
                    action=AuditAction.TOKEN_ISSUED,
                    actor=Actor(ActorKind.USER, live.user_id),
                    target_kind="auth_session",
                    target_id=live.id,
                    after={
                        "kind": live.kind,
                        "via": "authorization_code",
                        "client_id": client.client_id,
                    },
                ),
            )
            refresh = await tokens.issue_refresh(conn, org_id, live.id)
            await oauth.touch_client(conn, client.client_id)
        return self._kit.token_reply(
            org_id, live.user_id, live.id, refresh, live.agent_client_id, used.resource
        )

    async def _reused(
        self, conn: AsyncConnection, org_id: str, session_id: str, client: oauth.Client
    ) -> None:
        """A used code again: someone else may hold it. End what it opened (RFC 6749 4.1.2)."""
        actor = Actor(ActorKind.INTEGRATION, "oauth")
        await sessions.revoke_session(conn, org_id, session_id, "code_reuse", actor=actor)
        await append_event(
            conn,
            NewEvent(
                org_id=org_id,
                action=AuditAction.CODE_REUSED,
                actor=actor,
                target_kind="auth_session",
                target_id=session_id,
                after={"client_id": client.client_id},
            ),
        )
        log.warning(
            "authorization code presented again in %s: session %s revoked", org_id, session_id
        )

    # ── routes ────────────────────────────────────────────────────────────────

    def add_routes(self, app: FastAPI) -> None:  # noqa: C901, PLR0915  (one route table)
        kit, s = self._kit, self._s

        @app.get("/.well-known/oauth-authorization-server")
        def authorization_server() -> JSONResponse:
            return JSONResponse(metadata(s.auth_url), headers={"cache-control": "max-age=300"})

        @app.post("/register")
        async def register(request: Request) -> JSONResponse:
            if not self._registrations.allow(self._address(request)):
                return JSONResponse(
                    {"error": "invalid_request", "error_description": "too many registrations"},
                    status_code=429,
                    headers={"retry-after": "3600", "cache-control": "no-store"},
                )
            raw = await request.body()
            try:
                body: object = json.loads(raw) if len(raw) <= MAX_REGISTRATION_BYTES else None
            except ValueError:
                body = None
            asked = registration(body)
            if isinstance(asked, tuple):
                error, description = asked
                return JSONResponse(
                    {"error": error, "error_description": description},
                    status_code=400,
                    headers={"cache-control": "no-store"},
                )
            async with kit.engine.begin() as conn:
                client, issued = await oauth.register_client(
                    conn, client_name=asked.client_name, redirect_uris=asked.redirect_uris
                )
            log.info("OAuth client %s registered", client.client_id)
            return JSONResponse(
                {
                    "client_id": client.client_id,
                    "client_id_issued_at": issued,
                    "client_name": client.client_name,
                    "redirect_uris": list(client.redirect_uris),
                    "grant_types": list(GRANT_TYPES),
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
                status_code=201,
                headers={"cache-control": "no-store"},
            )

        @app.get("/authorize")
        async def authorize(request: Request) -> Response:  # noqa: PLR0911  (one answer per check)
            q = request.query_params
            if any(len(q.getlist(k)) > 1 for k in q):
                return self._html(pages.UNKNOWN_CLIENT, 400)
            redirect_uri = q.get("redirect_uri", "")
            client = await self._client(q.get("client_id", ""))
            if client is None or not oauth.allowed_redirect(client, redirect_uri):
                return self._html(pages.UNKNOWN_CLIENT, 400)
            state = q.get("state", "")
            if q.get("response_type") != "code":
                return self._back(
                    redirect_uri, {"error": "unsupported_response_type", "state": state}
                )
            challenge = q.get("code_challenge", "")
            if (
                not state
                or len(state) > oauth.MAX_STATE_LENGTH
                or q.get("code_challenge_method") != "S256"
                or not oauth.challenge_ok(challenge)
            ):
                return self._back(redirect_uri, {"error": "invalid_request", "state": state})
            resource = self._resource(client)
            if not same_resource(q.get("resource", ""), resource):
                return self._back(redirect_uri, {"error": "invalid_target", "state": state})
            pending = {
                "client_id": client.client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "challenge": challenge,
                "resource": resource,
            }
            connection = await self._remembered(request)
            if connection is not None:
                return kit.to_workos(
                    connection, {"flow": "oauth", "org": connection.org_id, **pending}
                )
            sealed = kit.sealer.seal("authorize", pending, self._now() + AUTHORIZE_SECONDS)
            return self._html(pages.work_email(sealed))

        @app.post("/authorize")
        async def authorize_email(request: Request) -> Response:
            if not _same_site(request):
                return self._html(pages.BAD_REQUEST, 400)
            form = _form(await request.body())
            sealed = kit.sealer.open("authorize", form.get("pending", ""), self._now())
            if sealed is None:
                return self._html(pages.BAD_REQUEST, 400)
            domain = email_domain(form.get("email", ""))
            by_address = self._emails_by_address.allow(self._address(request))
            if not by_address or (domain is not None and not self._emails_by_domain.allow(domain)):
                return self._html(pages.TOO_MANY, 429)
            connection = None
            if domain is not None:
                try:
                    organizations = await kit.workos.organizations_for_domain(domain)
                except WorkOSError as e:
                    log.warning("WorkOS organisation lookup failed: %s", e)
                    return self._html(pages.UNAVAILABLE, 503)
                connection = await self._org_for(organizations)
            if connection is None:
                return self._html(pages.NO_SIGN_IN)
            pending = {k: str(sealed.get(k, "")) for k in PENDING}
            login = {"flow": "oauth", "org": connection.org_id, **pending}
            return kit.to_workos(connection, login, from_form=True)

        @app.post("/authorize/consent")
        async def consent(request: Request) -> Response:
            if not _same_site(request):
                return self._html(pages.BAD_REQUEST, 400)
            form = _form(await request.body())
            sealed = kit.sealer.open("consent", form.get("answer_token", ""), self._now())
            nonce = request.cookies.get(kit.consent_cookie, "")
            answer = form.get("answer")
            org_id = None if sealed is None else _org(sealed.get("org", ""))
            if (
                sealed is None
                or org_id is None
                or not nonce
                or not hmac.compare_digest(str(sealed.get("nonce")), _hash(nonce))
                or answer not in {"approve", "deny"}
            ):
                return self._html(pages.BAD_REQUEST, 400)
            pending = {k: str(sealed.get(k, "")) for k in PENDING}
            user_id = str(sealed.get("user"))
            async with bound_org(kit.engine, org_id) as conn:
                client = await oauth.load_client(conn, pending["client_id"], s.console_url)
                if client is None or client.first_party:
                    response: Response = self._html(pages.UNKNOWN_CLIENT, 400)
                elif answer == "deny":
                    await self._audit(
                        conn,
                        org_id,
                        AuditAction.AUTHORIZE_DENIED,
                        user_id,
                        client,
                        redirect_uri=pending["redirect_uri"],
                    )
                    response = self._back(
                        pending["redirect_uri"],
                        {"error": "access_denied", "state": pending["state"]},
                    )
                else:
                    session_id = await sessions.open_session(
                        conn,
                        org_id,
                        user_id=user_id,
                        kind="cli",
                        connection_id=str(sealed.get("connection")),
                        actor=Actor(ActorKind.USER, user_id),
                        agent_client_id=oauth.agent_slug(client.client_name),
                        token_audience=pending["resource"],
                    )
                    response = await self._approve(
                        conn,
                        org_id,
                        user_id=user_id,
                        session_id=session_id,
                        client=client,
                        pending=pending,
                    )
            kit.clear(response, kit.consent_cookie)
            return response
