"""The timer task by name, and how callers defer it (decision 014, decision 020).

The API and the timers service import this module, never ``timers.jobs``. A scheduled run is one
job per schedule and instant (``queueing_lock`` ``sch:<id>:<instant>``, deferred with
``schedule_at``); a manual run is one job per run. No timer job takes a ``lock``: Procrastinate
3.10 holds a later job behind an earlier one with the same lock even while the earlier one waits
for its ``schedule_at``, and a timer job never calls the runtime driver, so it needs no
``env:<id>`` lock. Overlap is refused by ``timer_run``'s partial unique indexes instead. A manual
run has ``MANUAL_TIMER_PRIORITY``, ahead of scheduled runs that are already due.
"""

from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_control.deferral import MANUAL_TIMER_PRIORITY, defer

NAMESPACE: Final = "timers"
RUN: Final = f"{NAMESPACE}:run"


def instant_key(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y%m%dT%H%MZ")


async def defer_scheduled_run(
    conn: AsyncConnection, *, org_id: str, schedule_id: str, instant: datetime
) -> int | None:
    """The run of ``schedule_id`` at ``instant``, in ``conn``'s transaction; None when it is
    already waiting."""
    return await defer(
        conn,
        RUN,
        queueing_lock=f"sch:{schedule_id}:{instant_key(instant)}",
        schedule_at=instant,
        org_id=org_id,
        schedule_id=schedule_id,
        scheduled_for=instant.astimezone(UTC).isoformat(),
    )


async def defer_manual_run(
    conn: AsyncConnection, *, org_id: str, schedule_id: str, run_id: str
) -> int | None:
    return await defer(
        conn,
        RUN,
        queueing_lock=f"tmr:{run_id}",
        priority=MANUAL_TIMER_PRIORITY,
        org_id=org_id,
        schedule_id=schedule_id,
        run_id=run_id,
    )
