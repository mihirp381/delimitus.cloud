"""The GitHub task by name, and how callers defer it (decision 014). The API imports this
module, never ``github.jobs``.

One push job per app and commit is waiting at a time (``queueing_lock`` ``push:<app>:<sha>``),
so a redelivered webhook defers nothing; the job defers its own next step under the same lock.
"""

from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_control.deferral import defer

NAMESPACE: Final = "github"
RUN_PUSH: Final = f"{NAMESPACE}:run_push"


def push_lock(app_id: str, sha: str) -> str:
    return f"push:{app_id}:{sha}"


async def defer_push(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    app_id: str,
    sha: str,
    check_run_id: int | None = None,
    build_id: str | None = None,
    deployment_id: str | None = None,
    schedule_at: datetime | None = None,
) -> int | None:
    return await defer(
        conn,
        RUN_PUSH,
        queueing_lock=push_lock(app_id, sha),
        schedule_at=schedule_at,
        org_id=org_id,
        app_id=app_id,
        sha=sha,
        check_run_id=check_run_id,
        build_id=build_id,
        deployment_id=deployment_id,
    )
