"""Auth-host sessions, one-time login codes and revocation (SSC-019). Every function runs in the
caller's org-bound transaction.

A session lives at most 12 hours from sign-in and is never extended. A login code is 256 random
bits, kept only as its SHA-256, valid for one minute, for one app host and for the browser that
holds the gateway's login nonce; it works once, and a wrong host or nonce uses it up.

Revoking a person (:func:`revoke_user`) revokes every live session, which ends their refresh
tokens and their API tokens' ``sid``, and sets ``sessions_not_before``, which the snapshot carries
to the gateway so every app-host cookie issued earlier is refused, even after reactivation.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import AuditAction
from ssc_contracts.ids import new_id
from ssc_control.audit.chain import Actor, NewEvent, append_event
from ssc_control.snapshot.service import mark_dirty

SESSION_SECONDS: Final = 12 * 3600
CODE_SECONDS: Final = 60
SessionKind = Literal["browser", "cli"]
RevokeReason = Literal["logout", "user_deactivated", "refresh_reuse", "operator"]


def digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class LiveSession:
    id: str
    user_id: str
    kind: SessionKind
    display_name: str
    email: str
    created_at: datetime
    expires_at: datetime
    now: datetime
    agent_client_id: str | None = None


@dataclass(frozen=True, slots=True)
class Redeemed:
    user_id: str
    display_name: str
    email: str
    iat: int
    exp: int


_OPEN = text(
    "insert into ssc.auth_session (id, org_id, user_id, kind, connection_id, created_at, "
    "expires_at, agent_client_id) values (:id, :org, :user, :kind, :conn, now(), "
    "now() + make_interval(secs => :secs), :agent)"
)
# Live: not revoked, not expired, the person active and not revoked since the session began.
_LIVE = text(
    "select s.id, s.user_id, s.kind, u.display_name, u.email, s.created_at, s.expires_at, now(), "
    "s.agent_client_id "
    "from ssc.auth_session s join ssc.user_account u on u.org_id = s.org_id and u.id = s.user_id "
    "where s.org_id = :org and s.id = :id and s.revoked_at is null and s.expires_at > now() "
    "and u.status = 'active' "
    "and (u.sessions_not_before is null or s.created_at >= u.sessions_not_before)"
)
_REVOKE = text(
    "update ssc.auth_session set revoked_at = now(), revoke_reason = :reason "
    "where org_id = :org and id = :id and revoked_at is null returning user_id, kind"
)
_REVOKE_USER = text(
    "update ssc.auth_session set revoked_at = now(), revoke_reason = :reason "
    "where org_id = :org and user_id = :user and revoked_at is null returning id, kind"
)
_NOT_BEFORE = text(
    "update ssc.user_account set sessions_not_before = now() where org_id = :org and id = :user"
)
_ISSUE_CODE = text(
    "insert into ssc.login_code (id, org_id, session_id, code_hash, binding_hash, host, "
    "created_at, expires_at) values (:id, :org, :session, :hash, :binding, :host, now(), "
    "now() + make_interval(secs => :secs))"
)
_USE_CODE = text(
    "update ssc.login_code set used_at = now() where org_id = :org and code_hash = :hash "
    "and used_at is null and expires_at > now() returning session_id, host, binding_hash"
)
_PRUNE = (
    text(
        "delete from ssc.login_code where org_id = :org and expires_at < now() - interval '1 day'"
    ),
    text(
        "delete from ssc.device_grant where org_id = :org and expires_at < now() - interval '1 day'"
    ),
    text(
        "delete from ssc.refresh_token r using ssc.auth_session s where r.org_id = :org "
        "and s.org_id = r.org_id and s.id = r.session_id "
        "and (s.expires_at < now() - interval '1 day' or s.revoked_at < now() - interval '1 day')"
    ),
)


async def open_session(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    org_id: str,
    *,
    user_id: str,
    kind: SessionKind,
    connection_id: str,
    actor: Actor,
    agent_client_id: str | None = None,
) -> str:
    """A new session; a ``cli`` one may be a coding agent's (``agent_client_id``, SSC-048)."""
    session_id = new_id("ses")
    await conn.execute(
        _OPEN,
        {
            "id": session_id,
            "org": org_id,
            "user": user_id,
            "kind": kind,
            "conn": connection_id,
            "secs": SESSION_SECONDS,
            "agent": agent_client_id,
        },
    )
    after = {"kind": kind, "user_id": user_id}
    if agent_client_id is not None:
        after["client_id"] = agent_client_id
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.LOGIN_SUCCEEDED,
            actor=actor,
            target_kind="auth_session",
            target_id=session_id,
            after=after,
        ),
    )
    return session_id


async def live_session(conn: AsyncConnection, org_id: str, session_id: str) -> LiveSession | None:
    row = (await conn.execute(_LIVE, {"org": org_id, "id": session_id})).one_or_none()
    if row is None:
        return None
    sid, user_id, kind, name, email, created, expires, now, agent = row
    return LiveSession(sid, user_id, kind, name, email, created, expires, now, agent)


async def _audit_revoked(
    conn: AsyncConnection, org_id: str, session_id: str, reason: RevokeReason, actor: Actor
) -> None:
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.TOKEN_REVOKED,
            actor=actor,
            target_kind="auth_session",
            target_id=session_id,
            after={"reason": reason},
        ),
    )


async def revoke_session(
    conn: AsyncConnection, org_id: str, session_id: str, reason: RevokeReason, *, actor: Actor
) -> bool:
    row = (
        await conn.execute(_REVOKE, {"org": org_id, "id": session_id, "reason": reason})
    ).one_or_none()
    if row is None:
        return False
    await _audit_revoked(conn, org_id, session_id, reason, actor)
    return True


async def revoke_user(
    conn: AsyncConnection, org_id: str, user_id: str, reason: RevokeReason, *, actor: Actor
) -> int:
    """End everything the person holds: sessions, refresh tokens, API tokens with a ``sid`` and
    (through the snapshot) app-host cookies. Returns the number of sessions revoked."""
    revoked = (
        await conn.execute(_REVOKE_USER, {"org": org_id, "user": user_id, "reason": reason})
    ).all()
    for session_id, _ in revoked:
        await _audit_revoked(conn, org_id, str(session_id), reason, actor)
    await conn.execute(_NOT_BEFORE, {"org": org_id, "user": user_id})
    await mark_dirty(conn, org_id)
    return len(revoked)


async def issue_code(
    conn: AsyncConnection, org_id: str, *, session_id: str, host: str, binding_hash: bytes
) -> str:
    code = new_secret()
    await conn.execute(
        _ISSUE_CODE,
        {
            "id": new_id("lgc"),
            "org": org_id,
            "session": session_id,
            "hash": digest(code),
            "binding": binding_hash,
            "host": host,
            "secs": CODE_SECONDS,
        },
    )
    return code


async def redeem_code(
    conn: AsyncConnection, org_id: str, *, code: str, host: str, nonce: str
) -> Redeemed | None:
    """The person a code signs in on ``host``, or None. The code is used up either way."""
    row = (await conn.execute(_USE_CODE, {"org": org_id, "hash": digest(code)})).one_or_none()
    if row is None:
        return None
    session_id, issued_host, binding = row
    if not hmac.compare_digest(issued_host, host) or not hmac.compare_digest(
        bytes(binding), digest(nonce)
    ):
        return None
    live = await live_session(conn, org_id, str(session_id))
    if live is None:
        return None
    iat = int(live.now.timestamp())
    exp = min(int(live.expires_at.timestamp()), iat + SESSION_SECONDS)
    return Redeemed(live.user_id, live.display_name, live.email, iat, exp)


async def prune(conn: AsyncConnection, org_id: str) -> None:
    """Drop codes, grants and refresh tokens dead for over a day."""
    for statement in _PRUNE:
        await conn.execute(statement, {"org": org_id})
