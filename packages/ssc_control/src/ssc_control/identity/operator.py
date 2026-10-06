"""What an SSC operator runs to make an org and to recover its admin (SSC-097).

``create-org`` does the work of the runbook's old ``python -c`` and ``UPDATE``: the org with its
founder, the check of the founder against WorkOS (``connections.check_founder``), the directory
connection and the org's cell label, all in ONE org-bound transaction. A refusal or a failure at
any step rolls back all of it: no org, no ``org_index`` row, no audit event. Events, in order:
``org.created``, ``directory.connected``, and ``org.updated`` when a cell label is given.

``restore-admin`` is for an org the directory left with no active admin (decision 024 allows
that: sync deactivates a founder it cannot find, and an operator restores an admin). In one
transaction it reactivates the person and makes them admin, and audits ``user.updated`` (role and
status before and after), ``user.reactivated`` when the status changed, and ``operator.access``,
all with the operator as actor. ``already_applied_at`` records a change somebody already made by
hand without touching the row: the chain is append-only, so the event is appended now and carries
the real time of the change in its body.

Both run as ``ssc_app`` with ``SSC_DATABASE_DSN`` (the role of ``connect``). There is no API route.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts.audit import AuditAction
from ssc_contracts.ids import new_id
from ssc_control.audit.chain import Actor, NewEvent, append_event
from ssc_control.db import CreatedOrg, NewOrg, bound_org, create_org_in
from ssc_control.db.errors import UNIQUE_VIOLATION
from ssc_control.identity import connections
from ssc_control.identity.connections import ConnectError, FounderResult
from ssc_control.identity.rules import JoinRule
from ssc_control.identity.workos import WorkOSClient
from ssc_control.snapshot.service import mark_dirty

MAX_REASON: Final = 200
_OPERATOR_ID: Final = re.compile(r"op_[a-z0-9][a-z0-9._-]{0,62}")
_CELL_LABEL: Final = re.compile(r"[a-z][a-z0-9]{7,15}")
_LONG_DIGITS: Final = re.compile(r"\d{6,}")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")

_SET_CELL_LABEL = text("update ssc.org set cell_label = :label where id = :org")
_LOCK_USER = text(
    "select role, status from ssc.user_account where org_id = :org and id = :id for update"
)
_LINKED = text(
    "select 1 from ssc.identity_link where org_id = :org and user_id = :user "
    "and issuer = :issuer limit 1"
)
_IN_GROUP = text(
    "select 1 from ssc.group_member m join ssc.user_group g "
    "on g.org_id = m.org_id and g.id = m.group_id "
    "where m.org_id = :org and m.user_id = :user and g.directory_ref = :ref limit 1"
)
_RESTORE = text(
    "update ssc.user_account set role = 'admin', status = 'active', deactivated_at = null "
    "where org_id = :org and id = :id"
)
_RECORDED = text(
    "select 1 from ssc.audit_event where org_id = :org and action = :action "
    "and target_kind = 'user_account' and target_id = :id and after ->> 'applied_at' = :at limit 1"
)


class RestoreError(ValueError):
    """The admin cannot be restored as asked."""


def check_operator_id(value: str) -> str:
    """``op_<name>``: the operator's own id, as in every other operator record."""
    if not _OPERATOR_ID.fullmatch(value):
        raise ValueError(f"an operator id looks like op_<name>, not {value!r}")
    return value


def check_cell_label(value: str) -> str:
    if not _CELL_LABEL.fullmatch(value):
        raise ValueError("a cell label is a letter then 7 to 15 lower-case letters or digits")
    return value


def check_reason(value: str) -> str:
    """The reason as it goes into the audit log, which nobody can edit afterwards: so it names
    no address and carries no long number (a phone or an ID), and it is short."""
    reason = value.strip()
    if not 1 <= len(reason) <= MAX_REASON:
        raise ValueError(f"the reason is 1 to {MAX_REASON} characters")
    if "@" in reason or _LONG_DIGITS.search(reason) or _CONTROL.search(reason):
        raise ValueError(
            "the reason goes into the audit log for good: no email address, no run of six or "
            "more digits, one line"
        )
    return reason


@dataclass(frozen=True, slots=True)
class OrgSetUp:
    org: CreatedOrg
    connection_id: str
    founder: list[FounderResult]


async def create_org_with_directory(  # noqa: PLR0913  (keyword-only)
    engine: AsyncEngine,
    client: WorkOSClient,
    *,
    name: str,
    founder_name: str,
    founder_email: str,
    founder_idp_id: str,
    workos_organization_id: str,
    workos_directory_id: str,
    sso_connection_ids: list[str],
    join_rule: JoinRule,
    admin_group_ref: str | None,
    cell_label: str | None,
    actor: Actor,
) -> OrgSetUp:
    """Create the org keyed under the directory, check the founder in WorkOS, connect the
    directory and set the cell label, if one is given (else the org keeps the generated one): all
    of it or none. :class:`ConnectError` says why not."""
    org_id = new_id("org")
    spec = NewOrg(
        name,
        founder_name,
        founder_email.strip().lower(),
        connections.directory_issuer(workos_directory_id),
        founder_idp_id,
    )
    try:
        async with bound_org(engine, org_id) as conn:
            created = await create_org_in(conn, org_id, spec, actor=actor)
            founder = await connections.check_founder(
                conn,
                client,
                org_id,
                workos_directory_id=workos_directory_id,
                join_rule=join_rule,
            )
            connection_id = await connections.connect(
                conn,
                org_id,
                workos_organization_id=workos_organization_id,
                workos_directory_id=workos_directory_id,
                sso_connection_ids=sso_connection_ids,
                join_rule=join_rule,
                admin_group_ref=admin_group_ref,
                actor=actor,
            )
            if cell_label is not None:
                await conn.execute(_SET_CELL_LABEL, {"label": cell_label, "org": org_id})
                await append_event(
                    conn,
                    NewEvent(
                        org_id=org_id,
                        action=AuditAction.ORG_UPDATED,
                        actor=actor,
                        target_kind="org",
                        target_id=org_id,
                        before={"cell_label": created.cell_label},
                        after={"cell_label": cell_label},
                    ),
                )
    except DBAPIError as e:
        if getattr(e.orig, "sqlstate", None) != UNIQUE_VIOLATION:
            raise
        raise ConnectError(
            "the WorkOS organisation, the directory or the cell label is already used by "
            "another org"
        ) from e
    return OrgSetUp(created, connection_id, founder)


@dataclass(frozen=True, slots=True)
class Restored:
    user_id: str
    role_before: str
    status_before: str
    recorded_only: bool
    warnings: tuple[str, ...]


def _applied_at(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise RestoreError("--already-applied-at needs a UTC offset, such as 2026-10-06T04:25:00Z")
    if value > datetime.now(UTC):
        raise RestoreError("--already-applied-at is in the future")
    return value.astimezone(UTC)


async def restore_admin(  # noqa: PLR0913, C901  (keyword-only; one list of refusals)
    conn: AsyncConnection,
    org_id: str,
    user_id: str,
    *,
    actor: Actor,
    reason: str,
    outside_admin_group: bool = False,
    already_applied_at: datetime | None = None,
) -> Restored:
    """Make ``user_id`` an active admin of ``org_id`` and audit it, in ``conn``'s org-bound
    transaction. With ``already_applied_at`` the row must already be an active admin and only
    ``operator.access`` is written. Raises :class:`RestoreError` before writing anything."""
    try:
        reason = check_reason(reason)
    except ValueError as e:
        raise RestoreError(str(e)) from e
    applied = None if already_applied_at is None else _applied_at(already_applied_at)
    connection = await connections.load(conn, org_id)
    if connection is None:
        raise RestoreError("the org has no directory connection")
    row = (await conn.execute(_LOCK_USER, {"org": org_id, "id": user_id})).one_or_none()
    if row is None:
        raise RestoreError(f"{user_id} is not a user of {org_id}")
    role, status = str(row[0]), str(row[1])
    linked = await conn.execute(
        _LINKED, {"org": org_id, "user": user_id, "issuer": connection.issuer}
    )
    if linked.one_or_none() is None:
        raise RestoreError(f"{user_id} has no identity link under the org's directory")
    warnings: list[str] = []
    ref = connection.admin_group_ref
    if ref is not None:
        in_group = await conn.execute(_IN_GROUP, {"org": org_id, "user": user_id, "ref": ref})
        if in_group.one_or_none() is None:
            if not outside_admin_group:
                raise RestoreError(
                    f"{user_id} is not in the org's admin group {ref}; sync would demote them "
                    "once another admin exists. Pass --outside-admin-group to go on"
                )
            warnings.append(
                f"{user_id} is not in the admin group {ref}: sync will demote them once "
                "another admin exists"
            )
    is_active_admin = (role, status) == ("admin", "active")
    if applied is None and is_active_admin:
        raise RestoreError(f"{user_id} is already an active admin; nothing to restore")
    if applied is not None:
        if not is_active_admin:
            raise RestoreError(
                f"{user_id} is not an active admin now ({role}, {status}), so there is no "
                "applied change to record"
            )
        recorded = await conn.execute(
            _RECORDED,
            {
                "org": org_id,
                "action": AuditAction.OPERATOR_ACCESS.value,
                "id": user_id,
                "at": applied.isoformat(),
            },
        )
        if recorded.one_or_none() is not None:
            raise RestoreError(f"a change applied at {applied.isoformat()} is already recorded")
        await _audit(
            conn,
            org_id,
            actor,
            AuditAction.OPERATOR_ACCESS,
            user_id,
            after={
                "role": "admin",
                "status": "active",
                "reason": reason,
                "applied_at": applied.isoformat(),
                "applied_via": "sql",
            },
        )
        return Restored(user_id, role, status, True, tuple(warnings))
    await conn.execute(_RESTORE, {"org": org_id, "id": user_id})
    await _audit(
        conn,
        org_id,
        actor,
        AuditAction.USER_UPDATED,
        user_id,
        before={"role": role, "status": status},
        after={"role": "admin", "status": "active"},
    )
    if status != "active":
        await _audit(
            conn,
            org_id,
            actor,
            AuditAction.USER_REACTIVATED,
            user_id,
            before={"status": status},
            after={"status": "active"},
        )
    await mark_dirty(conn, org_id)
    await _audit(
        conn, org_id, actor, AuditAction.OPERATOR_ACCESS, user_id, after={"reason": reason}
    )
    return Restored(user_id, role, status, False, tuple(warnings))


async def _audit(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    org_id: str,
    actor: Actor,
    action: AuditAction,
    user_id: str,
    *,
    before: dict[str, str] | None = None,
    after: dict[str, str] | None = None,
) -> None:
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=action,
            actor=actor,
            target_kind="user_account",
            target_id=user_id,
            before=before,
            after=after,
        ),
    )
