"""The lifecycle tasks by name, and how callers defer them (decision 014).

The API imports this module, never ``lifecycle.jobs``. At most one kill-switch job waits per app
(``queueing_lock`` ``kil:<app id>``). A job that scales an environment takes that environment's
``lock``, like every job that calls the runtime driver for it; the others take none.
"""

from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_control.deferral import KILL_SWITCH_PRIORITY, defer, env_lock

NAMESPACE: Final = "lifecycle"
RUN_KILL_SWITCH: Final = f"{NAMESPACE}:run_kill_switch"


def queueing_lock(app_id: str) -> str:
    return f"kil:{app_id}"


async def defer_kill_switch(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    app_id: str,
    run_id: str,
    env_id: str | None = None,
    schedule_at: datetime | None = None,
) -> int | None:
    """The next step of ``run_id``; ``env_id`` is the environment the job may scale."""
    return await defer(
        conn,
        RUN_KILL_SWITCH,
        queueing_lock=queueing_lock(app_id),
        lock=None if env_id is None else env_lock(env_id),
        schedule_at=schedule_at,
        priority=KILL_SWITCH_PRIORITY,
        org_id=org_id,
        run_id=run_id,
        env_id=env_id,
    )
