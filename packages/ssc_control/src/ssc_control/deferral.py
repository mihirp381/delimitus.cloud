"""Defer a Procrastinate job in the caller's transaction (decision 008, decision 014).

The job commits or rolls back with the caller's own rows, because it is written through the
caller's connection, inside a savepoint. A duplicate ``queueing_lock`` is refused by Procrastinate
with ``AlreadyEnqueued``; the savepoint rolls back just that insert, so the caller's transaction
stays usable, and ``defer`` returns None.

Procrastinate lives in schema ``procrastinate`` and its SQL names its functions and types
unqualified. Inside the savepoint, ``search_path`` is set to that schema for the one insert and put
back after it (a rolled-back savepoint puts it back by itself), so the caller's connection and
engine are left as they were.

The API imports this module and nothing from ``ssc_control.worker``: tasks are named by string
(``"<namespace>:<task>"``) and need not be importable here. The job priorities are all here, so
their order is in one place.
"""

from datetime import datetime
from typing import Final

from procrastinate import App, PsycopgConnector
from procrastinate.exceptions import AlreadyEnqueued
from procrastinate.types import JSONValue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_control.db.catalog import QUEUE_SCHEMA

# Never opened: every defer passes the caller's connection, so the connector's pool is never used.
_APP: Final = App(connector=PsycopgConnector())
_CURRENT_PATH: Final = text("select current_setting('search_path')")
_SET_PATH: Final = text("select set_config('search_path', :path, true)")
KILL_SWITCH_PRIORITY: Final = 20
ROLLBACK_PRIORITY: Final = 10
MANUAL_TIMER_PRIORITY: Final = 1
"""Job priorities, highest first; every other job has Procrastinate's default 0. A worker
takes the highest waiting job, and among jobs waiting on one ``lock`` the highest goes first."""


class DeferralError(RuntimeError):
    pass


def env_lock(env_id: str) -> str:
    """The ``lock`` every job that calls the runtime driver for ``env_id`` takes (decision 014)."""
    return f"env:{env_id}"


async def defer(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    task_name: str,
    *,
    queueing_lock: str | None,
    lock: str | None = None,
    queue: str | None = None,
    schedule_at: datetime | None = None,
    priority: int | None = None,
    **task_kwargs: JSONValue,
) -> int | None:
    """Defer ``task_name`` with ``task_kwargs`` in ``conn``'s open transaction.

    Returns the job id, or None when ``queueing_lock`` already has a job waiting. ``lock``
    serialises jobs that share it; ``queue`` defaults to Procrastinate's ``default``.
    """
    if not conn.in_transaction():
        raise DeferralError("defer needs the caller's open transaction; the job rides it")
    raw = (await conn.get_raw_connection()).driver_connection
    if raw is None:
        raise DeferralError("no driver connection behind the SQLAlchemy connection")
    deferrer = _APP.configure_task(
        task_name,
        allow_unknown=True,
        connection=raw,
        queueing_lock=queueing_lock,
        lock=lock,
        queue=queue,
        schedule_at=schedule_at,
        priority=priority,
    )
    previous = (await conn.execute(_CURRENT_PATH)).scalar_one()
    try:
        async with conn.begin_nested():
            await conn.execute(_SET_PATH, {"path": QUEUE_SCHEMA})
            job_id = await deferrer.defer_async(**task_kwargs)
            await conn.execute(_SET_PATH, {"path": previous})
    except AlreadyEnqueued:
        return None
    return job_id
