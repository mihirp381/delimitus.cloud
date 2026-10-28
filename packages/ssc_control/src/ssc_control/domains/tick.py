"""The delegation tick (CD-4.1): ``pending_dns`` to ``issuing`` when every resolver answers our
name servers, ``issuing`` to ``active`` or ``failed`` on the certificate's state, ``failed`` after
72 hours without a delegation. Network calls run before the row's transaction; one row's error
never stops the others."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.domains import DomainState
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.db.bind import bound_org
from ssc_control.db.domains import (
    OrgDomain,
    lock_domain,
    mark_stalled,
    open_domains,
    update_state,
)
from ssc_control.domains.probe import ProbeError, Resolver, delegation_seen, observe
from ssc_control.runtime.cell_domains import CellDomainNotFoundError, CellDomainsError
from ssc_control.runtime.cells import CellPorts, CellUnavailableError

log = logging.getLogger("ssc.domains")

TICK_ACTOR: Final = Actor(ActorKind.OPERATOR, "system:domains_tick")
PENDING_LIMIT: Final = timedelta(hours=72)
STALL_AFTER: Final = timedelta(minutes=120)
NO_DELEGATION: Final = "no delegation seen in 72 hours"
NO_CERTIFICATE: Final = "the cell has no certificate for the domain"
CERTIFICATE_FAILED: Final = "the certificate failed"
STALLED_MARKER: Final = "domain_stalled"

_ACTIONS: Final[Mapping[DomainState, AuditAction]] = {
    "issuing": AuditAction.DOMAIN_DELEGATED,
    "active": AuditAction.DOMAIN_ACTIVE,
    "failed": AuditAction.DOMAIN_FAILED,
}


@dataclass(frozen=True, slots=True)
class Step:
    state: DomainState
    observed: Mapping[str, frozenset[str]] | None = None
    failure: str | None = None
    delegated: bool = False


@dataclass(frozen=True, slots=True)
class TickReport:
    checked: int
    changed: tuple[str, ...]
    stalled: tuple[str, ...]
    errors: tuple[str, ...]


async def tick(
    engine: AsyncEngine,
    cells: CellPorts | None,
    resolvers: Mapping[str, Resolver],
    now: datetime,
) -> TickReport:
    rows = await open_domains(engine)
    changed: list[str] = []
    stalled: list[str] = []
    errors: list[str] = []
    for row in rows:
        try:
            step = await _step(row, cells, resolvers, now)
            if step is None:
                continue
            outcome = await _apply(engine, row, step, now)
            if outcome == "changed":
                changed.append(row.id)
            elif outcome == "stalled":
                stalled.append(row.id)
        except Exception:
            log.exception("domain tick failed for a row", extra={"domain_id": row.id})
            errors.append(row.id)
    return TickReport(len(rows), tuple(changed), tuple(stalled), tuple(errors))


async def _step(
    row: OrgDomain,
    cells: CellPorts | None,
    resolvers: Mapping[str, Resolver],
    now: datetime,
) -> Step | None:
    if row.state == "pending_dns":
        return await _pending(row, resolvers, now)
    if row.state == "issuing":
        return await _issuing(row, cells)
    return None


async def _pending(row: OrgDomain, resolvers: Mapping[str, Resolver], now: datetime) -> Step:
    try:
        observed: Mapping[str, frozenset[str]] | None = await observe(resolvers, row.suffix)
    except ProbeError as exc:
        log.warning("domain probe failed: %s", exc, extra={"domain_id": row.id})
        observed = None
    if observed is not None and delegation_seen(observed, frozenset(row.name_servers)):
        return Step("issuing", observed, delegated=True)
    if now - row.created_at >= PENDING_LIMIT:
        return Step("failed", observed, NO_DELEGATION)
    return Step("pending_dns", observed)


async def _issuing(row: OrgDomain, cells: CellPorts | None) -> Step | None:
    try:
        agent = None if cells is None else (await cells.for_org(row.org_id)).domains
        if agent is None:
            return None
        status = await agent.status(row.suffix)
    except CellDomainNotFoundError:
        return Step("failed", failure=NO_CERTIFICATE)
    except (CellUnavailableError, CellDomainsError) as exc:
        log.warning("domain status unavailable: %s", exc, extra={"domain_id": row.id})
        return None
    if status.state == "active":
        return Step("active")
    if status.state == "failed":
        return Step("failed", failure=status.detail or CERTIFICATE_FAILED)
    return Step("issuing", failure=status.detail)


async def _apply(engine: AsyncEngine, seen: OrgDomain, step: Step, now: datetime) -> str | None:
    async with bound_org(engine, seen.org_id) as conn:
        row = await lock_domain(conn, seen.org_id, seen.id)
        if row is None or row.state != seen.state:
            return None
        if step.state == row.state:
            outcome = await _keep(conn, row, step, now)
        else:
            outcome = await _move(conn, row, step, now)
    if outcome == "stalled":
        _log_stalled(row)
    elif outcome == "changed":
        log.info(
            "domain %s: %s to %s",
            row.id,
            row.state,
            step.state,
            extra={"domain_id": row.id, "org_id": row.org_id},
        )
    return outcome


async def _keep(conn: AsyncConnection, row: OrgDomain, step: Step, now: datetime) -> str | None:
    if step.observed is not None or step.failure != row.failure:
        await update_state(
            conn,
            org_id=row.org_id,
            domain_id=row.id,
            state=row.state,
            observed_ns=step.observed,
            failure=step.failure,
        )
    return await _stall(conn, row, now)


async def _move(conn: AsyncConnection, row: OrgDomain, step: Step, now: datetime) -> str:
    after = await update_state(
        conn,
        org_id=row.org_id,
        domain_id=row.id,
        state=step.state,
        observed_ns=step.observed,
        failure=step.failure,
        last_delegated_at=now if step.delegated else None,
    )
    body: dict[str, object] = {"suffix": after.suffix, "state": after.state}
    if after.failure is not None:
        body["failure"] = after.failure
    if step.delegated:
        body["name_servers"] = list(after.name_servers)
    await append_event(
        conn,
        NewEvent(
            org_id=row.org_id,
            action=_ACTIONS[step.state],
            actor=TICK_ACTOR,
            target_kind="org_domain",
            target_id=row.id,
            before={"suffix": row.suffix, "state": row.state},
            after=body,
        ),
    )
    return "changed"


async def _stall(conn: AsyncConnection, row: OrgDomain, now: datetime) -> str | None:
    since = row.last_delegated_at
    if row.state != "issuing" or since is None or now - since < STALL_AFTER:
        return None
    return "stalled" if await mark_stalled(conn, row.org_id, row.id, now) else None


def _log_stalled(row: OrgDomain) -> None:
    since = row.last_delegated_at.isoformat() if row.last_delegated_at else "?"
    log.error(
        "%s: %s has been issuing since %s",
        STALLED_MARKER,
        row.suffix,
        since,
        extra={"marker": STALLED_MARKER, "domain_id": row.id, "org_id": row.org_id},
    )


__all__ = [
    "NO_DELEGATION",
    "PENDING_LIMIT",
    "STALLED_MARKER",
    "STALL_AFTER",
    "TICK_ACTOR",
    "TickReport",
    "tick",
]
