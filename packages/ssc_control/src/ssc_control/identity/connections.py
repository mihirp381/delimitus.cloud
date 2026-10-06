"""An org's directory connection: its WorkOS organisation, directory and allowed SSO connections.

Connected by an SSC operator after the org's IT admin finishes the WorkOS Admin Portal. The
first-admin check: an active admin must already be linked under the directory's issuer (the org
is created with ``issuer = workos:<directory id>`` and the founder's directory ``idp_id``), so
the first sync finds the founder instead of creating a second account. ``dsync.deleted`` freezes the
connection (no mass deprovisioning); an operator unfreezes by connecting again.

:func:`check_founder` is the same check made against WorkOS itself, before sync starts: sync keys
people by the directory's ``idp_id`` under both join rules and deactivates a linked person it
cannot find, so a founder keyed any other way leaves the org with no active admin at the first
full sync. ``connect`` stays a database transaction (the dev stack and tests use it), so the
operator commands call ``check_founder`` first.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import AuditAction
from ssc_contracts.ids import new_id
from ssc_control.audit.chain import Actor, NewEvent, append_event
from ssc_control.identity.rules import DirectoryPerson, JoinRule, ProfileError, is_member
from ssc_control.identity.workos import WorkOSClient

ISSUER_PREFIX: Final = "workos:"
FrozenReason = Literal["directory_deleted", "operator"]


def directory_issuer(workos_directory_id: str) -> str:
    """The issuer of every identity link the directory creates (decision 024)."""
    return f"{ISSUER_PREFIX}{workos_directory_id}"


def admin_link_issuer(sso_connection_id: str) -> str:
    """The issuer of a link an org admin made from the Unlinked logins list."""
    return f"{ISSUER_PREFIX}sso:{sso_connection_id}"


@dataclass(frozen=True, slots=True, kw_only=True)
class DirectoryConnection:
    id: str
    org_id: str
    workos_organization_id: str
    workos_directory_id: str
    sso_connection_ids: frozenset[str]
    join_rule: JoinRule
    admin_group_ref: str | None
    frozen: bool
    event_cursor: str | None

    @property
    def issuer(self) -> str:
        return directory_issuer(self.workos_directory_id)


_COLUMNS = (
    "id, org_id, workos_organization_id, workos_directory_id, sso_connection_ids, join_rule, "
    "admin_group_ref, state, event_cursor"
)
_LOAD = text(f"select {_COLUMNS} from ssc.directory_connection where org_id = :org")  # noqa: S608
_LOAD_FOR_UPDATE = text(
    f"select {_COLUMNS} from ssc.directory_connection where org_id = :org for update"  # noqa: S608
)
_UPSERT = text(
    "insert into ssc.directory_connection (id, org_id, workos_organization_id, "
    "workos_directory_id, sso_connection_ids, join_rule, admin_group_ref) "
    "values (:id, :org, :wo, :wd, cast(:sso as text[]), :rule, :admin) "
    "on conflict (org_id) do update set workos_organization_id = excluded.workos_organization_id, "
    "workos_directory_id = excluded.workos_directory_id, "
    "sso_connection_ids = excluded.sso_connection_ids, join_rule = excluded.join_rule, "
    "admin_group_ref = excluded.admin_group_ref, state = 'active', frozen_reason = null, "
    "event_cursor = case when ssc.directory_connection.workos_directory_id = "
    "excluded.workos_directory_id then ssc.directory_connection.event_cursor end, "
    "last_full_sync_at = case when ssc.directory_connection.workos_directory_id = "
    "excluded.workos_directory_id then ssc.directory_connection.last_full_sync_at end, "
    "updated_at = now() returning id"
)
_FREEZE = text(
    "update ssc.directory_connection set state = 'frozen', frozen_reason = :reason, "
    "updated_at = now() where org_id = :org and state = 'active' returning id"
)
_FOUNDER_LINKED = text(
    "select count(*) from ssc.identity_link l join ssc.user_account u "
    "on u.org_id = l.org_id and u.id = l.user_id "
    "where l.org_id = :org and l.issuer = :issuer and u.role = 'admin' and u.status = 'active'"
)
_FOUNDERS = text(
    "select u.id, l.subject, u.email from ssc.identity_link l join ssc.user_account u "
    "on u.org_id = l.org_id and u.id = l.user_id "
    "where l.org_id = :org and l.issuer = :issuer and u.role = 'admin' and u.status = 'active' "
    "order by u.id"
)


class ConnectError(ValueError):
    """The connection cannot be made as asked."""


@dataclass(frozen=True, slots=True)
class FounderResult:
    """One linked admin against the directory: ``problem`` is None when sync will keep them."""

    user_id: str
    subject: str
    problem: str | None

    @property
    def ok(self) -> bool:
        return self.problem is None

    @property
    def line(self) -> str:
        verdict = "ok" if self.problem is None else f"REFUSED: {self.problem}"
        return f"admin {self.user_id} (subject {self.subject}): {verdict}"


def _row(row: Sequence[object]) -> DirectoryConnection:
    cid, org, wo, wd, sso, rule, admin, state, cursor = row
    return DirectoryConnection(
        id=str(cid),
        org_id=str(org),
        workos_organization_id=str(wo),
        workos_directory_id=str(wd),
        sso_connection_ids=frozenset(cast(list[str], sso)),
        join_rule=cast(JoinRule, rule),
        admin_group_ref=None if admin is None else str(admin),
        frozen=state == "frozen",
        event_cursor=None if cursor is None else str(cursor),
    )


async def load(
    conn: AsyncConnection, org_id: str, *, for_update: bool = False
) -> DirectoryConnection | None:
    stmt = _LOAD_FOR_UPDATE if for_update else _LOAD
    row = (await conn.execute(stmt, {"org": org_id})).one_or_none()
    return None if row is None else _row(tuple(row))


def _founder_problem(
    subject: str, email: str, people: Sequence[DirectoryPerson], join_rule: JoinRule
) -> str | None:
    """Why sync would not keep the admin keyed ``subject``, or None. Sync joins by ``idp_id``
    under both rules; ``email`` also needs the address to name exactly one active member."""
    members = [p for p in people if p.active and is_member(p)]
    by_subject = next((p for p in members if p.idp_id == subject), None)
    if join_rule == "email":
        holders = [p for p in members if p.email == email.lower()]
        if len(holders) > 1:
            problem = f"{len(holders)} active directory users have this admin's email"
        elif holders and holders[0].idp_id != subject:
            problem = (
                f"email matches directory user {holders[0].id} whose idp_id is "
                f"{holders[0].idp_id}, not the linked subject {subject}"
            )
        elif not holders:
            problem = "no active directory user has this admin's email"
        else:
            return None
    elif by_subject is not None:
        return None
    else:
        problem = f"no active directory user has idp_id {subject}"
    named = [p for p in people if p.email == email.lower()]
    if not named:
        return f"{problem}; no directory user has this admin's email"
    listed = ", ".join(
        f"{p.id} (idp_id {p.idp_id}, {'active' if p.active else 'not active'})" for p in named
    )
    return f"{problem}; directory users with this admin's email: {listed}"


async def check_founder(
    conn: AsyncConnection,
    client: WorkOSClient,
    org_id: str,
    *,
    workos_directory_id: str,
    join_rule: JoinRule,
) -> list[FounderResult]:
    """Every active admin linked under the directory's issuer, each checked against the
    directory's users in WorkOS. Raises :class:`ConnectError` (listing every result) unless at
    least one passes, so the first sync keeps an admin. Reads the database and WorkOS; writes
    nothing."""
    rows = (
        await conn.execute(
            _FOUNDERS, {"org": org_id, "issuer": directory_issuer(workos_directory_id)}
        )
    ).all()
    if not rows:
        raise ConnectError("no active admin is linked under this directory's issuer")
    people: list[DirectoryPerson] = []
    for raw in await client.directory_users(workos_directory_id):
        try:
            people.append(DirectoryPerson.from_wire(raw))
        except ProfileError:
            continue
    results = [
        FounderResult(
            str(user_id),
            str(subject),
            _founder_problem(str(subject), str(email), people, join_rule),
        )
        for user_id, subject, email in rows
    ]
    if not any(r.ok for r in results):
        raise ConnectError(
            "no linked admin is an active user of the directory in WorkOS, so the first sync "
            "would leave the org with no admin\n" + "\n".join(r.line for r in results)
        )
    return results


async def connect(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    org_id: str,
    *,
    workos_organization_id: str,
    workos_directory_id: str,
    sso_connection_ids: Sequence[str],
    join_rule: JoinRule,
    admin_group_ref: str | None,
    actor: Actor,
) -> str:
    """Record (or replace) the org's connection. A new directory id starts sync over."""
    if not sso_connection_ids:
        raise ConnectError("at least one SSO connection is needed")
    linked = (
        await conn.execute(
            _FOUNDER_LINKED, {"org": org_id, "issuer": directory_issuer(workos_directory_id)}
        )
    ).scalar_one()
    if int(linked) == 0:
        raise ConnectError("no active admin is linked under this directory's issuer")
    connection_id = (
        await conn.execute(
            _UPSERT,
            {
                "id": new_id("dcn"),
                "org": org_id,
                "wo": workos_organization_id,
                "wd": workos_directory_id,
                "sso": sorted(set(sso_connection_ids)),
                "rule": join_rule,
                "admin": admin_group_ref,
            },
        )
    ).scalar_one()
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.DIRECTORY_CONNECTED,
            actor=actor,
            target_kind="directory_connection",
            target_id=str(connection_id),
            after={
                "state": "active",
                "join_rule": join_rule,
                "workos_directory_id": workos_directory_id,
            },
        ),
    )
    return str(connection_id)


async def freeze(conn: AsyncConnection, org_id: str, reason: FrozenReason, *, actor: Actor) -> bool:
    connection_id = (
        await conn.execute(_FREEZE, {"org": org_id, "reason": reason})
    ).scalar_one_or_none()
    if connection_id is None:
        return False
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.DIRECTORY_FROZEN,
            actor=actor,
            target_kind="directory_connection",
            target_id=str(connection_id),
            after={"state": "frozen", "reason": reason},
        ),
    )
    return True
