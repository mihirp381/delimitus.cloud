"""OAuth 2.1 authorization codes for remote MCP clients and the console (decision 029).

Clients (RFC 7591): public clients register themselves; there is no client secret anywhere. A
redirect URI is ``https://...``, a loopback ``http://127.0.0.1:<port>/...`` or
``http://localhost:<port>/...`` (RFC 8252: the port is ignored when matching, since a native app
picks one per run), or a private-use scheme with a dot in it (``com.example.app:/...``). Never a
fragment, a wildcard, ``javascript:`` or ``data:``. The console is the one first-party client,
:data:`CONSOLE_CLIENT_ID`, a constant whose redirect is ``<console URL>/auth/callback``.

Codes: ``ac.<org id>.<secret>``, kept only as their SHA-256, one minute, once. A code is bound
to its client, redirect URI, PKCE S256 challenge (RFC 7636) and resource (RFC 8707). Presenting
a used code again revokes the session it opened (``code_reuse``).
"""

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.ids import new_id
from ssc_control.identity.sessions import CODE_SECONDS, digest, new_secret
from ssc_control.identity.tokens import AGENT_CLIENT, org_of

CODE_PREFIX: Final = "ac"
CONSOLE_CLIENT_ID: Final = "ssc-console"
CONSOLE_CLIENT_NAME: Final = "Delimitus console"
CONSOLE_CALLBACK_PATH: Final = "/auth/callback"
MCP_AGENT_CLIENT: Final = "mcp-client"
"""The ``client_id`` claim of an MCP client whose name makes no valid agent name."""
MAX_REDIRECT_URIS: Final = 10
MAX_REDIRECT_URI_LENGTH: Final = 2000
MAX_CLIENT_NAME: Final = 100
MAX_STATE_LENGTH: Final = 500
CLIENT_UNUSED_DAYS: Final = 30
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost"})
_CLIENT_ID: Final = re.compile(r"[A-Za-z0-9_-]{22}")
_CHALLENGE: Final = re.compile(r"[A-Za-z0-9_-]{43}")
_VERIFIER: Final = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_PRIVATE_SCHEME: Final = re.compile(r"[a-z][a-z0-9+-]*(\.[a-z0-9+-]+)+")
_FORBIDDEN_SCHEMES: Final = frozenset({"javascript", "data", "vbscript", "file", "blob"})


@dataclass(frozen=True, slots=True)
class Client:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    first_party: bool = False


def console_client(console_url: str) -> Client:
    """The console: no consent page, its own session kind, the API's user audience."""
    redirect = f"{console_url.rstrip('/')}{CONSOLE_CALLBACK_PATH}"
    return Client(CONSOLE_CLIENT_ID, CONSOLE_CLIENT_NAME, (redirect,), first_party=True)


# ── redirect URIs ────────────────────────────────────────────────────────────


def redirect_uri_problem(uri: str) -> str | None:  # noqa: PLR0911  (one answer per rule)
    """Why ``uri`` may not be registered, or None."""
    if not uri or len(uri) > MAX_REDIRECT_URI_LENGTH:
        return "a redirect URI is 1 to 2000 characters"
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in uri):  # noqa: PLR2004
        return "a redirect URI has no spaces or control characters"
    if "#" in uri:
        return "a redirect URI has no fragment"
    if "*" in uri:
        return "a redirect URI has no wildcard"
    try:
        parts = urlsplit(uri)
        _ = parts.port  # a port that is not a number raises
    except ValueError:
        return "a redirect URI is a URL"
    scheme = parts.scheme.lower()
    if scheme != parts.scheme or scheme in _FORBIDDEN_SCHEMES:
        return "a redirect URI's scheme is https, http on a loopback host, or a private-use one"
    if scheme == "https":
        if not parts.hostname or "@" in parts.netloc:
            return "an https redirect URI names a host and no user"
        return None
    if scheme == "http":
        if parts.hostname not in LOOPBACK_HOSTS or "@" in parts.netloc:
            return "an http redirect URI is on 127.0.0.1 or localhost"
        return None
    if _PRIVATE_SCHEME.fullmatch(scheme):
        return None
    return "a redirect URI's scheme is https, http on a loopback host, or a private-use one"


def _loopback(uri: str) -> tuple[str, str, str] | None:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS or "@" in parts.netloc:
        return None
    return parts.hostname, parts.path or "/", parts.query


def redirect_matches(registered: str, presented: str) -> bool:
    """Exact, except that a loopback redirect's port may differ (RFC 8252 section 7.3)."""
    if hmac.compare_digest(registered.encode(), presented.encode()):
        return True
    mine = _loopback(registered)
    return mine is not None and mine == _loopback(presented)


def allowed_redirect(client: Client, presented: str) -> bool:
    return any(redirect_matches(r, presented) for r in client.redirect_uris)


def redirect_host(uri: str) -> str:
    """What the consent page names as the destination: the host, or the private-use scheme."""
    parts = urlsplit(uri)
    return parts.hostname or f"{parts.scheme}:"


def form_action_source(uri: str) -> str:
    """The CSP ``form-action`` source a consent answer may redirect to."""
    parts = urlsplit(uri)
    if parts.scheme == "https":
        return f"https://{parts.netloc}"
    if parts.scheme == "http":
        return f"http://{parts.hostname}:*"
    return f"{parts.scheme}:"


def agent_slug(client_name: str) -> str:
    """The ``agent_client_id`` of an MCP client's session: its name as an agent name."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", client_name.lower()).strip("-._")[:64]
    return slug if AGENT_CLIENT.fullmatch(slug) else MCP_AGENT_CLIENT


# ── PKCE (RFC 7636, S256 only) ───────────────────────────────────────────────


def challenge_ok(challenge: str) -> bool:
    return _CHALLENGE.fullmatch(challenge) is not None


def pkce_ok(verifier: str, challenge: str) -> bool:
    if not _VERIFIER.fullmatch(verifier):
        return False
    made = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return hmac.compare_digest(made, challenge.encode())


# ── clients ──────────────────────────────────────────────────────────────────

_REGISTER = text(
    "insert into ssc.oauth_client (client_id, client_name, redirect_uris) "
    "values (:id, :name, :uris) returning extract(epoch from created_at)::bigint"
)
_LOAD = text("select client_name, redirect_uris from ssc.oauth_client where client_id = :id")
_TOUCH = text("update ssc.oauth_client set last_used_at = now() where client_id = :id")
_PRUNE_CLIENTS = text(
    "delete from ssc.oauth_client "
    "where coalesce(last_used_at, created_at) < now() - make_interval(days => :days)"
)


def new_client_id() -> str:
    """128 random bits, URL-safe: 22 characters."""
    return secrets.token_urlsafe(16)


async def register_client(
    conn: AsyncConnection, *, client_name: str, redirect_uris: Sequence[str]
) -> tuple[Client, int]:
    """A new client and when it was made (seconds since the epoch). Validate first."""
    client_id = new_client_id()
    issued = (
        await conn.execute(
            _REGISTER, {"id": client_id, "name": client_name, "uris": list(redirect_uris)}
        )
    ).scalar_one()
    return Client(client_id, client_name, tuple(redirect_uris)), int(issued)


async def load_client(conn: AsyncConnection, client_id: str, console_url: str) -> Client | None:
    if client_id == CONSOLE_CLIENT_ID:
        return console_client(console_url) if console_url else None
    if not _CLIENT_ID.fullmatch(client_id):
        return None
    row = (await conn.execute(_LOAD, {"id": client_id})).one_or_none()
    if row is None:
        return None
    name, uris = row
    return Client(client_id, str(name), tuple(str(u) for u in uris))


async def touch_client(conn: AsyncConnection, client_id: str) -> None:
    if client_id != CONSOLE_CLIENT_ID:
        await conn.execute(_TOUCH, {"id": client_id})


async def prune_clients(conn: AsyncConnection, *, days: int = CLIENT_UNUSED_DAYS) -> int:
    """Delete clients nobody used for ``days``; returns how many."""
    return (await conn.execute(_PRUNE_CLIENTS, {"days": days})).rowcount


# ── codes ────────────────────────────────────────────────────────────────────

_ISSUE = text(
    "insert into ssc.oauth_code (id, org_id, code_hash, client_id, redirect_uri, code_challenge, "
    "resource, session_id, user_id, created_at, expires_at) values (:id, :org, :hash, :client, "
    ":redirect, :challenge, :resource, :session, :user, now(), "
    "now() + make_interval(secs => :secs))"
)
_USE = text(
    "update ssc.oauth_code set used_at = now() where org_id = :org and code_hash = :hash "
    "and used_at is null returning client_id, redirect_uri, code_challenge, resource, "
    "session_id, user_id, expires_at > now()"
)
_USED = text("select session_id from ssc.oauth_code where org_id = :org and code_hash = :hash")


@dataclass(frozen=True, slots=True)
class Grant:
    """What a code was issued for."""

    client_id: str
    redirect_uri: str
    code_challenge: str
    resource: str
    session_id: str
    user_id: str


@dataclass(frozen=True, slots=True)
class Reused:
    """A used code presented again: the session it opened, which the caller revokes."""

    session_id: str


def code_org(code: str) -> str | None:
    parsed = org_of(code, CODE_PREFIX)
    return None if parsed is None else parsed[0]


async def issue_code(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    org_id: str,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    resource: str,
    session_id: str,
    user_id: str,
) -> str:
    code = f"{CODE_PREFIX}.{org_id}.{new_secret()}"
    await conn.execute(
        _ISSUE,
        {
            "id": new_id("oac"),
            "org": org_id,
            "hash": digest(code),
            "client": client_id,
            "redirect": redirect_uri,
            "challenge": code_challenge,
            "resource": resource,
            "session": session_id,
            "user": user_id,
            "secs": CODE_SECONDS,
        },
    )
    return code


async def use_code(conn: AsyncConnection, org_id: str, code: str) -> Grant | Reused | None:
    """Use a code up. None when it is unknown or expired; :class:`Reused` when it was used."""
    hashed = digest(code)
    row = (await conn.execute(_USE, {"org": org_id, "hash": hashed})).one_or_none()
    if row is None:
        used = (await conn.execute(_USED, {"org": org_id, "hash": hashed})).scalar_one_or_none()
        return None if used is None else Reused(str(used))
    client_id, redirect_uri, challenge, resource, session_id, user_id, live = row
    if not live:
        return None
    return Grant(
        str(client_id),
        str(redirect_uri),
        str(challenge),
        str(resource),
        str(session_id),
        str(user_id),
    )
