"""Connections and their grants (SSC-052).

An org admin creates a connection by hand with the customer: its address is stored and never read
back. A connection stays ``pending`` until an admin sets it ``ready``; only ready connections
reach the snapshot. A grant links one environment to one connection. Each change runs in the
caller's org-bound transaction, is audited, and marks the org's access snapshot dirty.

The ceiling is checked when a sharing change widens an environment's audience, when a grant is
created, and when a ceiling changes. Changing a ceiling only flags: every environment now over it
gets ``over_ceiling_since`` and one ``connection.flagged`` row. Nothing is blocked at the data
gateway and no approval is opened for it. The flag clears when the environment's audience is back
inside every ceiling, or when a widening past it was approved. Directory membership is read at
the moment of each check and not watched afterwards.
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import AuditAction
from ssc_contracts.connections import AVAILABLE, SQL_KINDS, Address, Kind, SqlAddress, address_json
from ssc_contracts.ids import new_id
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.domain.approval_rules import GrantKey
from ssc_control.domain.audience import (
    ORG,
    Ceiling,
    ceiling_json,
    ceiling_view,
    exceeds,
    lowers,
    parse_ceiling,
    with_members,
)
from ssc_control.snapshot.service import mark_dirty

Problem = Literal["name_taken", "owner_not_active", "ceiling_required", "kind_unavailable"]
Classification = Literal["internal", "confidential", "restricted"]
SetupStatus = Literal["pending", "ready"]
Status = Literal["active", "suspended"]

CONNECTION: Final = "connection"
GRANT: Final = "connection_grant"
_COLUMNS: Final = (
    "id, name, kind, classification, owner_user_id, ceiling, allowed_schemas, limits, "
    "setup_status, status, created_at, updated_at"
)
_INSERT = text(
    "insert into ssc.connection (id, org_id, name, kind, classification, host, port, "
    "database_name, address, owner_user_id, ceiling, allowed_schemas, limits) values (:id, :org, "
    ":name, :kind, :class, :host, :port, :database, cast(:address as jsonb), :owner, "
    "cast(:ceiling as jsonb), cast(:schemas as text[]), cast(:limits as jsonb)) "
    "on conflict (org_id, name) do nothing returning id"
)
_OWNER_ACTIVE = text(
    "select 1 from ssc.user_account where org_id = :org and id = :id and status = 'active'"
)
_ONE = text(
    f"select {_COLUMNS} from ssc.connection where org_id = :org and name = :name"  # noqa: S608
)
_LOCK = text(
    f"select {_COLUMNS} from ssc.connection "  # noqa: S608  (constant column list)
    "where org_id = :org and name = :name for update"
)
_ALL = text(
    f"select {_COLUMNS} from ssc.connection where org_id = :org order by name"  # noqa: S608
)
_UPDATE = text(
    "update ssc.connection set owner_user_id = :owner, classification = :class, "
    "ceiling = cast(:ceiling as jsonb), allowed_schemas = cast(:schemas as text[]), "
    "limits = cast(:limits as jsonb), setup_status = :setup, status = :status, "
    "updated_at = now() where org_id = :org and id = :id"
)
_LINKS = text(
    "select g.id, g.environment_id, g.limits, g.over_ceiling_since, g.created_at, "
    "c.id, c.name, c.kind, c.classification, c.owner_user_id, c.ceiling, c.allowed_schemas, "
    "c.setup_status, c.status, c.limits, c.created_at, c.updated_at "
    "from ssc.connection_grant g join ssc.connection c "
    "on c.org_id = g.org_id and c.id = g.connection_id "
    "where g.org_id = :org and g.environment_id = :env order by c.name"
)
_OF_CONNECTION = text(
    "select id, environment_id, over_ceiling_since from ssc.connection_grant "
    "where org_id = :org and connection_id = :con order by environment_id for update"
)
_FIND_GRANT = text(
    "select id from ssc.connection_grant "
    "where org_id = :org and connection_id = :con and environment_id = :env for update"
)
_INSERT_GRANT = text(
    "insert into ssc.connection_grant (id, org_id, connection_id, environment_id, limits, "
    "created_by_user_id) values (:id, :org, :con, :env, cast(:limits as jsonb), :by)"
)
_SET_LIMITS = text(
    "update ssc.connection_grant set limits = cast(:limits as jsonb) "
    "where org_id = :org and id = :id"
)
_DELETE_GRANT = text(
    "delete from ssc.connection_grant where org_id = :org and connection_id = :con "
    "and environment_id = :env returning id"
)
_FLAG = text(
    "update ssc.connection_grant set over_ceiling_since = now() where org_id = :org and id = :id"
)
_CLEAR = text(
    "update ssc.connection_grant set over_ceiling_since = null where org_id = :org and id = :id"
)
_ENV_GRANTS = text(
    "select role, subject_kind, coalesce(user_id, group_id) from ssc.app_grant "
    "where org_id = :org and environment_id = :env"
)
_ACTIVE_MEMBERS = text(
    "select distinct m.user_id from ssc.group_member m join ssc.user_account u "
    "on u.org_id = m.org_id and u.id = m.user_id where m.org_id = :org "
    "and m.group_id = any(cast(:groups as text[])) and m.user_id = any(cast(:users as text[])) "
    "and u.status = 'active'"
)


class ConnectionChangeError(Exception):
    """A change the data does not allow; ``problem`` says which."""

    def __init__(self, problem: Problem) -> None:
        super().__init__(problem)
        self.problem: Problem = problem


@dataclass(frozen=True, slots=True, kw_only=True)
class Connection:
    """A connection as the API shows it: never its address."""

    id: str
    name: str
    kind: Kind
    classification: Classification
    owner_user_id: str | None
    ceiling: Ceiling
    allowed_schemas: tuple[str, ...]
    limits: dict[str, Any]
    setup_status: SetupStatus
    status: Status
    created_at: datetime
    updated_at: datetime

    def view(self) -> dict[str, Any]:
        """The ``connection`` audit view."""
        return {
            "name": self.name,
            "kind": self.kind,
            "classification": self.classification,
            "owner_user_id": self.owner_user_id,
            "ceiling": ceiling_view(self.ceiling),
            "allowed_schemas": list(self.allowed_schemas),
            "setup_status": self.setup_status,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class Link:
    """One environment's use of a connection."""

    id: str
    environment_id: str
    limits: dict[str, Any]
    over_ceiling_since: datetime | None
    created_at: datetime
    connection: Connection


def _connection(row: Mapping[str, Any]) -> Connection:
    return Connection(
        id=str(row["id"]),
        name=str(row["name"]),
        kind=cast(Kind, row["kind"]),
        classification=cast(Classification, row["classification"]),
        owner_user_id=row["owner_user_id"],
        ceiling=parse_ceiling(row["ceiling"]),
        allowed_schemas=tuple(row["allowed_schemas"]),
        limits=cast(dict[str, Any], row["limits"]),
        setup_status=cast(SetupStatus, row["setup_status"]),
        status=cast(Status, row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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
    policy_decision_id: str | None = None,
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
            policy_decision_id=policy_decision_id,
        ),
    )


async def _owner_active(conn: AsyncConnection, org_id: str, user_id: str) -> bool:
    found = await conn.execute(_OWNER_ACTIVE, {"org": org_id, "id": user_id})
    return found.first() is not None


async def get(conn: AsyncConnection, org_id: str, name: str) -> Connection | None:
    """The connection called ``name``, or None."""
    row = (await conn.execute(_ONE, {"org": org_id, "name": name})).mappings().first()
    return None if row is None else _connection(row)


async def list_all(conn: AsyncConnection, org_id: str) -> list[Connection]:
    """Every connection of the org, by name."""
    return [_connection(r) for r in (await conn.execute(_ALL, {"org": org_id})).mappings()]


async def create(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    actor: Actor,
    name: str,
    owner_user_id: str,
    classification: Classification,
    ceiling: Ceiling | None,
    allowed_schemas: list[str],
    limits: dict[str, Any],
    kind: Kind,
    address: Address,
) -> Connection:
    """A new ``pending`` connection. ``internal`` defaults to the whole-org ceiling; a
    ``confidential`` or ``restricted`` one needs ``ceiling`` (``ceiling_required``). The owner
    must be an active user (``owner_not_active``); a name in use is ``name_taken``; a kind
    without a connector is ``kind_unavailable``. ``address`` is the kind's (never a credential):
    a SQL kind's also fills the host, port and database columns."""
    if kind not in AVAILABLE:
        raise ConnectionChangeError("kind_unavailable")
    if (kind in SQL_KINDS) != isinstance(address, SqlAddress):
        raise ValueError(f"the address does not fit {kind}")
    if ceiling is None:
        if classification != "internal":
            raise ConnectionChangeError("ceiling_required")
        ceiling = ORG
    if not await _owner_active(conn, org_id, owner_user_id):
        raise ConnectionChangeError("owner_not_active")
    con_id = new_id("con")
    created = (
        await conn.execute(
            _INSERT,
            {
                "id": con_id,
                "org": org_id,
                "name": name,
                "kind": kind,
                "class": classification,
                "host": address.host if isinstance(address, SqlAddress) else None,
                "port": address.port if isinstance(address, SqlAddress) else None,
                "database": address.database if isinstance(address, SqlAddress) else None,
                "address": _dump(address_json(address)),
                "owner": owner_user_id,
                "ceiling": _dump(ceiling_json(ceiling)),
                "schemas": allowed_schemas,
                "limits": _dump(limits),
            },
        )
    ).scalar_one_or_none()
    if created is None:
        raise ConnectionChangeError("name_taken")
    made = await get(conn, org_id, name)
    if made is None:
        raise RuntimeError(f"connection {con_id} vanished inside its own transaction")
    await _audit(
        conn,
        org_id,
        actor,
        AuditAction.CONNECTION_CREATED,
        target_kind=CONNECTION,
        target_id=con_id,
        after=made.view(),
    )
    return made


async def _resolved(
    conn: AsyncConnection, org_id: str, ceiling: Ceiling, grants: Collection[GrantKey]
) -> Ceiling:
    """``ceiling`` with the users in ``grants`` who are, now, active members of a listed group."""
    if ceiling.subjects is None:
        return ceiling
    groups = sorted(i for k, i in ceiling.subjects if k == "group")
    users = sorted({sid for _, kind, sid in grants if kind == "user" and sid is not None})
    if not groups or not users:
        return ceiling
    rows = await conn.execute(_ACTIVE_MEMBERS, {"org": org_id, "groups": groups, "users": users})
    return with_members(ceiling, (str(u) for u in rows.scalars()))


async def links(conn: AsyncConnection, org_id: str, environment_id: str) -> list[Link]:
    """The connections one environment is granted, by name."""
    rows = (await conn.execute(_LINKS, {"org": org_id, "env": environment_id})).all()
    return [
        Link(
            id=str(r[0]),
            environment_id=str(r[1]),
            limits=cast(dict[str, Any], r[2]),
            over_ceiling_since=r[3],
            created_at=r[4],
            connection=Connection(
                id=str(r[5]),
                name=str(r[6]),
                kind=cast(Kind, r[7]),
                classification=cast(Classification, r[8]),
                owner_user_id=r[9],
                ceiling=parse_ceiling(r[10]),
                allowed_schemas=tuple(r[11]),
                limits=cast(dict[str, Any], r[14]),
                setup_status=cast(SetupStatus, r[12]),
                status=cast(Status, r[13]),
                created_at=r[15],
                updated_at=r[16],
            ),
        )
        for r in rows
    ]


async def ceilings(
    conn: AsyncConnection, org_id: str, environment_id: str, grants: Collection[GrantKey]
) -> dict[str, Ceiling]:
    """Each connection the environment uses, with its ceiling resolved against ``grants``."""
    return {
        link.connection.name: await _resolved(conn, org_id, link.connection.ceiling, grants)
        for link in await links(conn, org_id, environment_id)
    }


async def over_ceiling(
    conn: AsyncConnection, org_id: str, connection: Connection, grants: Collection[GrantKey]
) -> bool:
    """Whether ``grants`` give an audience beyond ``connection``'s ceiling, now."""
    return exceeds(await _resolved(conn, org_id, connection.ceiling, grants), grants)


async def environment_grants(
    conn: AsyncConnection, org_id: str, environment_id: str
) -> set[GrantKey]:
    """The environment's current sharing rules as grant keys."""
    rows = await conn.execute(_ENV_GRANTS, {"org": org_id, "env": environment_id})
    return {(str(r[0]), str(r[1]), None if r[2] is None else str(r[2])) for r in rows}


async def flag_over_ceiling(
    conn: AsyncConnection, org_id: str, connection: Connection, actor: Actor
) -> list[str]:
    """Flag every environment using ``connection`` whose audience is now over its ceiling, one
    ``connection.flagged`` row each, and clear the flag of those back inside. Opens no approval.
    The environments newly flagged."""
    flagged: list[str] = []
    rows = (await conn.execute(_OF_CONNECTION, {"org": org_id, "con": connection.id})).all()
    for grant_id, env_id, since in rows:
        grants = await environment_grants(conn, org_id, str(env_id))
        over = exceeds(await _resolved(conn, org_id, connection.ceiling, grants), grants)
        if over and since is None:
            await conn.execute(_FLAG, {"org": org_id, "id": grant_id})
            await _audit(
                conn,
                org_id,
                actor,
                AuditAction.CONNECTION_FLAGGED,
                target_kind=GRANT,
                target_id=str(grant_id),
                after={"connection_id": connection.id, "environment_id": str(env_id)},
            )
            flagged.append(str(env_id))
        elif not over and since is not None:
            await _clear(
                conn,
                org_id,
                connection_id=connection.id,
                grant_id=str(grant_id),
                env_id=str(env_id),
                actor=actor,
            )
    return flagged


async def _clear(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    org_id: str,
    *,
    connection_id: str,
    grant_id: str,
    env_id: str,
    actor: Actor,
) -> None:
    await conn.execute(_CLEAR, {"org": org_id, "id": grant_id})
    await _audit(
        conn,
        org_id,
        actor,
        AuditAction.CONNECTION_UPDATED,
        target_kind=GRANT,
        target_id=grant_id,
        before={"connection_id": connection_id, "environment_id": env_id},
        after={
            "connection_id": connection_id,
            "environment_id": env_id,
            "over_ceiling_since": None,
        },
    )


async def settle_environment(
    conn: AsyncConnection,
    org_id: str,
    environment_id: str,
    *,
    approved: Collection[str],
    actor: Actor,
) -> None:
    """After a sharing change applied: clear the flag of each connection the environment is
    now inside, and of each in ``approved`` (names whose ceiling the change was approved to
    exceed)."""
    grants = await environment_grants(conn, org_id, environment_id)
    for link in await links(conn, org_id, environment_id):
        if link.over_ceiling_since is None:
            continue
        ceiling = await _resolved(conn, org_id, link.connection.ceiling, grants)
        if link.connection.name in approved or not exceeds(ceiling, grants):
            await _clear(
                conn,
                org_id,
                connection_id=link.connection.id,
                grant_id=link.id,
                env_id=environment_id,
                actor=actor,
            )


async def update(
    conn: AsyncConnection,
    *,
    org_id: str,
    actor: Actor,
    name: str,
    changes: Mapping[str, Any],
) -> Connection | None:
    """Apply the fields in ``changes`` (``owner_user_id``, ``classification``, ``ceiling`` as a
    :class:`Ceiling`, ``allowed_schemas``, ``limits``, ``setup_status``, ``status``) to the
    connection called ``name``; None when there is none. A new ceiling flags the environments
    now over it. ``owner_not_active`` when the new owner is not an active user."""
    row = (await conn.execute(_LOCK, {"org": org_id, "name": name})).mappings().first()
    if row is None:
        return None
    before = _connection(row)
    if (
        changes.get("classification", "internal") != "internal"
        and before.classification == "internal"
        and "ceiling" not in changes
    ):
        raise ConnectionChangeError("ceiling_required")
    owner = changes.get("owner_user_id", before.owner_user_id)
    if owner != before.owner_user_id and not await _owner_active(conn, org_id, str(owner)):
        raise ConnectionChangeError("owner_not_active")
    ceiling: Ceiling = changes.get("ceiling", before.ceiling)
    await conn.execute(
        _UPDATE,
        {
            "org": org_id,
            "id": before.id,
            "owner": owner,
            "class": changes.get("classification", before.classification),
            "ceiling": _dump(ceiling_json(ceiling)),
            "schemas": list(changes.get("allowed_schemas", before.allowed_schemas)),
            "limits": _dump(changes.get("limits", before.limits)),
            "setup": changes.get("setup_status", before.setup_status),
            "status": changes.get("status", before.status),
        },
    )
    after = await get(conn, org_id, name)
    if after is None:
        raise RuntimeError(f"connection {before.id} vanished while it was locked")
    old, new = before.view(), after.view()
    changed = [k for k in new if new[k] != old[k]]
    await _audit(
        conn,
        org_id,
        actor,
        AuditAction.CONNECTION_UPDATED,
        target_kind=CONNECTION,
        target_id=before.id,
        before={k: old[k] for k in changed},
        after={k: new[k] for k in changed},
    )
    if lowers(before.ceiling, ceiling):
        await _audit(
            conn,
            org_id,
            actor,
            AuditAction.CONNECTION_CEILING_LOWERED,
            target_kind=CONNECTION,
            target_id=before.id,
            before={"ceiling": old["ceiling"]},
            after={"ceiling": new["ceiling"]},
        )
    if ceiling != before.ceiling:
        await flag_over_ceiling(conn, org_id, after, actor)
    await mark_dirty(conn, org_id)
    return after


async def grant(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    actor: Actor,
    connection: Connection,
    environment_id: str,
    limits: dict[str, Any],
    by_user_id: str,
    policy_decision_id: str | None,
) -> tuple[str, bool]:
    """Link the environment to ``connection``, or replace the limits of a link it has. The
    link's id, and whether it is new."""
    params = {"org": org_id, "con": connection.id, "env": environment_id}
    found = (await conn.execute(_FIND_GRANT, params)).scalar_one_or_none()
    if found is not None:
        await conn.execute(_SET_LIMITS, {"org": org_id, "id": found, "limits": _dump(limits)})
        await mark_dirty(conn, org_id)
        return str(found), False
    grant_id = new_id("cgr")
    await conn.execute(
        _INSERT_GRANT,
        {**params, "id": grant_id, "limits": _dump(limits), "by": by_user_id},
    )
    await _audit(
        conn,
        org_id,
        actor,
        AuditAction.CONNECTION_GRANTED,
        target_kind=GRANT,
        target_id=grant_id,
        after={"connection_id": connection.id, "environment_id": environment_id},
        policy_decision_id=policy_decision_id,
    )
    await mark_dirty(conn, org_id)
    return grant_id, True


async def revoke(
    conn: AsyncConnection,
    *,
    org_id: str,
    actor: Actor,
    connection: Connection,
    environment_id: str,
) -> bool:
    """Remove the environment's link to ``connection``; False when it had none."""
    removed = (
        await conn.execute(
            _DELETE_GRANT, {"org": org_id, "con": connection.id, "env": environment_id}
        )
    ).scalar_one_or_none()
    if removed is None:
        return False
    await _audit(
        conn,
        org_id,
        actor,
        AuditAction.CONNECTION_REVOKED,
        target_kind=GRANT,
        target_id=str(removed),
        before={"connection_id": connection.id, "environment_id": environment_id},
    )
    await mark_dirty(conn, org_id)
    return True
