"""Directory sync (SSC-021): the users, groups and memberships an org's directory pushes.

A person is found by ``(issuer, subject)`` through ``identity_link``, never by email. Every
change is audited in the caller's transaction. A change that alters what the gateway decides (a
new user, a status change, a membership change) also marks the org's access snapshot dirty in
that transaction (decision 019). The database refuses demoting or deactivating the org's last
active admin (SC002, ``LAST_ORG_ADMIN``). Two first syncs of one identity racing each other end
with one unique violation (``ALREADY_EXISTS``); the directory retries and finds the user.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import AuditAction
from ssc_contracts.ids import new_id
from ssc_control.audit.chain import Actor, NewEvent, append_event
from ssc_control.snapshot.service import mark_dirty

Role = Literal["admin", "member"]
Status = Literal["active", "deactivated"]
Problem = Literal["group_not_found", "unknown_users"]


@dataclass(frozen=True, slots=True, kw_only=True)
class DirectoryUser:
    issuer: str
    subject: str
    display_name: str
    email: str
    role: Role
    status: Status


@dataclass(frozen=True, slots=True)
class UserResult:
    user_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class GroupResult:
    group_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class MembersResult:
    group_id: str
    added: tuple[str, ...]
    removed: tuple[str, ...]


class DirectoryError(Exception):
    """A sync the directory must fix on its side: a group or users this org does not have."""

    def __init__(self, problem: Problem, evidence: Mapping[str, object]) -> None:
        super().__init__(problem)
        self.problem: Problem = problem
        self.evidence: dict[str, object] = dict(evidence)


_FIND_LINK = text(
    "select user_id from ssc.identity_link "
    "where org_id = :org and issuer = :issuer and subject = :subject"
)
_INSERT_USER = text(
    "insert into ssc.user_account (id, org_id, display_name, email, role, status, deactivated_at) "
    "values (:id, :org, :name, :email, :role, :status, "
    "case when cast(:status as text) = 'deactivated' then now() end)"
)
_INSERT_LINK = text(
    "insert into ssc.identity_link (id, org_id, user_id, issuer, subject) "
    "values (:id, :org, :user, :issuer, :subject)"
)
_LOCK_USER = text(
    "select role, status, display_name, email from ssc.user_account "
    "where org_id = :org and id = :id for update"
)
_UPDATE_USER = text(
    "update ssc.user_account set display_name = :name, email = :email, role = :role, "
    "status = :status, deactivated_at = case when cast(:status as text) = 'active' then null "
    "when status = 'deactivated' then deactivated_at else now() end "
    "where org_id = :org and id = :id"
)
_INSERT_GROUP = text(
    "insert into ssc.user_group (id, org_id, directory_ref, display_name) "
    "values (:id, :org, :ref, :name) on conflict (org_id, directory_ref) do nothing returning id"
)
_LOCK_GROUP_BY_REF = text(
    "select id, display_name from ssc.user_group "
    "where org_id = :org and directory_ref = :ref for update"
)
_RENAME_GROUP = text(
    "update ssc.user_group set display_name = :name where org_id = :org and id = :id"
)
_LOCK_GROUP = text(
    "select directory_ref from ssc.user_group where org_id = :org and id = :id for update"
)
_KNOWN_USERS = text(
    "select id from ssc.user_account where org_id = :org and id = any(cast(:ids as text[]))"
)
_MEMBERS = text("select user_id from ssc.group_member where org_id = :org and group_id = :grp")
_REMOVE_MEMBERS = text(
    "delete from ssc.group_member "
    "where org_id = :org and group_id = :grp and user_id = any(cast(:ids as text[]))"
)
_ADD_MEMBERS = text(
    "insert into ssc.group_member (org_id, group_id, user_id) "
    "select :org, :grp, unnest(cast(:ids as text[]))"
)


async def _audit(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    org_id: str,
    actor: Actor,
    action: AuditAction,
    *,
    target_kind: str,
    target_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=action,
            actor=actor,
            target_kind=target_kind,
            target_id=target_id,
            before=before,
            after=after,
        ),
    )


async def _create_user(
    conn: AsyncConnection, org_id: str, user: DirectoryUser, actor: Actor
) -> UserResult:
    user_id = new_id("usr")
    await conn.execute(
        _INSERT_USER,
        {
            "id": user_id,
            "org": org_id,
            "name": user.display_name,
            "email": user.email,
            "role": user.role,
            "status": user.status,
        },
    )
    await conn.execute(
        _INSERT_LINK,
        {
            "id": new_id("idl"),
            "org": org_id,
            "user": user_id,
            "issuer": user.issuer,
            "subject": user.subject,
        },
    )
    await _audit(
        conn,
        org_id,
        actor,
        AuditAction.USER_CREATED,
        target_kind="user_account",
        target_id=user_id,
        after={"role": user.role, "status": user.status},
    )
    await mark_dirty(conn, org_id)
    return UserResult(user_id=user_id, created=True)


async def upsert_user(
    conn: AsyncConnection, org_id: str, user: DirectoryUser, *, actor: Actor
) -> UserResult:
    """Create the person or bring their record in line with the directory."""
    found = (
        await conn.execute(
            _FIND_LINK, {"org": org_id, "issuer": user.issuer, "subject": user.subject}
        )
    ).scalar_one_or_none()
    if found is None:
        return await _create_user(conn, org_id, user, actor)
    user_id = str(found)
    role, status, name, email = (
        await conn.execute(_LOCK_USER, {"org": org_id, "id": user_id})
    ).one()
    if (role, status, name, email) == (user.role, user.status, user.display_name, user.email):
        return UserResult(user_id=user_id, created=False)
    await conn.execute(
        _UPDATE_USER,
        {
            "org": org_id,
            "id": user_id,
            "name": user.display_name,
            "email": user.email,
            "role": user.role,
            "status": user.status,
        },
    )
    if (role, name, email) != (user.role, user.display_name, user.email):
        role_changed = role != user.role
        await _audit(
            conn,
            org_id,
            actor,
            AuditAction.USER_UPDATED,
            target_kind="user_account",
            target_id=user_id,
            before={"role": role} if role_changed else None,
            after={"role": user.role} if role_changed else None,
        )
    if status != user.status:
        await _audit(
            conn,
            org_id,
            actor,
            AuditAction.USER_DEACTIVATED
            if user.status == "deactivated"
            else AuditAction.USER_REACTIVATED,
            target_kind="user_account",
            target_id=user_id,
            before={"status": status},
            after={"status": user.status},
        )
        await mark_dirty(conn, org_id)
    return UserResult(user_id=user_id, created=False)


async def upsert_group(
    conn: AsyncConnection, org_id: str, *, directory_ref: str, display_name: str, actor: Actor
) -> GroupResult:
    """Create the group named by the directory's own id, or refresh its cached name.

    A new group has no members and no grants, so it changes no access decision."""
    created = (
        await conn.execute(
            _INSERT_GROUP,
            {"id": new_id("grp"), "org": org_id, "ref": directory_ref, "name": display_name},
        )
    ).scalar_one_or_none()
    if created is not None:
        await _audit(
            conn,
            org_id,
            actor,
            AuditAction.GROUP_SYNCED,
            target_kind="user_group",
            target_id=str(created),
            after={"directory_ref": directory_ref},
        )
        return GroupResult(group_id=str(created), created=True)
    group_id, name = (
        await conn.execute(_LOCK_GROUP_BY_REF, {"org": org_id, "ref": directory_ref})
    ).one()
    if name != display_name:
        await conn.execute(_RENAME_GROUP, {"org": org_id, "id": group_id, "name": display_name})
    return GroupResult(group_id=str(group_id), created=False)


async def set_group_members(
    conn: AsyncConnection, org_id: str, group_id: str, user_ids: Sequence[str], *, actor: Actor
) -> MembersResult:
    """Make the group's members exactly ``user_ids``. Every one must be a user of this org."""
    ref = (await conn.execute(_LOCK_GROUP, {"org": org_id, "id": group_id})).scalar_one_or_none()
    if ref is None:
        raise DirectoryError("group_not_found", {"group_id": group_id})
    desired = set(user_ids)
    known = {
        str(u)
        for u in (
            await conn.execute(_KNOWN_USERS, {"org": org_id, "ids": sorted(desired)})
        ).scalars()
    }
    if missing := desired - known:
        raise DirectoryError("unknown_users", {"group_id": group_id, "count": len(missing)})
    current = {
        str(u) for u in (await conn.execute(_MEMBERS, {"org": org_id, "grp": group_id})).scalars()
    }
    added, removed = sorted(desired - current), sorted(current - desired)
    if removed:
        await conn.execute(_REMOVE_MEMBERS, {"org": org_id, "grp": group_id, "ids": removed})
    if added:
        await conn.execute(_ADD_MEMBERS, {"org": org_id, "grp": group_id, "ids": added})
    if added or removed:
        await _audit(
            conn,
            org_id,
            actor,
            AuditAction.GROUP_SYNCED,
            target_kind="user_group",
            target_id=group_id,
            after={"directory_ref": str(ref), "added": added, "removed": removed},
        )
        await mark_dirty(conn, org_id)
    return MembersResult(group_id=group_id, added=tuple(added), removed=tuple(removed))
