"""The delegation tick (CD-4.1): a ``pending_dns`` row with no certificate yet has the cell
make its zone and certificate; ``pending_dns`` to ``issuing`` when every resolver answers our
name servers, ``issuing`` to ``active`` or ``failed`` on the certificate's state, ``failed`` after
72 hours without a delegation, once the cell has emptied the zone. ``active`` and ``failed``
queue a mail to every active admin; a stall also mails the operator when a notice is given.
Network calls run before the row's transaction; one row's error never stops the others."""

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
from ssc_control.domains.provider import parent_provider
from ssc_control.notifications import messages
from ssc_control.notifications import service as outbox
from ssc_control.notifications.mailer import Mail, Mailer
from ssc_control.runtime.cell_domains import (
    CellDomainNotFoundError,
    CellDomains,
    CellDomainsError,
    CreatedDomain,
)
from ssc_control.runtime.cells import CellPorts, CellUnavailableError

log = logging.getLogger("ssc.domains")

TICK_ACTOR: Final = Actor(ActorKind.OPERATOR, "system:domains_tick")
PENDING_LIMIT: Final = timedelta(hours=72)
STALL_AFTER: Final = timedelta(minutes=120)
NO_DELEGATION: Final = "no delegation seen in 72 hours"
NO_ZONE: Final = "the cell did not make the zone in 72 hours"
NO_CERTIFICATE: Final = "the cell has no certificate for the domain"
CERTIFICATE_FAILED: Final = "the certificate failed"
STALLED_MARKER: Final = "domain_stalled"

_ACTIONS: Final[Mapping[DomainState, AuditAction]] = {
    "issuing": AuditAction.DOMAIN_DELEGATED,
    "active": AuditAction.DOMAIN_ACTIVE,
    "failed": AuditAction.DOMAIN_FAILED,
}


class _NotEmptiedError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Step:
    state: DomainState
    observed: Mapping[str, frozenset[str]] | None = None
    failure: str | None = None
    delegated: bool = False
    made: CreatedDomain | None = None
    provider: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class StallNotice:
    mailer: Mailer
    to: str
    console_url: str


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
    *,
    notice: StallNotice | None = None,
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
            outcome = await _apply(engine, row, step, now, notice)
            if outcome == "changed":
                changed.append(row.id)
            elif outcome == "stalled":
                stalled.append(row.id)
        except _NotEmptiedError:
            errors.append(row.id)
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
    if row.state == "pending_dns" and row.certificate is None:
        return await _create(row, cells, resolvers, now)
    if row.state == "pending_dns":
        step = await _pending(row, resolvers, now)
    elif row.state == "issuing":
        step = await _issuing(row, cells)
    else:
        return None
    if step is not None and step.state == "failed" and not await _emptied(row, cells):
        raise _NotEmptiedError
    return step


async def _agent(row: OrgDomain, cells: CellPorts | None) -> CellDomains | None:
    return None if cells is None else (await cells.for_org(row.org_id)).domains


async def _create(
    row: OrgDomain, cells: CellPorts | None, resolvers: Mapping[str, Resolver], now: datetime
) -> Step | None:
    try:
        agent = await _agent(row, cells)
        made = None if agent is None else await agent.create(row.suffix)
    except (CellUnavailableError, CellDomainsError) as exc:
        log.warning("domain zone not made: %s", exc, extra={"domain_id": row.id})
        made = None
    if made is None:
        return Step("failed", failure=NO_ZONE) if now - row.created_at >= PENDING_LIMIT else None
    return Step("pending_dns", made=made, provider=await parent_provider(resolvers, row.suffix))


async def _emptied(row: OrgDomain, cells: CellPorts | None) -> bool:
    """A failed domain's zone is emptied before the row says so; False to try again."""
    try:
        agent = await _agent(row, cells)
        if agent is not None:
            await agent.retire(row.suffix)
    except CellDomainNotFoundError:
        pass
    except (CellUnavailableError, CellDomainsError) as exc:
        log.warning("domain zone not emptied: %s", exc, extra={"domain_id": row.id})
        return False
    return True


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
        agent = await _agent(row, cells)
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


async def _apply(
    engine: AsyncEngine, seen: OrgDomain, step: Step, now: datetime, notice: StallNotice | None
) -> str | None:
    async with bound_org(engine, seen.org_id) as conn:
        row = await lock_domain(conn, seen.org_id, seen.id)
        if row is None or row.state != seen.state:
            return None
        if step.made is not None:
            return await _made(conn, row, step)
        if step.state == row.state:
            outcome = await _keep(conn, row, step, now)
        else:
            outcome = await _move(conn, row, step, now)
    if outcome == "stalled":
        _log_stalled(row)
        if notice is not None:
            await _mail_stalled(notice, row)
    elif outcome == "changed":
        log.info(
            "domain %s: %s to %s",
            row.id,
            row.state,
            step.state,
            extra={"domain_id": row.id, "org_id": row.org_id},
        )
    return outcome


async def _made(conn: AsyncConnection, row: OrgDomain, step: Step) -> None:
    if row.certificate is not None or step.made is None:
        return
    await update_state(
        conn,
        org_id=row.org_id,
        domain_id=row.id,
        state=row.state,
        failure=row.failure,
        name_servers=step.made.name_servers,
        certificate=step.made.certificate,
        provider=step.provider,
    )
    log.info("domain %s: zone made", row.id, extra={"domain_id": row.id, "org_id": row.org_id})


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
    if after.state in {"active", "failed"}:
        await outbox.domain_changed(
            conn,
            org_id=row.org_id,
            domain_id=row.id,
            kind=f"domain_{after.state}",
            at=after.updated_at,
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


async def _mail_stalled(notice: StallNotice, row: OrgDomain) -> None:
    subject, body = messages.domain_stalled(row.org_id, row.suffix, row.failure, notice.console_url)
    try:
        await notice.mailer.send(Mail(to=notice.to, subject=subject, body=body))
    except Exception as exc:
        log.warning(
            "stall notice not sent",
            extra={"domain_id": row.id, "error": type(exc).__name__},
        )


__all__ = [
    "NO_DELEGATION",
    "NO_ZONE",
    "PENDING_LIMIT",
    "STALLED_MARKER",
    "STALL_AFTER",
    "StallNotice",
    "TICK_ACTOR",
    "TickReport",
    "tick",
]
