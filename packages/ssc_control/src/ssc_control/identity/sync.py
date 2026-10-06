"""Directory sync from WorkOS (SSC-019, decision 024).

Each tick reads the org's ``dsync.*`` events after the stored cursor and fetches the current state
of every user and group they name: an event is a trigger, never the truth. A full reconcile runs
on the first tick (after a new directory, too) and then every :data:`FULL_SYNC_SECONDS`; it lists
the whole directory and deactivates linked people the directory no longer has. What a tick
learns is applied, with the new cursor and health, in one org-bound transaction through
``ssc_control.directory``: people are keyed by ``(workos:<directory id>, idp_id)``, never by
email, and a deactivation revokes sessions.

Not members: guests (``userType`` guest) and directory users whose ``idp_id`` is missing or shaped
like an address. Roles follow ``admin_group_ref`` when it is set; the database refuses demoting
the last active admin, so that person keeps the role until another admin exists. ``dsync.deleted``
freezes the connection instead of deactivating everyone, and so does a full listing that comes
back empty while people are linked.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast

import httpx2
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts.audit import ActorKind
from ssc_control import directory
from ssc_control.audit.chain import Actor
from ssc_control.db import SqlState, bound_org
from ssc_control.identity import connections, sessions
from ssc_control.identity.connections import DirectoryConnection
from ssc_control.identity.rules import DirectoryPerson, ProfileError, is_member
from ssc_control.identity.workos import WorkOSClient, WorkOSError

log = logging.getLogger(__name__)

FULL_SYNC_SECONDS: Final = 6 * 3600
MAX_EVENT_PAGES: Final = 50
GROUP_REF_PREFIX: Final = "directory_group_"

type Json = dict[str, Any]


@dataclass(slots=True)
class Fetched:
    """What one tick learned from WorkOS. ``members`` hold the people WorkOS lists per group."""

    full: bool = False
    people: dict[str, DirectoryPerson] = field(default_factory=dict[str, DirectoryPerson])
    removed_subjects: set[str] = field(default_factory=set[str])
    group_names: dict[str, str] = field(default_factory=dict[str, str])
    members: dict[str, list[DirectoryPerson]] = field(
        default_factory=dict[str, list[DirectoryPerson]]
    )
    deleted_groups: set[str] = field(default_factory=set[str])
    admins: set[str] | None = None
    directory_deleted: bool = False
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class TickReport:
    org_id: str
    full: bool = False
    people: int = 0
    removed: int = 0
    frozen: bool = False
    error: str | None = None


_HEALTH = text(
    "update ssc.directory_connection set last_sync_ok_at = now(), last_error = null, "
    "event_cursor = coalesce(cast(:cursor as text), event_cursor), "
    "last_full_sync_at = case when cast(:full as boolean) then now() else last_full_sync_at end, "
    "updated_at = now() where org_id = :org"
)
_FAILED = text(
    "update ssc.directory_connection set last_error = :error, updated_at = now() "
    "where org_id = :org"
)
_DUE_FULL = text(
    "select last_full_sync_at is null, "
    "coalesce(last_full_sync_at < now() - make_interval(secs => :secs), true) "
    "from ssc.directory_connection where org_id = :org"
)
_LINKED = text(
    "select l.subject, u.id, u.role, u.status, u.display_name, u.email "
    "from ssc.identity_link l join ssc.user_account u on u.org_id = l.org_id and u.id = l.user_id "
    "where l.org_id = :org and l.issuer = :issuer"
)
_DIRECTORY_GROUPS = text(
    "select id, directory_ref from ssc.user_group where org_id = :org "
    "and starts_with(directory_ref, :prefix)"
)


def actor_for(connection: DirectoryConnection) -> Actor:
    return Actor(kind=ActorKind.INTEGRATION, id=connection.id)


def _person(raw: Mapping[str, object]) -> DirectoryPerson | None:
    try:
        return DirectoryPerson.from_wire(raw)
    except ProfileError:
        return None


def _people(raws: list[Json]) -> list[DirectoryPerson]:
    return [p for p in map(_person, raws) if p is not None]


def _nested(data: Mapping[str, object], key: str) -> Json:
    value = data.get(key)
    return cast(Json, value) if isinstance(value, dict) else {}


async def _fetch_full(client: WorkOSClient, connection: DirectoryConnection, out: Fetched) -> None:
    out.full = True
    for person in _people(await client.directory_users(connection.workos_directory_id)):
        out.people[person.id] = person
    for raw in await client.directory_groups(connection.workos_directory_id):
        gid, name = raw.get("id"), raw.get("name")
        if isinstance(gid, str):
            out.group_names[gid] = name if isinstance(name, str) and name else gid
    for gid in out.group_names:
        out.members[gid] = _people(await client.group_members(gid))
    if connection.admin_group_ref is not None:
        out.admins = {p.id for p in out.members.get(connection.admin_group_ref, ())}


async def _skip_to_end(client: WorkOSClient, connection: DirectoryConnection) -> str | None:
    """The newest event id: the first full reconcile supersedes everything before it."""
    cursor: str | None = None
    for _ in range(MAX_EVENT_PAGES * 20):
        page = await client.events(organization_id=connection.workos_organization_id, after=cursor)
        if not page.last_id:
            return cursor
        cursor = page.last_id
    return cursor


async def _fetch_events(  # noqa: C901, PLR0912  (one pass over the event kinds)
    client: WorkOSClient, connection: DirectoryConnection, out: Fetched
) -> None:
    """Events after the cursor, then the current state of what they name. Any failure raises
    before the new cursor is stored, so the same events are read again next tick."""
    cursor = connection.event_cursor
    user_ids: set[str] = set()
    removed: dict[str, str] = {}
    group_ids: set[str] = set()
    for _ in range(MAX_EVENT_PAGES):
        page = await client.events(organization_id=connection.workos_organization_id, after=cursor)
        if not page.events:
            break
        for event in page.events:
            kind = str(event.get("event", ""))
            data = _nested(event, "data")
            if kind in {"dsync.deleted", "dsync.activated"}:
                if data.get("id") == connection.workos_directory_id:
                    out.directory_deleted = kind == "dsync.deleted"
                    out.full = out.full or kind == "dsync.activated"
                continue
            if data.get("directory_id") != connection.workos_directory_id:
                continue
            if kind.startswith("dsync.user."):
                uid, idp = data.get("id"), data.get("idp_id")
                if isinstance(uid, str):
                    user_ids.add(uid)
                    if isinstance(idp, str):
                        removed[uid] = idp
            elif kind.startswith("dsync.group."):
                gid = (_nested(data, "group") or data).get("id")
                if isinstance(gid, str):
                    group_ids.add(gid)
        cursor = page.last_id or cursor
    if cursor != connection.event_cursor:
        out.cursor = cursor
    for uid in sorted(user_ids):
        raw = await client.directory_user(uid)
        person = None if raw is None else _person(raw)
        if person is not None:
            out.people[uid] = person
        elif uid in removed:
            out.removed_subjects.add(removed[uid])
    for gid in sorted(group_ids):
        try:
            out.members[gid] = _people(await client.group_members(gid))
        except WorkOSError as e:
            if e.status != 404:  # noqa: PLR2004
                raise
            out.deleted_groups.add(gid)
    if connection.admin_group_ref is not None and out.people:
        out.admins = set()
        for uid in out.people:
            groups = await client.user_groups(uid)
            if any(g.get("id") == connection.admin_group_ref for g in groups):
                out.admins.add(uid)


async def fetch(
    client: WorkOSClient, connection: DirectoryConnection, *, full_due: bool, first: bool = False
) -> Fetched:
    """``first``: never fully synced. Events before the first full listing are skipped; after it,
    a missing cursor means no event has been seen yet, so events are read from the start."""
    out = Fetched()
    if first:
        out.cursor = await _skip_to_end(client, connection)
    else:
        await _fetch_events(client, connection, out)
    if full_due or out.full or first:
        incremental = dict(out.people)
        await _fetch_full(client, connection, out)
        out.people = {**incremental, **out.people}
    return out


async def _upsert(
    conn: AsyncConnection, org_id: str, entry: directory.DirectoryUser, actor: Actor
) -> str:
    """``directory.upsert_user``; a refused demotion of the last admin keeps the admin role."""
    try:
        async with conn.begin_nested():
            return (await directory.upsert_user(conn, org_id, entry, actor=actor)).user_id
    except DBAPIError as e:
        if getattr(e.orig, "sqlstate", None) != SqlState.LAST_ORG_ADMIN.value:
            raise
    log.warning("directory sync kept the last active admin of %s", org_id)
    kept = directory.DirectoryUser(
        issuer=entry.issuer,
        subject=entry.subject,
        display_name=entry.display_name,
        email=entry.email,
        role="admin",
        status=entry.status,
    )
    return (await directory.upsert_user(conn, org_id, kept, actor=actor)).user_id


async def _linked(conn: AsyncConnection, connection: DirectoryConnection) -> dict[str, Any]:
    rows = await conn.execute(_LINKED, {"org": connection.org_id, "issuer": connection.issuer})
    return {str(r[0]): r for r in rows}


async def apply(conn: AsyncConnection, connection: DirectoryConnection, got: Fetched) -> TickReport:
    """Apply one tick in ``conn``'s org-bound transaction."""
    org, actor = connection.org_id, actor_for(connection)
    linked = await _linked(conn, connection)
    full_and_empty = got.full and not any(map(is_member, got.people.values())) and bool(linked)
    if got.directory_deleted or full_and_empty:
        await connections.freeze(conn, org, "directory_deleted", actor=actor)
        return TickReport(org, full=got.full, frozen=True)
    applied = 0
    for person in got.people.values():
        if not is_member(person):
            if person.guest:
                got.removed_subjects.add(person.idp_id)
            continue
        current = linked.get(person.idp_id)
        if got.admins is not None:
            admin = person.id in got.admins
        else:
            admin = current is not None and current[2] == "admin"
        entry = directory.DirectoryUser(
            issuer=connection.issuer,
            subject=person.idp_id,
            display_name=person.display_name,
            email=person.email,
            role="admin" if admin else "member",
            status="active" if person.active else "deactivated",
        )
        await _upsert(conn, org, entry, actor)
        applied += 1
    if got.full:
        present = {p.idp_id for p in got.people.values() if is_member(p)}
        got.removed_subjects |= set(linked) - present
    removed = 0
    for subject in sorted(got.removed_subjects):
        row = linked.get(subject)
        if row is None or row[3] == "deactivated":
            continue
        entry = directory.DirectoryUser(
            issuer=connection.issuer,
            subject=subject,
            display_name=str(row[4]),
            email=str(row[5]),
            role="admin" if row[2] == "admin" else "member",
            status="deactivated",
        )
        await _upsert(conn, org, entry, actor)
        removed += 1
    await _apply_groups(conn, connection, got, actor)
    await conn.execute(_HEALTH, {"org": org, "cursor": got.cursor, "full": got.full})
    await sessions.prune(conn, org)
    return TickReport(org, full=got.full, people=applied, removed=removed)


async def _apply_groups(
    conn: AsyncConnection, connection: DirectoryConnection, got: Fetched, actor: Actor
) -> None:
    org = connection.org_id
    by_subject = {s: str(r[1]) for s, r in (await _linked(conn, connection)).items()}
    for gid in sorted(got.members):
        users = sorted(
            {
                by_subject[p.idp_id]
                for p in got.members[gid]
                if is_member(p) and p.idp_id in by_subject
            }
        )
        group = await directory.upsert_group(
            conn,
            org,
            directory_ref=gid,
            display_name=got.group_names.get(gid, gid)[:200],
            actor=actor,
        )
        await directory.set_group_members(conn, org, group.group_id, users, actor=actor)
    stale = set(got.deleted_groups)
    rows = (await conn.execute(_DIRECTORY_GROUPS, {"org": org, "prefix": GROUP_REF_PREFIX})).all()
    if got.full:
        stale |= {str(ref) for _, ref in rows if str(ref) not in got.group_names}
    for group_id, ref in rows:
        if str(ref) in stale:
            await directory.set_group_members(conn, org, str(group_id), [], actor=actor)


async def tick(engine: AsyncEngine, client: WorkOSClient, org_id: str) -> TickReport | None:
    """One sync of one org. None when the org has no active connection or another tick won."""
    async with bound_org(engine, org_id) as conn:
        connection = await connections.load(conn, org_id)
        due = (
            await conn.execute(_DUE_FULL, {"org": org_id, "secs": FULL_SYNC_SECONDS})
        ).one_or_none()
    if connection is None or connection.frozen or due is None:
        return None
    try:
        got = await fetch(client, connection, full_due=bool(due[1]), first=bool(due[0]))
    except (WorkOSError, httpx2.HTTPError) as e:
        error = str(e)[:200] if isinstance(e, WorkOSError) else type(e).__name__
        async with bound_org(engine, org_id) as conn:
            await conn.execute(_FAILED, {"org": org_id, "error": error})
        log.warning("directory sync of %s failed: %s", org_id, error)
        return TickReport(org_id, error=error)
    async with bound_org(engine, org_id) as conn:
        locked = await connections.load(conn, org_id, for_update=True)
        if locked is None or locked.frozen or locked.event_cursor != connection.event_cursor:
            return None
        return await apply(conn, locked, got)
