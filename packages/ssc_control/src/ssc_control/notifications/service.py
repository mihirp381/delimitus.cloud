"""Queue the mail an approval needs, in the caller's transaction (SSC-049).

A row in ``ssc.notification_outbox`` names a recipient and a template, never an address or a
text. Queuing also defers ``notify:send`` for the org in the same transaction, so the request
and its mail commit or roll back together and the worker has the mail within moments. Each row
has a ``dedupe_key``, so a repeated call queues nothing twice. The API imports this module and
never ``notifications.jobs``.
"""

from collections.abc import Sequence
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.ids import new_id
from ssc_control.deferral import defer

NAMESPACE: Final = "notify"
SEND_TASK: Final = f"{NAMESPACE}:send"

_APPROVERS = text(
    "select u.id from ssc.user_account u where u.org_id = :org and u.status = 'active' "
    "and u.id <> :requester and (u.role = 'admin' or u.id = (select c.owner_user_id "
    "from ssc.connection c where c.org_id = :org and c.name = :connection)) order by u.id"
)
_INSERT = text(
    "insert into ssc.notification_outbox (id, org_id, user_id, kind, approval_id, dedupe_key) "
    "values (:id, :org, :user, :kind, :approval, :key) "
    "on conflict (org_id, dedupe_key) do nothing returning id"
)


async def approvers(
    conn: AsyncConnection, *, org_id: str, requester_id: str, connection: str | None
) -> list[str]:
    """Who may decide a request: every active org admin and, for ``exceed_ceiling``, the named
    connection's owner. Never the requester."""
    rows = await conn.execute(
        _APPROVERS, {"org": org_id, "requester": requester_id, "connection": connection}
    )
    return list(rows.scalars())


async def queue(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    kind: str,
    approval_id: str | None,
    recipients: Sequence[str],
    key: str,
) -> int:
    """One row per recipient (``key:user``), then one send job for the org. How many rows were
    new."""
    queued = 0
    for user in recipients:
        row = await conn.execute(
            _INSERT,
            {
                "id": new_id("ntf"),
                "org": org_id,
                "user": user,
                "kind": kind,
                "approval": approval_id,
                "key": f"{key}:{user}",
            },
        )
        queued += row.scalar_one_or_none() is not None
    if queued:
        await defer(conn, SEND_TASK, queueing_lock=f"notify:{org_id}", org_id=org_id)
    return queued


async def arrived(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    approval_id: str,
    requester_id: str,
    connection: str | None,
    kind: str = "arrival",
) -> int:
    """Tell everyone who may decide the new request (``kind`` ``reminder`` for the reminder)."""
    who = await approvers(conn, org_id=org_id, requester_id=requester_id, connection=connection)
    return await queue(
        conn,
        org_id=org_id,
        kind=kind,
        approval_id=approval_id,
        recipients=who,
        key=f"{kind}:{approval_id}",
    )


async def decided(
    conn: AsyncConnection, *, org_id: str, approval_id: str, requester_id: str
) -> int:
    """Tell the requester how their request was decided."""
    return await queue(
        conn,
        org_id=org_id,
        kind="decided",
        approval_id=approval_id,
        recipients=[requester_id],
        key=f"decided:{approval_id}",
    )
