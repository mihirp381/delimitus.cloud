"""The deploy tasks by name, and how callers defer them (decision 014).

The API imports this module, never ``deploy.jobs``. A build job is one per build
(``queueing_lock``); a deployment job also takes the environment's ``lock``, like every job that
calls the runtime driver for that environment.
"""

from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_control.deferral import defer, env_lock

NAMESPACE: Final = "deploy"
RUN_BUILD: Final = f"{NAMESPACE}:run_build"
RUN_DEPLOYMENT: Final = f"{NAMESPACE}:run_deployment"
COLLECT_BUNDLES: Final = f"{NAMESPACE}:collect_bundles"


async def defer_build(
    conn: AsyncConnection, *, org_id: str, build_id: str, schedule_at: datetime | None = None
) -> int | None:
    return await defer(
        conn,
        RUN_BUILD,
        queueing_lock=f"bld:{build_id}",
        schedule_at=schedule_at,
        org_id=org_id,
        build_id=build_id,
    )


async def defer_deployment(
    conn: AsyncConnection, *, org_id: str, environment_id: str, deployment_id: str
) -> int | None:
    return await defer(
        conn,
        RUN_DEPLOYMENT,
        queueing_lock=f"dep:{deployment_id}",
        lock=env_lock(environment_id),
        org_id=org_id,
        deployment_id=deployment_id,
    )
