"""The authorisation decision for one request to an app host (SSC-018). Pure apart from the
login-code redemption, so every rule is tested without Envoy.

Stages, in order, stopping at the first refusal:

1. Host: lower-cased, port dropped. A host that is not an app host of this cell is ``404``.
2. Own paths under ``/.ssc/``: the login hand-back (``/.ssc/callback``) and ``/.ssc/logout``.
   Apps never receive these paths. The hand-back redeems the code with the login nonce this
   host set when it sent the browser to log in, so a code is good only in that browser.
3. Request shape, the same for every host: a declared body over the cap is ``413``; a
   cross-origin request (Fetch-Metadata) that is not a top-level ``GET``/``HEAD`` navigation is
   ``403``; a WebSocket upgrade whose ``Origin`` is not the app's own origin is ``403``.
4. Session: no valid session cookie for this host is a redirect to login, whether or not an app
   lives at the host. The redirect sets a fresh login nonce cookie and sends its SHA-256.
   A request carrying a schedule token (``SSC-Schedule-Token``, SSC-041) is a timer call
   instead: it needs no session and goes to :meth:`Gate._timer`, which never redirects to login
   and never marks the request for the "waking up" page, so the call waits for the app or fails
   with a plain status.
5. Snapshot: none loaded is ``503`` for every host (fail closed).
6. Revocation: a session issued before the person's ``sessions_not_before`` (deactivation,
   SSC-019) is a redirect to login with the cookie cleared.
7. Environment and sharing rule (``ssc_shared.access.decide``): an unknown host label and any
   refusal are the same ``404`` page.
8. Allowed: the identity note is minted, and the request goes to the environment's service with
   the time Cloud Run will end it (``X-SSC-Request-Deadline``). A browser page load without the
   wake cookie is marked for the "waking up" page (``ssc_edge.envoy``). A WebSocket, an event
   stream and every other request that is not a page or a static file (:func:`relayed`) is
   admitted to the stream relay (``ssc_edge.streams``), which asks :meth:`Gate.holds` again
   while it is open. A timer call is never relayed.

A refusal never names the reason to the caller; ``Deny.reason`` is for the gateway's own log.
"""

import base64
import hashlib
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from ssc_contracts.identity import MAX_GROUPS, IdentityNote
from ssc_contracts.schedule_token import SCHEDULE_TOKEN_HEADER
from ssc_contracts.snapshot import GrantRole
from ssc_edge import pages
from ssc_edge.identity_note import compose_note
from ssc_edge.schedule_token import ScheduleKeys
from ssc_edge.session import (
    COOKIE_NAME,
    LOGIN_COOKIE,
    WAKE_COOKIE,
    Session,
    SessionCodec,
    clear_cookie,
    clear_login_cookie,
    cookie_values,
    login_cookie,
    set_cookie,
    wake_cookie,
)
from ssc_shared.access import AccessView, EnvironmentIndex, decide
from ssc_shared.hosts import AppHost, parse_app_host
from ssc_shared.runtime import MAX_TIMEOUT_SECONDS, service_name

OWN_PREFIX: Final = "/.ssc/"
CALLBACK_PATH: Final = "/.ssc/callback"
LOGOUT_PATH: Final = "/.ssc/logout"
IDENTITY_HEADER: Final = "x-ssc-identity"
UPSTREAM_HEADER: Final = "x-ssc-upstream"
"""Internal: the service host Envoy forwards to. Envoy removes it before forwarding."""
DEADLINE_HEADER: Final = "x-ssc-request-deadline"
"""Unix seconds by which Cloud Run will have ended the request: from the check, the lower of the
gateway's request timeout and the environment's own (``timeout_seconds`` in the snapshot, 300
seconds when it has none)."""
WAKE_HEADER: Final = "x-ssc-wake"
"""Internal: a browser page load that gets the "waking up" page when the app is slow. Envoy
removes it before forwarding."""
STREAM_HEADER: Final = "x-ssc-stream"
"""Internal: the stream relay's ticket for a WebSocket or an event stream (``ssc_edge.streams``).
Envoy sends a request carrying it to the relay, which removes it."""
SCHEDULE_HEADER: Final = SCHEDULE_TOKEN_HEADER.lower()
"""A timer call's schedule token. The check reads it and Envoy removes it before forwarding."""
REQUEST_SECONDS: Final = MAX_TIMEOUT_SECONDS
SAFE_METHODS: Final = frozenset({"GET", "HEAD"})
_SAME_ORIGIN: Final = frozenset({"same-origin", "none"})

type Reason = Literal[
    "not_app_host",
    "too_large",
    "cross_origin",
    "websocket_origin",
    "no_session",
    "revoked",
    "bad_callback",
    "no_view",
    "unknown_host_label",
    "not_granted",
    "signed_in",
    "signed_out",
    "bad_schedule_token",
    "app_inactive",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class GateConfig:
    org_id: str
    cell_label: str
    apps_domain: str
    auth_url: str
    """The auth host's origin, ``https://auth.delimitus.com``; login is ``<auth_url>/login``."""
    issuer: str
    project_number: str
    region: str
    max_body_bytes: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Facts:
    """One request as Envoy describes it. ``headers`` are lower-cased names."""

    method: str
    host: str
    path: str
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class Allow:
    """``headers`` go to the app; ``client_headers`` are added to whatever answer the browser
    gets, the app's or the gateway's own."""

    upstream: str
    headers: Mapping[str, str]
    user: str
    environment: str
    client_headers: tuple[tuple[str, str], ...] = ()
    host: str = ""
    session: Session | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Deny:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    reason: Reason


class Redeemer(Protocol):
    """Turns the auth host's one-time code into a session for ``host``, presenting the login
    nonce the code is bound to (SSC-019)."""

    async def redeem(self, code: str, host: str, nonce: str) -> Session | None: ...


def binding_of(nonce: str) -> str:
    """What the auth host is told about a login nonce: its SHA-256, base64url."""
    digest = hashlib.sha256(nonce.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def new_nonce() -> str:
    return secrets.token_urlsafe(32)


def upstream_host(environment_id: str, *, project_number: str, region: str) -> str:
    """The deterministic ``run.app`` host of the environment's Cloud Run service (decision 014)."""
    return f"{service_name(environment_id)}-{project_number}.{region}.run.app"


def page_load(facts: Facts) -> bool:
    """A browser loading a page into a tab (Fetch-Metadata and ``Accept``); never a script, a
    WebSocket or a timer call."""
    h = facts.headers
    return (
        facts.method == "GET"
        and h.get("sec-fetch-mode") == "navigate"
        and h.get("sec-fetch-dest") == "document"
        and "text/html" in h.get("accept", "")
        and "upgrade" not in h
    )


def streaming(facts: Facts) -> bool:
    """A request that may stay open: a WebSocket upgrade or an ``EventSource`` stream."""
    h = facts.headers
    return h.get("upgrade", "").lower() == "websocket" or "text/event-stream" in h.get("accept", "")


PAGE_DESTS: Final = frozenset(
    {"document", "iframe", "script", "style", "image", "font", "manifest"}
)
"""``Sec-Fetch-Dest`` values of a page or a static file, which Envoy sends to the app itself."""


def relayed(facts: Facts) -> bool:
    """Whether the stream relay carries the request, so that a removed grant or the kill switch
    ends it while it is open (SSC-021, decided 2026-10-06): a stream, and every request that is
    not a page or a static file. That is ``Sec-Fetch-Dest`` outside ``PAGE_DESTS`` (a
    ``fetch()``, among them the usual AI chat stream) or no ``Sec-Fetch-Dest`` at all (scripts,
    curl, agents). A slow page load itself is not relayed and runs to the request timeout."""
    return streaming(facts) or facts.headers.get("sec-fetch-dest", "") not in PAGE_DESTS


def normal_host(raw: str) -> str:
    """Lower-cased, without port or trailing dot. An IPv6 literal is never an app host."""
    host = raw.strip().lower()
    if ":" in host and not host.startswith("["):
        host = host.rsplit(":", 1)[0]
    return host.removesuffix(".")


def _page(status: int, body: bytes, reason: Reason, *extra: tuple[str, str]) -> Deny:
    return Deny(status=status, headers=(*pages.HEADERS, *extra), body=body, reason=reason)


def _redirect(location: str, reason: Reason, *extra: tuple[str, str]) -> Deny:
    return Deny(
        status=302,
        headers=(("location", location), ("cache-control", "no-store"), *extra),
        body=b"",
        reason=reason,
    )


def not_found(reason: Reason) -> Deny:
    return _page(404, pages.NOT_FOUND, reason)


def _local_path(raw: str | None) -> str:
    """A path on this host only: starts with one ``/``, no scheme, no backslash."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return "/"
    return raw


class Gate:
    def __init__(  # noqa: PLR0913  (keyword-only collaborators)
        self,
        config: GateConfig,
        *,
        codec: SessionCodec,
        view: Callable[[], AccessView | None],
        sign: Callable[[IdentityNote], str],
        clock: Callable[[], int],
        redeemer: Redeemer | None = None,
        nonce: Callable[[], str] = new_nonce,
        refresh: Callable[[], Awaitable[None]] | None = None,
        schedule_keys: ScheduleKeys | None = None,
    ) -> None:
        """``refresh`` runs only when a check reaches the snapshot (``OnDemandView.refresh``).
        ``schedule_keys`` verifies timer calls; without them every timer call is ``404``."""
        self._cfg = config
        self._codec = codec
        self._view = view
        self._sign = sign
        self._clock = clock
        self._redeemer = redeemer
        self._nonce = nonce
        self._refresh = refresh
        self._schedule_keys = schedule_keys

    async def check(self, facts: Facts) -> Allow | Deny:
        host = normal_host(facts.host)
        app = parse_app_host(host, self._cfg.apps_domain)
        if app is None or app.cell_label != self._cfg.cell_label:
            return not_found("not_app_host")
        path = facts.path or "/"
        own = urlsplit(path).path
        if own.startswith(OWN_PREFIX):
            return await self._own(own, path, host, facts)
        shape = self._shape(facts, host)
        if shape is not None:
            return shape
        token = facts.headers.get(SCHEDULE_HEADER)
        if token is not None:
            return await self._timer(token, host, path, app, facts)
        session, presented = self._session(facts, host)
        if session is None:
            return self._login(host, path, cleared=presented)
        if self._refresh is not None:
            await self._refresh()
        return self._decide(host, path, app, session, facts)

    def _shape(self, facts: Facts, host: str) -> Deny | None:
        h = facts.headers
        length = h.get("content-length", "")
        if length.isdigit() and int(length) > self._cfg.max_body_bytes:
            return _page(413, pages.TOO_LARGE, "too_large")
        site = h.get("sec-fetch-site")
        if site is not None and site not in _SAME_ORIGIN:
            navigation = (
                facts.method in SAFE_METHODS
                and h.get("sec-fetch-mode") == "navigate"
                and h.get("sec-fetch-dest", "document") == "document"
            )
            if not navigation:
                return _page(403, pages.REFUSED, "cross_origin")
        if h.get("upgrade", "").lower() == "websocket" and h.get("origin") != f"https://{host}":
            return _page(403, pages.REFUSED, "websocket_origin")
        return None

    def _session(self, facts: Facts, host: str) -> tuple[Session | None, bool]:
        values = cookie_values(facts.headers.get("cookie", ""), COOKIE_NAME)
        now = self._clock()
        for value in values:
            session = self._codec.open(value, host, now=now)
            if session is not None and session.org == self._cfg.org_id:
                return session, True
        return None, bool(values)

    def _login(self, host: str, path: str, *, cleared: bool, reason: Reason = "no_session") -> Deny:
        nonce = self._nonce()
        query = urlencode(
            {
                "org": self._cfg.org_id,
                "return_to": f"https://{host}{_local_path(path)}",
                "binding": binding_of(nonce),
            }
        )
        extra = [("set-cookie", login_cookie(nonce))]
        if cleared:
            extra.append(("set-cookie", clear_cookie()))
        return _redirect(f"{self._cfg.auth_url}/login?{query}", reason, *extra)

    async def _own(self, own: str, path: str, host: str, facts: Facts) -> Deny:
        if own == CALLBACK_PATH and facts.method in SAFE_METHODS:
            query = parse_qs(urlsplit(path).query)
            code = (query.get("code") or [""])[0]
            nonces = cookie_values(facts.headers.get("cookie", ""), LOGIN_COOKIE)
            session = None
            if code and nonces and self._redeemer is not None:
                session = await self._redeemer.redeem(code, host, nonces[-1])
            if session is None or session.org != self._cfg.org_id:
                return _page(
                    400, pages.LOGIN_FAILED, "bad_callback", ("set-cookie", clear_login_cookie())
                )
            value = self._codec.seal(session, host)
            max_age = max(0, session.exp - self._clock())
            nxt = _local_path((query.get("next") or ["/"])[0])
            return _redirect(
                nxt,
                "signed_in",
                ("set-cookie", set_cookie(value, max_age=max_age)),
                ("set-cookie", clear_login_cookie()),
            )
        if own == LOGOUT_PATH and facts.headers.get("sec-fetch-site", "none") in _SAME_ORIGIN:
            return _redirect(
                f"{self._cfg.auth_url}/logout", "signed_out", ("set-cookie", clear_cookie())
            )
        return not_found("unknown_host_label")

    def _admit(
        self, view: AccessView, host: str, app: AppHost, session: Session
    ) -> tuple[str, EnvironmentIndex, GrantRole] | Reason:
        """Stages 6 and 7: the environment and role, or why the person may not have them."""
        not_before = view.not_before.get(session.sub)
        if not_before is not None and session.iat < not_before:
            return "revoked"
        env_id = view.hosts.get(host.split(".", 1)[0])
        if env_id is None:
            return "unknown_host_label"
        decision = decide(view, env_id, session.sub)
        env = view.environments.get(env_id)
        if not decision.allowed or decision.role is None or env is None:
            return "not_granted"
        if env.name != app.environment:
            return "unknown_host_label"
        return env_id, env, decision.role

    def holds(self, allowed: Allow) -> bool:
        """Whether an open stream admitted as ``allowed`` would be admitted now: the same
        stages against the view the gate holds now, and the session not yet expired."""
        view, session = self._view(), allowed.session
        app = parse_app_host(allowed.host, self._cfg.apps_domain)
        if view is None or session is None or app is None or self._clock() >= session.exp:
            return False
        return not isinstance(self._admit(view, allowed.host, app, session), str)

    def _decide(  # noqa: PLR0913  (one request's parts)
        self, host: str, path: str, app: AppHost, session: Session, facts: Facts
    ) -> Allow | Deny:
        view = self._view()
        if view is None:
            return _page(503, pages.UNAVAILABLE, "no_view")
        admitted = self._admit(view, host, app, session)
        if admitted == "revoked":
            return self._login(host, path, cleared=True, reason="revoked")
        if isinstance(admitted, str):
            return not_found(admitted)
        env_id, env, role = admitted
        mine = view.groups_by_user.get(session.sub, frozenset())
        groups = tuple(sorted(mine & env.by_group.keys()))[:MAX_GROUPS]
        now = self._clock()
        note = compose_note(
            issuer=self._cfg.issuer,
            audience=f"https://{host}",
            subject=session.sub,
            org=self._cfg.org_id,
            app=env.app_id,
            env=env.name,
            role=role,
            now=now,
            groups=groups,
            name=session.name or None,
            email=session.email or None,
        )
        upstream, headers = self._forward(env_id, env, note, now)
        client: tuple[tuple[str, str], ...] = ()
        if page_load(facts) and not cookie_values(facts.headers.get("cookie", ""), WAKE_COOKIE):
            headers[WAKE_HEADER] = "1"
            client = (("set-cookie", wake_cookie()),)
        return Allow(
            upstream=upstream,
            headers=MappingProxyType(headers),
            user=session.sub,
            environment=env_id,
            client_headers=client,
            host=host,
            session=session,
        )

    def _forward(
        self, env_id: str, env: EnvironmentIndex, note: IdentityNote, now: int
    ) -> tuple[str, dict[str, str]]:
        """The environment's service host and what the app receives with the request."""
        upstream = upstream_host(
            env_id, project_number=self._cfg.project_number, region=self._cfg.region
        )
        headers = {
            IDENTITY_HEADER: self._sign(note),
            UPSTREAM_HEADER: upstream,
            DEADLINE_HEADER: str(now + min(REQUEST_SECONDS, env.timeout_seconds)),
        }
        return upstream, headers

    async def _timer(  # noqa: PLR0913  (one request's parts)
        self, token: str, host: str, path: str, app: AppHost, facts: Facts
    ) -> Allow | Deny:
        """A timer call: the schedule token must admit exactly this request (origin, method,
        path, org, once), then the snapshot must place the token's environment at this host and
        the app must be active. An unknown or refused token and every refusal after it are the
        same ``404`` as a wrong address; no snapshot is ``503``. The note names the schedule with
        role ``schedule`` and no groups, name or email. A timer is never a stream."""
        keys = self._schedule_keys
        claims = None
        if keys is not None and not streaming(facts):
            claims = keys.verify(
                token,
                origin=f"https://{host}",
                org=self._cfg.org_id,
                method=facts.method,
                path=path,
            )
        if claims is None:
            return not_found("bad_schedule_token")
        if self._refresh is not None:
            await self._refresh()
        view = self._view()
        if view is None:
            return _page(503, pages.UNAVAILABLE, "no_view")
        env_id = view.hosts.get(host.split(".", 1)[0])
        env = view.environments.get(claims.env)
        if env_id != claims.env or env is None or env.name != app.environment:
            return not_found("unknown_host_label")
        if not env.active:
            return not_found("app_inactive")
        now = self._clock()
        note = compose_note(
            issuer=self._cfg.issuer,
            audience=f"https://{host}",
            subject=claims.sub,
            org=self._cfg.org_id,
            app=env.app_id,
            env=env.name,
            role="schedule",
            now=now,
        )
        upstream, headers = self._forward(claims.env, env, note, now)
        return Allow(
            upstream=upstream,
            headers=MappingProxyType(headers),
            user=claims.sub,
            environment=claims.env,
            host=host,
        )
