"""Pilot invite codes (decision 029 amendment of 2026-10-26): ``inv_`` and 16 lower-case letters
or digits, entered on the console's sign-in card in place of the org id. ``ssc.pilot_invite``
keeps only the SHA-256 of a code. Operators mint, list and revoke them with
``python -m ssc_control.identity invite``; ``/authorize`` resolves one to its org, and a use is
counted when the console session opens."""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.ids import new_id
from ssc_control.audit.chain import Actor, NewEvent, append_event

CODE: Final = re.compile(r"inv_[a-z0-9]{16}")
_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz0123456789"
_DURATION: Final = re.compile(r"([1-9][0-9]{0,3})([dh])")
MAX_SECONDS: Final = 90 * 86400
MAX_USES: Final = 1000

_INSERT = text(
    "insert into ssc.pilot_invite (id, code_sha256, org_id, expires_at, max_uses, note, "
    "created_by) values (:id, :digest, :org, now() + make_interval(secs => :secs), :max, :note, "
    ":by) returning id, org_id, expires_at, max_uses, uses, note, created_by, created_at, "
    "revoked_at"
)
_ORG = text("select 1 from ssc.org where id = :org")
_LIST = text(
    "select id, org_id, expires_at, max_uses, uses, note, created_by, created_at, revoked_at "
    "from ssc.pilot_invite where org_id = :org order by created_at"
)
_ONE = text("select org_id from ssc.pilot_invite where id = :id")
_REVOKE = text(
    "update ssc.pilot_invite set revoked_at = now() where id = :id and revoked_at is null "
    "returning id, org_id, expires_at, max_uses, uses, note, created_by, created_at, revoked_at"
)
_FIND = text(
    "select id, org_id from ssc.pilot_invite where code_sha256 = :digest "
    "and revoked_at is null and expires_at > now() and uses < max_uses"
)
_USE = text(
    "update ssc.pilot_invite set uses = uses + 1 where id = :id and org_id = :org "
    "and revoked_at is null and expires_at > now() and uses < max_uses returning id"
)


class InviteError(ValueError):
    """The invite cannot be made or changed as asked."""


@dataclass(frozen=True, slots=True)
class Invite:
    id: str
    org_id: str
    expires_at: datetime
    max_uses: int
    uses: int
    note: str | None
    created_by: str
    created_at: datetime
    revoked_at: datetime | None

    @property
    def line(self) -> str:
        state = "revoked" if self.revoked_at else "active"
        note = f" {self.note}" if self.note else ""
        return (
            f"{self.id} {state} uses={self.uses}/{self.max_uses} "
            f"expires={self.expires_at.isoformat(timespec='seconds')}{note}"
        )


def new_code() -> str:
    return "inv_" + "".join(secrets.choice(_ALPHABET) for _ in range(16))


def digest(code: str) -> bytes:
    return hashlib.sha256(code.encode()).digest()


def check_duration(value: str) -> int:
    """``14d`` or ``12h`` as seconds, at most 90 days."""
    m = _DURATION.fullmatch(value)
    secs = 0 if m is None else int(m[1]) * (86400 if m[2] == "d" else 3600)
    if not 0 < secs <= MAX_SECONDS:
        raise ValueError("expires-in is a number of days or hours, like 14d or 12h, up to 90d")
    return secs


def check_max_uses(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= MAX_USES:
        raise ValueError(f"max-uses is 1 to {MAX_USES}")
    return int(value)


async def create(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    org_id: str,
    *,
    expires_in: int,
    max_uses: int,
    note: str | None,
    actor: Actor,
) -> tuple[Invite, str]:
    """A new invite and its code, in ``conn``'s org-bound transaction; the code is not kept."""
    if (await conn.execute(_ORG, {"org": org_id})).one_or_none() is None:
        raise InviteError(f"no org {org_id}")
    code = new_code()
    row = await conn.execute(
        _INSERT,
        {
            "id": new_id("pinv"),
            "digest": digest(code),
            "org": org_id,
            "secs": expires_in,
            "max": max_uses,
            "note": note,
            "by": actor.id,
        },
    )
    invite = Invite(*row.one())
    await _audit(
        conn,
        invite,
        AuditAction.INVITE_CREATED,
        actor,
        {"expires_at": invite.expires_at.isoformat(), "max_uses": max_uses},
    )
    return invite, code


async def list_for(conn: AsyncConnection, org_id: str) -> list[Invite]:
    return [Invite(*r) for r in (await conn.execute(_LIST, {"org": org_id})).all()]


async def org_of(conn: AsyncConnection, invite_id: str) -> str | None:
    return (await conn.execute(_ONE, {"id": invite_id})).scalar_one_or_none()


async def revoke(conn: AsyncConnection, invite_id: str, *, actor: Actor) -> Invite:
    row = (await conn.execute(_REVOKE, {"id": invite_id})).one_or_none()
    if row is None:
        raise InviteError(f"{invite_id} is unknown or already revoked")
    invite = Invite(*row)
    await _audit(conn, invite, AuditAction.INVITE_REVOKED, actor, {"uses": invite.uses})
    return invite


async def find(conn: AsyncConnection, code: str) -> tuple[str, str] | None:
    """``(invite id, org id)`` of a code that is not revoked, expired or used up."""
    row = (await conn.execute(_FIND, {"digest": digest(code)})).one_or_none()
    return None if row is None else (str(row.id), str(row.org_id))


async def claim(conn: AsyncConnection, org_id: str, invite_id: str) -> bool:
    """Count one use, if the invite is still good."""
    return (await conn.execute(_USE, {"id": invite_id, "org": org_id})).one_or_none() is not None


async def audit_use(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    org_id: str,
    invite_id: str,
    *,
    user_id: str,
    session_id: str,
    client_id: str,
) -> None:
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.INVITE_USED,
            actor=Actor(ActorKind.USER, user_id),
            target_kind="pilot_invite",
            target_id=invite_id,
            after={"session_id": session_id, "client_id": client_id},
        ),
    )


async def _audit(
    conn: AsyncConnection,
    invite: Invite,
    action: AuditAction,
    actor: Actor,
    after: dict[str, object],
) -> None:
    await append_event(
        conn,
        NewEvent(
            org_id=invite.org_id,
            action=action,
            actor=actor,
            target_kind="pilot_invite",
            target_id=invite.id,
            after=after,
        ),
    )
