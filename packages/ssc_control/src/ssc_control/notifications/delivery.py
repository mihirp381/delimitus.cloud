"""Send the queued mail, remind, digest and prune (SSC-049). The worker's side of the outbox.

``flush`` takes the due rows ``FOR UPDATE SKIP LOCKED`` so two workers never send one mail twice,
reads the recipient's address only now, sends, and marks the row. A failure backs off 2, 4, 8
and 16 minutes and the fifth marks the row ``failed``; it never touches the approval. A row
whose request was decided meanwhile, or whose recipient left, is deleted unsent. Logs name the
row and the error type, never an address, a message or the server's reply.
"""

import logging
from collections.abc import Mapping
from datetime import date
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_control.approvals import service as approvals
from ssc_control.db.bind import bound_org
from ssc_control.notifications import mailer, messages
from ssc_control.notifications import service as outbox

log = logging.getLogger(__name__)

BATCH: Final = 50
MAX_ATTEMPTS: Final = 5
REMIND_AFTER_HOURS: Final = 72
KEEP_DAYS: Final = 30

_DUE = text(
    "select id, user_id, kind, approval_id, attempts from ssc.notification_outbox "
    "where state = 'pending' and next_attempt_at <= now() order by created_at, id "
    "limit :limit for update skip locked"
)
_RECIPIENT = text(
    "select email, role, status from ssc.user_account where org_id = :org and id = :id"
)
_SENT = text(
    "update ssc.notification_outbox set state = 'sent', sent_at = now(), attempts = attempts + 1 "
    "where org_id = :org and id = :id"
)
_RETRY = text(
    "update ssc.notification_outbox set attempts = attempts + 1, "
    "state = case when attempts + 1 >= :max then 'failed' else 'pending' end, "
    "next_attempt_at = now() + make_interval(mins => :wait) where org_id = :org and id = :id"
)
_DROP = text("delete from ssc.notification_outbox where org_id = :org and id = :id")
_PRUNE = text(
    "delete from ssc.notification_outbox where org_id = :org and state <> 'pending' "
    "and created_at < now() - make_interval(days => :days)"
)
_REMIND = text(
    "update ssc.approval_request set reminded_at = now() where org_id = :org and state = 'pending' "
    "and reminded_at is null and created_at <= now() - make_interval(hours => :hours) "
    "returning id, requested_by_user_id, payload"
)
_APPROVERS_WITH_WORK = text(
    "select u.id, u.role from ssc.user_account u where u.org_id = :org and u.status = 'active' "
    "and (u.role = 'admin' or exists (select 1 from ssc.approval_request r "
    "join ssc.connection c on c.org_id = r.org_id and c.name = r.payload ->> 'connection' "
    "where r.org_id = u.org_id and r.kind = 'exceed_ceiling' and r.state = 'pending' "
    "and c.owner_user_id = u.id)) order by u.id"
)


def _request(row: approvals.ApprovalRow) -> messages.Request:
    return messages.Request(
        id=row.id,
        kind=row.kind.value,
        subject_key=row.subject_key,
        payload=row.payload,
        app=row.app,
        environment=row.environment,
        requester=row.requested_by_name,
        state=row.state,
        decision_reason=row.decision_reason,
    )


async def _waiting(
    conn: AsyncConnection, org_id: str, user_id: str, *, admin: bool
) -> list[messages.Request]:
    rows = await approvals.search(
        conn,
        org_id=org_id,
        visible_to=None,
        decidable_by=user_id,
        decider_is_admin=admin,
        state="pending",
        environment_id=None,
        before=None,
        limit=messages.MAX_LISTED + 1,
    )
    return [_request(r) for r in rows]


async def _build(
    conn: AsyncConnection, org_id: str, row: Mapping[Any, Any], console_url: str
) -> mailer.Mail | None:
    """The mail for one outbox row, or None when it is no longer worth sending."""
    who = (await conn.execute(_RECIPIENT, {"org": org_id, "id": row["user_id"]})).first()
    if who is None or who[2] != "active":
        return None
    to, kind = str(who[0]), str(row["kind"])
    if kind == "digest":
        waiting = await _waiting(conn, org_id, str(row["user_id"]), admin=who[1] == "admin")
        if not waiting:
            return None
        subject, body = messages.digest(waiting, console_url)
        return mailer.Mail(to=to, subject=subject, body=body)
    approval = await approvals.get(conn, org_id=org_id, approval_id=str(row["approval_id"]))
    if approval is None:
        return None
    if (approval.state == "pending") == (kind == "decided"):
        return None
    request = _request(approval)
    if kind == "decided":
        subject, body = messages.decision(request, console_url)
    else:
        subject, body = messages.arrival(request, console_url, reminder=kind == "reminder")
    return mailer.Mail(to=to, subject=subject, body=body)


async def flush(
    engine: AsyncEngine, sender: mailer.Mailer, *, org_id: str, console_url: str
) -> int:
    """Send the org's due mail; how many went out."""
    sent = 0
    async with bound_org(engine, org_id) as conn:
        due = (await conn.execute(_DUE, {"limit": BATCH})).mappings().all()
        for row in due:
            key = {"org": org_id, "id": row["id"]}
            try:
                async with conn.begin_nested():
                    mail = await _build(conn, org_id, row, console_url)
                if mail is None:
                    await conn.execute(_DROP, key)
                    continue
                await sender.send(mail)
            except Exception as exc:
                log.warning(
                    "mail not sent",
                    extra={
                        "notification": row["id"],
                        "kind": row["kind"],
                        "error": type(exc).__name__,
                    },
                )
                wait = 2 ** int(row["attempts"])
                await conn.execute(_RETRY, {**key, "max": MAX_ATTEMPTS, "wait": wait})
                continue
            await conn.execute(_SENT, key)
            sent += 1
    return sent


async def remind(engine: AsyncEngine, *, org_id: str) -> int:
    """Queue one reminder for each request still pending after three days; how many."""
    async with bound_org(engine, org_id) as conn:
        due = (await conn.execute(_REMIND, {"org": org_id, "hours": REMIND_AFTER_HOURS})).all()
        for apr_id, requester, payload in due:
            await outbox.arrived(
                conn,
                org_id=org_id,
                approval_id=str(apr_id),
                requester_id=str(requester),
                connection=payload.get("connection"),
                kind="reminder",
            )
    return len(due)


async def digest(engine: AsyncEngine, *, org_id: str, day: date) -> int:
    """Queue the day's digest for each approver who has something to decide; how many."""
    queued = 0
    async with bound_org(engine, org_id) as conn:
        people = (await conn.execute(_APPROVERS_WITH_WORK, {"org": org_id})).all()
        for user_id, role in people:
            if not await _waiting(conn, org_id, str(user_id), admin=role == "admin"):
                continue
            queued += await outbox.queue(
                conn,
                org_id=org_id,
                kind="digest",
                approval_id=None,
                recipients=[str(user_id)],
                key=f"digest:{day.isoformat()}",
            )
    return queued


async def prune(engine: AsyncEngine, *, org_id: str) -> None:
    """Delete rows that are no longer pending and are older than 30 days."""
    async with bound_org(engine, org_id) as conn:
        await conn.execute(_PRUNE, {"org": org_id, "days": KEEP_DAYS})
