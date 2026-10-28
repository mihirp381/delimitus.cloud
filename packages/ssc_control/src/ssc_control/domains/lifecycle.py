"""Retiring an org's customer domain (CD-3): never while its parent still delegates it, unless
the 12-month cap has passed."""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import AuditAction
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.db.domains import OrgDomain, update_state
from ssc_control.domains.probe import delegation_gone
from ssc_control.runtime.cell_domains import CellDomains


class RetireRefusedError(Exception):
    pass


_KEEP_SUFFIX = text(
    "insert into ssc.org_suffix_retired (org_id, suffix, until) values (:org, :suffix, :until) "
    "on conflict do nothing"
)


def retire_allowed(row: OrgDomain, *, now: datetime) -> str | None:
    """None when the domain may be retired, else why not."""
    if row.state == "retired":
        return "the domain is already retired"
    if now >= row.retire_after:
        return None
    if row.observed_ns is None:
        return "no probe has answered for the domain yet"
    observed = {k: frozenset(v) for k, v in row.observed_ns.items()}
    if not delegation_gone(observed, frozenset(row.name_servers)):
        return f"the parent still delegates {row.suffix} to its zone"
    return None


async def retire(
    conn: AsyncConnection, agent: CellDomains, row: OrgDomain, actor: Actor, now: datetime
) -> OrgDomain:
    """Empty the zone through the agent, keep the suffix from reuse and mark the row retired,
    in ``conn``'s org-bound transaction."""
    refused = retire_allowed(row, now=now)
    if refused is not None:
        raise RetireRefusedError(refused)
    await agent.retire(row.suffix)
    await conn.execute(_KEEP_SUFFIX, {"org": row.org_id, "suffix": row.suffix, "until": now})
    retired = await update_state(conn, org_id=row.org_id, domain_id=row.id, state="retired")
    await append_event(
        conn,
        NewEvent(
            org_id=row.org_id,
            action=AuditAction.DOMAIN_RETIRED,
            actor=actor,
            target_kind="org_domain",
            target_id=row.id,
            before={"suffix": row.suffix, "state": row.state},
            after={"suffix": row.suffix, "state": "retired", "zone_name": row.zone_name},
        ),
    )
    return retired


__all__ = ["RetireRefusedError", "retire", "retire_allowed"]
