"""The authorisation decision for one request to an app host (SSC-018). Pure apart from the
login-code redemption, so every rule is tested without Envoy.

Stages, in order, stopping at the first refusal:

1. Host: lower-cased, port dropped. A host that is not an app host of this cell is ``404``.
2. Own paths under ``/.ssc/``: the login hand-back (``/.ssc/callback``) and ``/.ssc/logout``.
   Apps never receive these paths.
3. Request shape, the same for every host: a declared body over the cap is ``413``; a
   cross-origin request (Fetch-Metadata) that is not a top-level ``GET``/``HEAD`` navigation is
   ``403``; a WebSocket upgrade whose ``Origin`` is not the app's own origin is ``403``.
4. Session: no valid session cookie for this host is a redirect to login, whether or not an app
   lives at the host.
5. Snapshot: none loaded is ``503`` for every host (fail closed).
6. Environment and sharing rule (``ssc_shared.access.decide``): an unknown host label and any
   refusal are the same ``404`` page.
7. Allowed: the identity note is minted, and the request goes to the environment's service.

A refusal never names the reason to the caller; ``Deny.reason`` is for the gateway's own log.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from ssc_contracts.identity import MAX_GROUPS, IdentityNote
from ssc_edge import pages
from ssc_edge.identity_note import compose_note
from ssc_edge.session import (
    COOKIE_NAME,
    Session,
    SessionCodec,
    clear_cookie,
    cookie_values,
    set_cookie,
)
from ssc_shared.access import AccessView, decide
from ssc_shared.hosts import AppHost, parse_app_host

OWN_PREFIX: Final = "/.ssc/"
CALLBACK_PATH: Final = "/.ssc/callback"
LOGOUT_PATH: Final = "/.ssc/logout"
IDENTITY_HEADER: Final = "x-ssc-identity"
UPSTREAM_HEADER: Final = "x-ssc-upstream"
"""Internal: the service host Envoy forwards to. Envoy removes it before forwarding."""
SAFE_METHODS: Final = frozenset({"GET", "HEAD"})
_SAME_ORIGIN: Final = frozenset({"same-origin", "none"})
_ENV_ID: Final = re.compile(r"env_([a-z0-9]{20})")

type Reason = Literal[
    "not_app_host",
    "too_large",
    "cross_origin",
    "websocket_origin",
    "no_session",
    "bad_callback",
    "no_view",
    "unknown_host_label",
    "not_granted",
    "signed_in",
    "signed_out",
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
    upstream: str
    headers: Mapping[str, str]
    user: str
    environment: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Deny:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    reason: Reason


class Redeemer(Protocol):
    """Turns the auth host's one-time code into a session for ``host`` (SSC-019)."""

    async def redeem(self, code: str, host: str) -> Session | None: ...


def upstream_host(environment_id: str, *, project_number: str, region: str) -> str:
    """The deterministic ``run.app`` host of the environment's Cloud Run service, ``ssc-a-``
    plus the id's 20 characters (decision 014)."""
    m = _ENV_ID.fullmatch(environment_id)
    if m is None:
        raise ValueError(f"not an environment id: {environment_id!r}")
    return f"ssc-a-{m.group(1)}-{project_number}.{region}.run.app"


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
    ) -> None:
        self._cfg = config
        self._codec = codec
        self._view = view
        self._sign = sign
        self._clock = clock
        self._redeemer = redeemer

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
        session, presented = self._session(facts, host)
        if session is None:
            return self._login(host, path, cleared=presented)
        return self._decide(host, app, session)

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

    def _login(self, host: str, path: str, *, cleared: bool) -> Deny:
        query = urlencode({"return_to": f"https://{host}{_local_path(path)}"})
        extra = (("set-cookie", clear_cookie()),) if cleared else ()
        return _redirect(f"{self._cfg.auth_url}/login?{query}", "no_session", *extra)

    async def _own(self, own: str, path: str, host: str, facts: Facts) -> Deny:
        if own == CALLBACK_PATH and facts.method in SAFE_METHODS:
            query = parse_qs(urlsplit(path).query)
            code = (query.get("code") or [""])[0]
            session = None
            if code and self._redeemer is not None:
                session = await self._redeemer.redeem(code, host)
            if session is None or session.org != self._cfg.org_id:
                return self._login(host, "/", cleared=False)
            value = self._codec.seal(session, host)
            max_age = max(0, session.exp - self._clock())
            nxt = _local_path((query.get("next") or ["/"])[0])
            return _redirect(nxt, "signed_in", ("set-cookie", set_cookie(value, max_age=max_age)))
        if own == LOGOUT_PATH and facts.headers.get("sec-fetch-site", "none") in _SAME_ORIGIN:
            return _redirect(
                f"{self._cfg.auth_url}/logout", "signed_out", ("set-cookie", clear_cookie())
            )
        return not_found("unknown_host_label")

    def _decide(self, host: str, app: AppHost, session: Session) -> Allow | Deny:
        view = self._view()
        if view is None:
            return _page(503, pages.UNAVAILABLE, "no_view")
        label = host.split(".", 1)[0]
        env_id = view.hosts.get(label)
        if env_id is None:
            return not_found("unknown_host_label")
        decision = decide(view, env_id, session.sub)
        env = view.environments.get(env_id)
        if not decision.allowed or decision.role is None or env is None:
            return not_found("not_granted")
        if env.name != app.environment:
            return not_found("unknown_host_label")
        mine = view.groups_by_user.get(session.sub, frozenset())
        groups = tuple(sorted(mine & env.by_group.keys()))[:MAX_GROUPS]
        note = compose_note(
            issuer=self._cfg.issuer,
            audience=f"https://{host}",
            subject=session.sub,
            org=self._cfg.org_id,
            app=env.app_id,
            env=env.name,
            role=decision.role,
            now=self._clock(),
            groups=groups,
            name=session.name or None,
            email=session.email or None,
        )
        upstream = upstream_host(
            env_id, project_number=self._cfg.project_number, region=self._cfg.region
        )
        headers = {IDENTITY_HEADER: self._sign(note), UPSTREAM_HEADER: upstream}
        return Allow(
            upstream=upstream,
            headers=MappingProxyType(headers),
            user=session.sub,
            environment=env_id,
        )
