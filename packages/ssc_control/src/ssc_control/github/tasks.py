"""The GitHub task by name, and how callers defer it (decision 014). The API imports this
module, never ``github.jobs``.

One push job per app and commit waits for a delivery at a time (``queueing_lock``
``push:<app>:<sha>``), so a redelivered webhook defers nothing while one waits. The job defers
its own next step under ``push:<app>:<sha>:next``, a queueing lock no delivery takes: a
redelivery that lands while a step runs can never crowd that step's next one out.
"""

from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_control.deferral import defer

NAMESPACE: Final = "github"
RUN_PUSH: Final = f"{NAMESPACE}:run_push"


def push_lock(app_id: str, sha: str) -> str:
    return f"push:{app_id}:{sha}"


def next_lock(app_id: str, sha: str) -> str:
    """The queueing lock of a push job's own next step."""
    return f"{push_lock(app_id, sha)}:next"


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
    follow_up = check_run_id is not None or build_id is not None or deployment_id is not None
    return await defer(
        conn,
        RUN_PUSH,
        queueing_lock=next_lock(app_id, sha) if follow_up else push_lock(app_id, sha),
        schedule_at=schedule_at,
        org_id=org_id,
        app_id=app_id,
        sha=sha,
        check_run_id=check_run_id,
        build_id=build_id,
        deployment_id=deployment_id,
    )
