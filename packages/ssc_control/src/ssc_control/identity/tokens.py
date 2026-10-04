"""Credentials the auth host issues (SSC-019, decision 024).

Access tokens: ES256 ``ssc-api+jwt`` the API verifies, five minutes, with ``sid`` naming the
session; ``jti`` is the session id, so rate limits and idempotency keys follow the session. A
session approved for a coding agent (SSC-048) adds ``agent: true`` and ``client_id``, so the API
records every call as the agent's.
Refresh tokens (command line only): ``ssc_rt.<org id>.<secret>``, used once; presenting a used one
revokes the session (``refresh_reuse``). Device grants (RFC 8628): the device code is
``<org id>.<secret>``, the user code eight consonants. Only SHA-256 digests are stored.
"""

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.ids import new_id
from ssc_control.api.auth import ALGORITHM, API_TOKEN_TYP
from ssc_control.audit.chain import Actor
from ssc_control.db.bind import check_org_id
from ssc_control.identity.sessions import digest, live_session, new_secret, revoke_session

ACCESS_SECONDS: Final = 300
REFRESH_PREFIX: Final = "ssc_rt"  # noqa: S105  (a token prefix, not a secret)
DEVICE_SECONDS: Final = 600
DEVICE_INTERVAL: Final = 5
USER_CODE_ALPHABET: Final = "BCDFGHJKLMNPQRSTVWXZ"
USER_CODE_LENGTH: Final = 8
AGENT_CLIENT: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

DeviceState = Literal["authorization_pending", "slow_down", "access_denied", "expired_token"]


class Signer:
    """The auth host's ES256 key. The API trusts :meth:`jwks` (``SSC_API_JWKS``)."""

    def __init__(self, private_pem: bytes, kid: str, issuer: str) -> None:
        key = serialization.load_pem_private_key(private_pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError("the signing key must be an EC P-256 private key")
        self._key = key
        self._pem = private_pem
        self.kid = kid
        self.issuer = issuer

    def jwks(self) -> dict[str, Any]:
        jwk: dict[str, Any] = dict(ECAlgorithm.to_jwk(self._key.public_key(), as_dict=True))
        jwk.update({"kid": self.kid, "alg": ALGORITHM, "use": "sig"})
        return {"keys": [jwk]}

    def access_token(  # noqa: PLR0913  (keyword-only)
        self,
        *,
        org_id: str,
        user_id: str,
        session_id: str,
        audience: str,
        now: datetime,
        agent_client_id: str | None = None,
    ) -> str:
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": audience,
            "sub": user_id,
            "iat": now,
            "exp": now + timedelta(seconds=ACCESS_SECONDS),
            "jti": session_id,
            "org": org_id,
            "kind": "user",
            "sid": session_id,
        }
        if agent_client_id is not None:
            claims.update(agent=True, client_id=agent_client_id)
        return jwt.encode(
            claims, self._pem, algorithm=ALGORITHM, headers={"kid": self.kid, "typ": API_TOKEN_TYP}
        )


def new_signing_pem() -> bytes:
    """A fresh P-256 key, PKCS#8 PEM (dev and tests; production keys come from the deploy)."""
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def org_of(token: str, prefix: str | None = None) -> tuple[str, str] | None:
    """``(org id, secret)`` from ``[prefix.]org.secret``, or None."""
    parts = token.split(".")
    if prefix is not None:
        if len(parts) != 3 or parts[0] != prefix:  # noqa: PLR2004
            return None
        parts = parts[1:]
    if len(parts) != 2 or not parts[1]:  # noqa: PLR2004
        return None
    try:
        return check_org_id(parts[0]), parts[1]
    except ValueError:
        return None


# ── refresh tokens ───────────────────────────────────────────────────────────

_ISSUE_REFRESH = text(
    "insert into ssc.refresh_token (id, org_id, session_id, token_hash) "
    "values (:id, :org, :session, :hash)"
)
_USE_REFRESH = text(
    "update ssc.refresh_token set used_at = now() where org_id = :org and token_hash = :hash "
    "and used_at is null returning session_id"
)
_USED_REFRESH = text(
    "select session_id from ssc.refresh_token where org_id = :org and token_hash = :hash"
)


async def issue_refresh(conn: AsyncConnection, org_id: str, session_id: str) -> str:
    secret = new_secret()
    token = f"{REFRESH_PREFIX}.{org_id}.{secret}"
    await conn.execute(
        _ISSUE_REFRESH,
        {"id": new_id("rft"), "org": org_id, "session": session_id, "hash": digest(token)},
    )
    return token


@dataclass(frozen=True, slots=True)
class Refreshed:
    user_id: str
    session_id: str
    refresh_token: str
    agent_client_id: str | None = None


async def rotate_refresh(
    conn: AsyncConnection, org_id: str, token: str, *, actor: Actor
) -> Refreshed | None:
    """A new refresh token for a live session, or None. A reused token revokes its session."""
    hashed = digest(token)
    session_id = (
        await conn.execute(_USE_REFRESH, {"org": org_id, "hash": hashed})
    ).scalar_one_or_none()
    if session_id is None:
        reused = (
            await conn.execute(_USED_REFRESH, {"org": org_id, "hash": hashed})
        ).scalar_one_or_none()
        if reused is not None:
            await revoke_session(conn, org_id, str(reused), "refresh_reuse", actor=actor)
        return None
    live = await live_session(conn, org_id, str(session_id))
    if live is None or live.kind != "cli":
        return None
    refresh = await issue_refresh(conn, org_id, live.id)
    return Refreshed(live.user_id, live.id, refresh, live.agent_client_id)


# ── device grants (RFC 8628) ─────────────────────────────────────────────────

_START = text(
    "insert into ssc.device_grant (id, org_id, device_code_hash, user_code, expires_at, "
    "agent_client_id) values (:id, :org, :hash, :code, now() + make_interval(secs => :secs), "
    ":agent)"
)
_AGENT = text("select agent_client_id from ssc.device_grant where org_id = :org and id = :id")
_PENDING = text(
    "select id from ssc.device_grant where org_id = :org and user_code = :code "
    "and state = 'pending' and expires_at > now()"
)
_DECIDE = text(
    "update ssc.device_grant set state = :state, session_id = :session "
    "where org_id = :org and id = :id and state = 'pending' and expires_at > now() returning id"
)
_POLL = text(
    "select id, state, session_id, expires_at <= now(), "
    "last_polled_at > now() - make_interval(secs => :interval) "
    "from ssc.device_grant where org_id = :org and device_code_hash = :hash for update"
)
_POLLED = text(
    "update ssc.device_grant set last_polled_at = now() where org_id = :org and id = :id"
)
_CONSUME = text("update ssc.device_grant set state = 'consumed' where org_id = :org and id = :id")


@dataclass(frozen=True, slots=True)
class DeviceStart:
    device_code: str
    user_code: str
    expires_in: int
    interval: int


def new_user_code() -> str:
    return "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))


def normal_user_code(raw: str) -> str:
    return "".join(c for c in raw.upper() if c.isalpha())


async def start_device(
    conn: AsyncConnection, org_id: str, agent_client_id: str | None = None
) -> DeviceStart:
    """A new grant; with ``agent_client_id`` the session it opens is that coding agent's."""
    device_code = f"{org_id}.{new_secret()}"
    user_code = new_user_code()
    for _ in range(5):
        if (await conn.execute(_PENDING, {"org": org_id, "code": user_code})).first() is None:
            break
        user_code = new_user_code()
    await conn.execute(
        _START,
        {
            "id": new_id("dvg"),
            "org": org_id,
            "hash": digest(device_code),
            "code": user_code,
            "secs": DEVICE_SECONDS,
            "agent": agent_client_id,
        },
    )
    return DeviceStart(device_code, user_code, DEVICE_SECONDS, DEVICE_INTERVAL)


async def pending_grant(conn: AsyncConnection, org_id: str, user_code: str) -> str | None:
    code = normal_user_code(user_code)
    found = (await conn.execute(_PENDING, {"org": org_id, "code": code})).scalar_one_or_none()
    return None if found is None else str(found)


async def grant_agent(conn: AsyncConnection, org_id: str, grant_id: str) -> str | None:
    """The coding agent a grant was started for, or None for the person's own login."""
    found = await conn.execute(_AGENT, {"org": org_id, "id": grant_id})
    agent = found.scalar_one_or_none()
    return None if agent is None else str(agent)


async def decide_grant(
    conn: AsyncConnection, org_id: str, grant_id: str, *, session_id: str | None
) -> bool:
    """Approve with the command-line session just opened, or deny (``session_id`` None)."""
    state = "approved" if session_id is not None else "denied"
    row = await conn.execute(
        _DECIDE, {"org": org_id, "id": grant_id, "state": state, "session": session_id}
    )
    return row.first() is not None


async def poll_device(conn: AsyncConnection, org_id: str, device_code: str) -> str | DeviceState:
    """The approved session id (consumed: a code yields one session), or the RFC 8628 error."""
    row = (
        await conn.execute(
            _POLL, {"org": org_id, "hash": digest(device_code), "interval": DEVICE_INTERVAL - 1}
        )
    ).one_or_none()
    if row is None:
        return "expired_token"
    grant_id, state, session_id, expired, too_soon = row
    await conn.execute(_POLLED, {"org": org_id, "id": grant_id})
    if state == "denied":
        return "access_denied"
    if state == "consumed" or (state == "pending" and expired):
        return "expired_token"
    if state == "pending":
        return "slow_down" if too_soon else "authorization_pending"
    await conn.execute(_CONSUME, {"org": org_id, "id": grant_id})
    return str(session_id)


def utcnow() -> datetime:
    return datetime.now(UTC)
