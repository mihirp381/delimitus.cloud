"""The cell tasks by name, and how callers defer them (SSC-087, SSC-092).

One job per cell and resource (``queueing_lock`` ``cellres:<org>:<resource>``), one per cell for
the gateway's warm setting (``cellwarm:<org>``), and the jobs of one cell take the cell's
``lock``, so two steps for one cell never run at once. Callers import this module, never
``cell.jobs``.
"""

from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.cells import CellResource
from ssc_control.deferral import defer

NAMESPACE: Final = "cell"
CREATE_RESOURCE: Final = f"{NAMESPACE}:create_resource"
WARM_GATEWAY: Final = f"{NAMESPACE}:warm_gateway"


def cell_lock(org_id: str) -> str:
    return f"cell:{org_id}"


async def defer_create(
    conn: AsyncConnection,
    *,
    org_id: str,
    resource: CellResource,
    schedule_at: datetime | None = None,
) -> int | None:
    return await defer(
        conn,
        CREATE_RESOURCE,
        queueing_lock=f"cellres:{org_id}:{resource.value}",
        lock=cell_lock(org_id),
        schedule_at=schedule_at,
        org_id=org_id,
        resource=resource.value,
    )


async def defer_warm_gateway(
    conn: AsyncConnection, *, org_id: str, schedule_at: datetime | None = None
) -> int | None:
    return await defer(
        conn,
        WARM_GATEWAY,
        queueing_lock=f"cellwarm:{org_id}",
        lock=cell_lock(org_id),
        schedule_at=schedule_at,
        org_id=org_id,
    )
