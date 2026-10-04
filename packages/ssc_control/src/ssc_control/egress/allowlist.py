"""The org's egress allowlist (SSC-053).

An org admin adds or removes a host pattern (``ssc_contracts.egress``); an admin approving an
app's ``enable_internet_hosts`` request adds its host too, with the request's id. Each change
runs in the caller's org-bound transaction: it marks the snapshot dirty, so the proxy reads it
with the next version, and audits ``org.updated`` on the ``egress_host`` target. The first host
turns on the cell's ``egress`` resource (SSC-087), which brings the proxy machine; one already
on is left as it is. Removing a host leaves the resource on: removing one is a runbook step.

The org row is locked (``FOR NO KEY UPDATE``) while the count is checked, so two admins adding
at once never pass ``MAX_HOSTS``, which the snapshot refuses.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.cells import CellResource, CellResourceCause
from ssc_contracts.egress import MAX_HOSTS, catalogue_entry, host_pattern_problem
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.cell import resources
from ssc_control.snapshot.service import mark_dirty

TARGET_KIND: Final = "egress_host"

_LIST = text(
    "select host, added_by_user_id, approval_request_id, created_at from ssc.egress_host "
    'where org_id = :org order by host collate "C"'
)
_LOCK_ORG = text("select 1 from ssc.org where id = :org for no key update")
_COUNT = text("select count(*) from ssc.egress_host where org_id = :org")
_INSERT = text(
    "insert into ssc.egress_host (org_id, host, added_by_user_id, approval_request_id) "
    "values (:org, :host, :by, :request) on conflict (org_id, host) do nothing returning host"
)
_DELETE = text(
    "delete from ssc.egress_host where org_id = :org and host = :host "
    "returning added_by_user_id, approval_request_id"
)


class EgressHostError(ValueError):
    """A host that is not an allowlist entry, or an allowlist already at ``MAX_HOSTS``.
    ``evidence`` says which, for the API's refusal."""

    def __init__(self, message: str, evidence: dict[str, object]) -> None:
        super().__init__(message)
        self.evidence = evidence


@dataclass(frozen=True, slots=True, kw_only=True)
class EgressHost:
    host: str
    added_by_user_id: str | None
    approval_request_id: str | None
    created_at: datetime

    @property
    def high_risk(self) -> bool:
        entry = catalogue_entry(self.host)
        return entry is not None and entry.high_risk


async def hosts(conn: AsyncConnection, org_id: str) -> list[EgressHost]:
    """The org's allowlist, by host."""
    rows = await conn.execute(_LIST, {"org": org_id})
    return [
        EgressHost(
            host=str(r.host),
            added_by_user_id=r.added_by_user_id,
            approval_request_id=r.approval_request_id,
            created_at=r.created_at,
        )
        for r in rows
    ]


def _view(host: str, approval_request_id: str | None) -> dict[str, object]:
    entry = catalogue_entry(host)
    return {
        "host": host,
        "high_risk": entry is not None and entry.high_risk,
        "approval_request_id": approval_request_id,
    }


async def allow(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    host: str,
    actor: Actor,
    approval_request_id: str | None = None,
    policy_decision_id: str | None = None,
) -> bool:
    """Add ``host``; False when it is already listed. Raises :class:`EgressHostError`."""
    problem = host_pattern_problem(host)
    if problem is not None:
        raise EgressHostError(f"{host!r}: {problem}", {"host": host, "problem": problem})
    await conn.execute(_LOCK_ORG, {"org": org_id})
    if int((await conn.execute(_COUNT, {"org": org_id})).scalar_one()) >= MAX_HOSTS:
        raise EgressHostError(f"the allowlist holds {MAX_HOSTS} hosts", {"max_hosts": MAX_HOSTS})
    params = {
        "org": org_id,
        "host": host,
        "by": actor.id if actor.kind is ActorKind.USER else None,
        "request": approval_request_id,
    }
    if (await conn.execute(_INSERT, params)).first() is None:
        return False
    await mark_dirty(conn, org_id)
    await resources.request(
        conn,
        org_id=org_id,
        resource=CellResource.EGRESS,
        cause=CellResourceCause.ADMIN
        if approval_request_id is None
        else CellResourceCause.EGRESS_APPROVED,
        actor=actor,
        policy_decision_id=policy_decision_id,
    )
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.ORG_UPDATED,
            actor=actor,
            target_kind=TARGET_KIND,
            target_id=host,
            after=_view(host, approval_request_id),
            policy_decision_id=policy_decision_id,
        ),
    )
    return True


async def remove(conn: AsyncConnection, *, org_id: str, host: str, actor: Actor) -> bool:
    """Remove ``host``; False when it was not listed. The proxy closes its open tunnels within
    ``DRAIN_SECONDS`` of reading the snapshot without it."""
    row = (await conn.execute(_DELETE, {"org": org_id, "host": host})).first()
    if row is None:
        return False
    await mark_dirty(conn, org_id)
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.ORG_UPDATED,
            actor=actor,
            target_kind=TARGET_KIND,
            target_id=host,
            before=_view(host, row.approval_request_id),
        ),
    )
    return True
