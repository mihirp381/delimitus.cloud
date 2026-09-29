"""Mark an org's snapshot dirty, record a cell's acknowledgement, and the ``SnapshotPort``.

``mark_dirty`` runs in the caller's transaction: it takes the org's snapshot lock shared, defers
one compile (coalesced by ``queueing_lock``), and returns the version that will contain the
caller's change. While the caller holds the lock no compile can start, so the next compile,
which is that version, reads after the caller commits.
"""

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_control.db.bind import bound_org
from ssc_control.deferral import defer
from ssc_control.ports import SnapshotPort
from ssc_control.snapshot.compiler import LOCK_CLASS, LOCK_SHARED, NEXT_VERSION

NAMESPACE: Final = "snapshot"
COMPILE_TASK: Final = f"{NAMESPACE}:compile"

_UPSERT_ACK = text(
    "insert into ssc.snapshot_ack (org_id, cell_label, version, acked_at) "
    "values (:org, :cell, :version, now()) on conflict (org_id) do update set "
    "cell_label = excluded.cell_label, version = excluded.version, acked_at = excluded.acked_at"
)
_CONFIRMED = text(
    "select 1 from ssc.snapshot_ack k join ssc.org o on o.id = k.org_id "
    "where k.org_id = :org and k.version >= :version "
    "and (o.cell_label is null or o.cell_label = k.cell_label)"
)


def compile_lock(org_id: str) -> str:
    """The Procrastinate ``lock`` and ``queueing_lock`` of the org's compile job."""
    return f"snapshot:{org_id}"


async def mark_dirty(conn: AsyncConnection, org_id: str) -> int:
    """Schedule a compile in ``conn``'s open transaction; the version that will include it."""
    await conn.execute(LOCK_SHARED, {"cls": LOCK_CLASS, "org": org_id})
    await defer(
        conn,
        COMPILE_TASK,
        queueing_lock=compile_lock(org_id),
        lock=compile_lock(org_id),
        org_id=org_id,
    )
    return int((await conn.execute(NEXT_VERSION, {"org": org_id})).scalar_one())


async def record_ack(conn: AsyncConnection, org_id: str, *, cell_label: str, version: int) -> None:
    """The cell's applied version, as its latest heartbeat reports it (a lower one replaces a
    higher one: the report is the truth). An unpublished version is a foreign-key refusal."""
    await conn.execute(_UPSERT_ACK, {"org": org_id, "cell": cell_label, "version": version})


class Snapshots(SnapshotPort):
    """The ``SnapshotPort``. ``confirmed`` needs the org's cell (one per org) to have reported
    ``version`` or later; with no report it is never confirmed."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def request(self, conn: AsyncConnection, org_id: str) -> int:
        return await mark_dirty(conn, org_id)

    async def confirmed(self, org_id: str, version: int) -> bool:
        async with bound_org(self._engine, org_id) as conn:
            row = (await conn.execute(_CONFIRMED, {"org": org_id, "version": version})).first()
        return row is not None
