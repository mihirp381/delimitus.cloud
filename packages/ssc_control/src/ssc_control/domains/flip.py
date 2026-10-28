"""Moving an org's apps to its customer suffix and back (CD-4.2), in one transaction: the
suffix that was current stays recognised for 30 days as a previous one."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import AuditAction
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.db.domains import domain_of
from ssc_control.db.orgs import suffix_of
from ssc_control.snapshot.service import mark_dirty

type FlipTo = Literal["customer", "default"]
PREVIOUS_FOR: Final = timedelta(days=30)

_LOCK_ORG = text("select cell_label, apps_suffix from ssc.org where id = :org for update")
_SET_SUFFIX = text("update ssc.org set apps_suffix = :suffix where id = :org")
_FORGET = text("delete from ssc.org_suffix_retired where org_id = :org and suffix = :suffix")
_KEEP = text(
    "insert into ssc.org_suffix_retired (org_id, suffix, until) values (:org, :suffix, :until)"
)


class FlipRefusedError(Exception):
    def __init__(self, reason: Literal["state", "not_ready"], message: str, rolling: int = 0):
        super().__init__(message)
        self.reason = reason
        self.rolling = rolling


@dataclass(frozen=True, slots=True)
class Flipped:
    before: str
    after: str
    version: int


async def flip(  # noqa: PLR0913
    conn: AsyncConnection,
    org_id: str,
    to: FlipTo,
    actor: Actor,
    now: datetime,
    *,
    apps_domain: str,
    rolling: int,
) -> Flipped:
    """``rolling`` is how many environments have not yet rolled for the coming suffix."""
    org = (await conn.execute(_LOCK_ORG, {"org": org_id})).one()
    if org.cell_label is None:
        raise FlipRefusedError("state", "the organization has no cell")
    default = suffix_of(org.cell_label, None, apps_domain)
    current = suffix_of(org.cell_label, org.apps_suffix, apps_domain)
    row = await domain_of(conn, org_id)
    if to == "customer":
        if row is None or row.state != "active":
            raise FlipRefusedError("state", "the customer domain is not active")
        if row.suffix == org.apps_suffix:
            raise FlipRefusedError("state", "the organization already uses the customer domain")
        coming, column = row.suffix, row.suffix
    else:
        if org.apps_suffix is None:
            raise FlipRefusedError("state", "the organization already uses its default suffix")
        coming, column = default, None
    if rolling:
        raise FlipRefusedError("not_ready", "apps are still rolling", rolling)
    await conn.execute(_FORGET, {"org": org_id, "suffix": coming})
    await conn.execute(_FORGET, {"org": org_id, "suffix": current})
    await conn.execute(_SET_SUFFIX, {"org": org_id, "suffix": column})
    await conn.execute(_KEEP, {"org": org_id, "suffix": current, "until": now + PREVIOUS_FOR})
    target = ("org_domain", row.id) if row is not None else ("org", org_id)
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.DOMAIN_FLIPPED,
            actor=actor,
            target_kind=target[0],
            target_id=target[1],
            before={"suffix": current} if row is not None else None,
            after={"suffix": coming, "to": to} if row is not None else None,
        ),
    )
    version = await mark_dirty(conn, org_id)
    return Flipped(before=current, after=coming, version=version)


__all__ = ["PREVIOUS_FOR", "FlipRefusedError", "FlipTo", "Flipped", "flip"]
