"""What removing an app deletes (GA-12), one step of the removal run at a time.

The run is the kill switch's in mode ``delete`` (``lifecycle.kill_switch``): it denies and stops
the app exactly as a disable does, then goes through ``STEPS``. A step here can always be run
again: what is already gone counts as removed.

``remove_timers`` deletes the app's schedules and turns its warm setting off. The four cell
steps work on one environment per job, in a job holding that environment's lock:
``drop_databases`` (the database, its roles and their sessions, then the ``ssc.app_database`` row,
which is the place given back), ``delete_files``, ``delete_secrets`` (every version, then the
``ssc.secret_ref`` and ``ssc.egress_credential`` rows) and ``delete_services`` (the service, its
revisions and the account it ran as). ``delete_bundles`` deletes the app's source bundles.
``remove_records`` deletes its grants, data-connection links and repository link, withdraws the
approval requests still pending for it, and marks the app ``deleted``.

Everything on the cell is named from an environment's id (``service_name``), which no other app
has or will get: nothing here takes a slug, a prefix a person typed or a pattern. The app's row,
its environments, releases, deployments, builds, runs, metrics and audit rows all stay.
"""

from collections.abc import Collection
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts import app_database, egress
from ssc_contracts.audit import AuditAction
from ssc_control.approvals import service as approvals
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.db.bind import bound_org
from ssc_control.deploy import bundle_gc
from ssc_control.github import links
from ssc_control.ports import TimersPort
from ssc_control.runtime.app_databases import AppDatabaseError
from ssc_control.runtime.cell_removal import CellRemovalError
from ssc_control.runtime.cells import OrgCell
from ssc_control.runtime.driver import service_name
from ssc_control.snapshot.service import mark_dirty
from ssc_control.storage import org_bundle_store
from ssc_control.worker_ports import Ports
from ssc_shared.runtime import secret_id

StepName = Literal[
    "remove_timers",
    "drop_databases",
    "delete_files",
    "delete_secrets",
    "delete_services",
    "delete_bundles",
    "remove_records",
]
CellStep = Literal["drop_databases", "delete_files", "delete_secrets", "delete_services"]

STEPS: Final[tuple[StepName, ...]] = (
    "remove_timers",
    "drop_databases",
    "delete_files",
    "delete_secrets",
    "delete_services",
    "delete_bundles",
    "remove_records",
)
"""After the kill switch's deny and stop. The service goes last on the cell: the agent removes a
database, a file or a secret only of a service it can see is stopped or gone."""
DELETING: Final = "deleting"
DELETED: Final = "deleted"
APP_DELETED_REASON: Final = "The app was deleted."

REMOVAL_UNAVAILABLE: Final = "REMOVAL_UNAVAILABLE"
DATABASE_UNAVAILABLE: Final = "DATABASE_UNAVAILABLE"
FILES_ERROR: Final = "FILES_ERROR"
SECRETS_ERROR: Final = "SECRETS_ERROR"

_SETTLE = text("select 1 from ssc.environment where org_id = :org and id = :env for update")
_HAS_DATABASE = text("select 1 from ssc.app_database where org_id = :org and environment_id = :env")
_DELETE_DATABASE = text(
    "delete from ssc.app_database where org_id = :org and environment_id = :env "
    "returning environment_id"
)
_SECRET_NAMES = text(
    "select name from ssc.secret_ref where org_id = :org and environment_id = :env order by name"
)
_DELETE_SECRETS = text(
    "delete from ssc.secret_ref where org_id = :org and environment_id = :env "
    "and (cast(:names as text[]) is null or name = any(cast(:names as text[]))) "
    "returning id, name, secret_version"
)
_DELETE_CREDENTIALS = text(
    "delete from ssc.egress_credential where org_id = :org and environment_id = :env"
)
_WARM_OFF = text(
    "update ssc.environment set warm = false where org_id = :org and app_id = :app and warm"
)
_DELETE_GRANTS = text(
    "delete from ssc.app_grant g using ssc.environment e "
    "where g.org_id = :org and e.org_id = g.org_id and e.id = g.environment_id "
    "and e.app_id = :app returning g.id, g.environment_id, g.role, g.subject_kind, "
    "coalesce(g.user_id, g.group_id) as subject_id"
)
_BUMP_GRANTS = text(
    "update ssc.environment set grants_version = grants_version + 1 "
    "where org_id = :org and id = any(cast(:envs as text[]))"
)
_DELETE_LINKS = text(
    "delete from ssc.connection_grant g using ssc.environment e "
    "where g.org_id = :org and e.org_id = g.org_id and e.id = g.environment_id "
    "and e.app_id = :app returning g.id, g.connection_id, g.environment_id"
)
_PENDING = text(
    "select r.id from ssc.approval_request r join ssc.environment e "
    "on e.org_id = r.org_id and e.id = r.environment_id "
    "where r.org_id = :org and e.app_id = :app and r.state = 'pending' "
    "order by r.id for update of r"
)
_SET_DELETED = text(
    "update ssc.app set status = 'deleted' "
    "where org_id = :org and id = :app and status = 'deleting'"
)


class RemovalError(Exception):
    """A step could not remove something; ``code`` is the reason the step records."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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


async def remove_timers(
    conn: AsyncConnection, timers: TimersPort, *, org_id: str, app_id: str, actor: Actor
) -> int:
    """Delete the app's schedules and turn its warm setting off, in the caller's transaction;
    how many schedules."""
    await conn.execute(_WARM_OFF, {"org": org_id, "app": app_id})
    return len(await timers.remove_for_app(conn, org_id=org_id, app_id=app_id, actor=actor))


async def in_cell(  # noqa: PLR0913  (keyword-only)
    engine: AsyncEngine, cell: OrgCell, step: CellStep, *, org_id: str, env_id: str, actor: Actor
) -> int:
    """One environment's part of a cell step; how many things it removed. The caller holds the
    environment's job lock, so no deployment or reconcile pass of it runs meanwhile. A request
    that locked the environment's row before the app was marked may still be calling the cell
    for it: this waits for that one, and every later one is refused by the app's status."""
    async with bound_org(engine, org_id) as conn:
        await conn.execute(_SETTLE, {"org": org_id, "env": env_id})
    match step:
        case "drop_databases":
            return await _drop_database(engine, cell, org_id=org_id, env_id=env_id, actor=actor)
        case "delete_files":
            return await _delete_files(cell, env_id)
        case "delete_secrets":
            return await _delete_secrets(engine, cell, org_id=org_id, env_id=env_id, actor=actor)
        case "delete_services":
            return await _delete_service(cell, env_id)


async def _drop_database(
    engine: AsyncEngine, cell: OrgCell, *, org_id: str, env_id: str, actor: Actor
) -> int:
    """The cell is asked even when no row says there is a database: a deployment that failed
    between making one and recording it left one. A cell that cannot be asked, or has no
    instance, while a row says it holds one is a failure: the row is the place, and forgetting
    it would leave the instance's place taken with no app to delete."""
    params = {"org": org_id, "env": env_id}
    async with bound_org(engine, org_id) as conn:
        recorded = (await conn.execute(_HAS_DATABASE, params)).first() is not None
    dropped = False
    if cell.app_databases is not None:
        try:
            dropped = await cell.app_databases.drop(service_name(env_id))
        except AppDatabaseError as exc:
            raise RemovalError(exc.code) from exc
    if recorded and not dropped:
        raise RemovalError(DATABASE_UNAVAILABLE)
    async with bound_org(engine, org_id) as conn:
        gone = (await conn.execute(_DELETE_DATABASE, params)).all()
        await _forget_secrets(conn, org_id, env_id, actor, names=app_database.SECRETS)
    return len(gone)


async def _delete_files(cell: OrgCell, env_id: str) -> int:
    if cell.removal is None:
        raise RemovalError(REMOVAL_UNAVAILABLE)
    try:
        return await cell.removal.drop_files(service_name(env_id))
    except CellRemovalError as exc:
        raise RemovalError(FILES_ERROR) from exc


async def _delete_secrets(
    engine: AsyncEngine, cell: OrgCell, *, org_id: str, env_id: str, actor: Actor
) -> int:
    """Every secret the environment was given, and the platform's own in case a row is missing.
    The agent cannot list a cell's secrets, so the names come from here."""
    if cell.removal is None:
        raise RemovalError(REMOVAL_UNAVAILABLE)
    params = {"org": org_id, "env": env_id}
    async with bound_org(engine, org_id) as conn:
        recorded = [str(name) for name in (await conn.execute(_SECRET_NAMES, params)).scalars()]
    service = service_name(env_id)
    for name in dict.fromkeys((*recorded, *app_database.SECRETS, *egress.SECRETS)):
        try:
            kept_there = await cell.removal.remove_secret(secret_id(service, name))
        except CellRemovalError as exc:
            raise RemovalError(SECRETS_ERROR) from exc
        if recorded and not kept_there:
            raise RemovalError(SECRETS_ERROR)
    async with bound_org(engine, org_id) as conn:
        await conn.execute(_DELETE_CREDENTIALS, params)
        return await _forget_secrets(conn, org_id, env_id, actor, names=None)


async def _forget_secrets(
    conn: AsyncConnection,
    org_id: str,
    env_id: str,
    actor: Actor,
    *,
    names: Collection[str] | None,
) -> int:
    """Delete the environment's ``secret_ref`` rows, those in ``names`` or all, each audited
    ``secret.removed`` after the last row is gone; how many."""
    params = {"org": org_id, "env": env_id, "names": None if names is None else list(names)}
    gone = (await conn.execute(_DELETE_SECRETS, params)).all()
    for ref_id, name, version in gone:
        await _audit(
            conn,
            org_id,
            actor,
            AuditAction.SECRET_REMOVED,
            target_kind="secret_ref",
            target_id=str(ref_id),
            before={"environment_id": env_id, "name": str(name), "version": str(version)},
        )
    return len(gone)


async def _delete_service(cell: OrgCell, env_id: str) -> int:
    if cell.runtime is None:
        raise RemovalError(REMOVAL_UNAVAILABLE)
    service = service_name(env_id)
    there = await cell.runtime.observe(service) is not None
    await cell.runtime.remove(service)
    return int(there)


async def delete_bundles(ports: Ports, *, org_id: str, app_id: str) -> int:
    """Delete the app's source bundles from the org's bundle store; how many objects."""
    store = await org_bundle_store(
        ports.engine, org_id, blob_store=ports.blob_store, cell_stores=ports.cell_stores
    )
    if store is None:
        return 0
    return await bundle_gc.remove_app(ports.engine, store, org_id, app_id)


async def remove_records(conn: AsyncConnection, *, org_id: str, app_id: str, actor: Actor) -> int:
    """Delete what ties the app to people, data and a repository, and mark it ``deleted``, in
    the caller's transaction; how many rows went. Row locks first, the audit rows after."""
    params = {"org": org_id, "app": app_id}
    await conn.execute(_SET_DELETED, params)
    grants = (await conn.execute(_DELETE_GRANTS, params)).mappings().all()
    shared = sorted({str(g["environment_id"]) for g in grants})
    if shared:
        await conn.execute(_BUMP_GRANTS, {"org": org_id, "envs": shared})
    linked = (await conn.execute(_DELETE_LINKS, params)).mappings().all()
    repository = await links.delete_link(conn, org_id, app_id)
    pending = [str(i) for i in (await conn.execute(_PENDING, params)).scalars()]
    await mark_dirty(conn, org_id)
    for grant in grants:
        await _audit(
            conn,
            org_id,
            actor,
            AuditAction.GRANT_REMOVED,
            target_kind="app_grant",
            target_id=str(grant["id"]),
            before={k: grant[k] for k in ("environment_id", "role", "subject_kind", "subject_id")},
        )
    for link in linked:
        await _audit(
            conn,
            org_id,
            actor,
            AuditAction.CONNECTION_REVOKED,
            target_kind="connection_grant",
            target_id=str(link["id"]),
            before={k: link[k] for k in ("connection_id", "environment_id")},
        )
    if repository is not None:
        await _audit(
            conn,
            org_id,
            actor,
            AuditAction.REPO_DISCONNECTED,
            target_kind="repo_link",
            target_id=app_id,
            before=repository.view(),
        )
    for approval_id in pending:
        await approvals.cancel(
            conn,
            org_id=org_id,
            approval_id=approval_id,
            user_id=None,
            channel=None,
            reason=APP_DELETED_REASON,
            actor=actor,
        )
    await _audit(
        conn,
        org_id,
        actor,
        AuditAction.APP_DELETED,
        target_kind="app",
        target_id=app_id,
        before={"status": DELETING},
        after={"status": DELETED},
    )
    return len(grants) + len(linked) + len(pending) + (repository is not None)


__all__ = [
    "DELETED",
    "DELETING",
    "STEPS",
    "CellStep",
    "RemovalError",
    "StepName",
    "delete_bundles",
    "in_cell",
    "remove_records",
    "remove_timers",
]
