"""Asking for a cell's lazy resources, and holding a deployment until they exist (SSC-087).

Every function here runs in the caller's org-bound transaction and locks the resource's row
before anything else, so a request, a waiting deployment and the job finishing are serialised
per cell and resource:

- ``request`` turns a resource on. A resource never asked for is inserted ``requested`` and its
  job deferred; a ``failed`` one is asked for again; one ``requested``, ``creating`` or ``ready``
  is left as it is (a second request joins the one in flight). Nothing here turns one off.
- ``hold_deployment`` is the deploy trigger: a manifest with ``[state] postgres = true`` needs the
  database. While it is not ``ready`` the deployment waits (a waiter row it owns) and the job
  re-defers it when the resource is ready or failed. A deployment woken by a failure fails with
  ``CELL_RESOURCE_FAILED``; a later one asks again.
- ``on_approval`` is the egress and connections trigger: an approved internet host or data
  source. ``request`` with ``connection_granted`` or ``file_use`` is the seam for granting a
  connection outside an approval and for SSC-046.

Audit rows are appended last, after every row lock (decision 020).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.cells import (
    MONTHLY_USD,
    NOTICE,
    CellResource,
    CellResourceCause,
    CellResourceState,
)
from ssc_contracts.manifest import Manifest
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.cell.tasks import defer_create
from ssc_control.domain.approval_rules import RequirementKind

CELL_RESOURCE_FAILED: Final = "CELL_RESOURCE_FAILED"
TARGET_KIND: Final = "cell_resource"

APPROVAL_TRIGGERS: Final = {
    RequirementKind.ENABLE_INTERNET_HOSTS: (CellResource.EGRESS, CellResourceCause.EGRESS_APPROVED),
    RequirementKind.CONNECT_DATA_SOURCE: (
        CellResource.CONNECTIONS,
        CellResourceCause.CONNECTION_GRANTED,
    ),
}

_LOCK = text("select * from ssc.cell_resource where org_id = :org and resource = :res for update")
_ALL = text("select * from ssc.cell_resource where org_id = :org")
_INSERT = text(
    "insert into ssc.cell_resource (org_id, resource, cause, actor_kind, actor_id, "
    "actor_via_agent, actor_client_id) values (:org, :res, :cause, :kind, :actor, :via, :client) "
    "on conflict (org_id, resource) do nothing returning *"
)
_REREQUEST = text(
    "update ssc.cell_resource set state = 'requested', cause = :cause, actor_kind = :kind, "
    "actor_id = :actor, actor_via_agent = :via, actor_client_id = :client, attempts = 0, "
    "execution = null, failure_code = null, last_error = null, requested_at = now(), "
    "started_at = null, failed_at = null "
    "where org_id = :org and resource = :res and state = 'failed' returning *"
)
_IS_WAITING = text(
    "select 1 from ssc.cell_resource_waiter "
    "where org_id = :org and resource = :res and deployment_id = :dep"
)
_WAIT = text(
    "insert into ssc.cell_resource_waiter (org_id, resource, deployment_id) "
    "values (:org, :res, :dep) on conflict do nothing"
)
_STOP_WAITING = text(
    "delete from ssc.cell_resource_waiter "
    "where org_id = :org and resource = :res and deployment_id = :dep"
)
_WAITING_ON = text(
    "select w.resource from ssc.cell_resource_waiter w "
    "join ssc.cell_resource r on r.org_id = w.org_id and r.resource = w.resource "
    "where w.org_id = :org and w.deployment_id = :dep and r.state in ('requested', 'creating') "
    "order by w.resource"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CellResourceRow:
    resource: CellResource
    state: CellResourceState
    cause: CellResourceCause
    attempts: int
    execution: str | None
    failure_code: str | None
    requested_at: datetime
    started_at: datetime | None
    ready_at: datetime | None
    failed_at: datetime | None
    actor: Actor

    @property
    def monthly_usd(self) -> int:
        return MONTHLY_USD[self.resource]


@dataclass(frozen=True, slots=True)
class Hold:
    """A deployment that must not go on yet: waiting for ``resources``, or failed with
    ``failure_code``."""

    resources: tuple[CellResource, ...]
    failure_code: str | None = None

    @property
    def notice(self) -> str | None:
        return notice_for(self.resources)


def row_of(row: Any) -> CellResourceRow:
    return CellResourceRow(
        resource=CellResource(row.resource),
        state=CellResourceState(row.state),
        cause=CellResourceCause(row.cause),
        attempts=int(row.attempts),
        execution=row.execution,
        failure_code=row.failure_code,
        requested_at=row.requested_at,
        started_at=row.started_at,
        ready_at=row.ready_at,
        failed_at=row.failed_at,
        actor=Actor(
            ActorKind(row.actor_kind),
            str(row.actor_id),
            via_agent=bool(row.actor_via_agent),
            client_id=row.actor_client_id,
        ),
    )


def needs_for(manifest: Manifest) -> tuple[CellResource, ...]:
    """The resources a deployment of ``manifest`` waits for."""
    return (CellResource.DATABASE,) if manifest.state.postgres else ()


def notice_for(resources: Sequence[CellResource]) -> str | None:
    """What a builder is told while a deployment waits for ``resources``."""
    return " ".join(NOTICE[r] for r in resources) or None


async def lock(
    conn: AsyncConnection, org_id: str, resource: CellResource
) -> CellResourceRow | None:
    row = (await conn.execute(_LOCK, {"org": org_id, "res": resource.value})).first()
    return None if row is None else row_of(row)


async def states(conn: AsyncConnection, org_id: str) -> dict[CellResource, CellResourceRow]:
    """Every resource the cell has asked for, by resource."""
    rows = (await conn.execute(_ALL, {"org": org_id})).all()
    return {r.resource: r for r in map(row_of, rows)}


async def waiting_on(
    conn: AsyncConnection, org_id: str, deployment_id: str
) -> tuple[CellResource, ...]:
    """The resources a deployment is waiting for now."""
    rows = await conn.execute(_WAITING_ON, {"org": org_id, "dep": deployment_id})
    return tuple(CellResource(r) for (r,) in rows)


@dataclass(frozen=True, slots=True)
class _Asked:
    """A resource this transaction asked for: the row after, and its audit view before."""

    row: CellResourceRow
    before: dict[str, Any] | None


async def _ensure(
    conn: AsyncConnection,
    org_id: str,
    resource: CellResource,
    cause: CellResourceCause,
    actor: Actor,
) -> tuple[CellResourceRow, _Asked | None]:
    """The locked row, and what this call asked for (None when it joined or found it ready)."""
    params = {
        "org": org_id,
        "res": resource.value,
        "cause": cause.value,
        "kind": actor.kind.value,
        "actor": actor.id,
        "via": actor.via_agent,
        "client": actor.client_id,
    }
    for _ in range(2):
        before = await lock(conn, org_id, resource)
        if before is not None and before.state is not CellResourceState.FAILED:
            return before, None
        sql = _INSERT if before is None else _REREQUEST
        written = (await conn.execute(sql, params)).first()
        if written is not None:
            row = row_of(written)
            view = (
                None
                if before is None
                else {"state": before.state.value, "failure_code": before.failure_code}
            )
            return row, _Asked(row, view)
    raise RuntimeError(f"cell resource {resource} kept changing under concurrent writers")


async def _announce(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    asked: _Asked,
    *,
    org_id: str,
    actor: Actor,
    deployment_id: str | None,
    policy_decision_id: str | None,
) -> None:
    """Defer the job and audit the request: the last writes of the caller's transaction."""
    await defer_create(conn, org_id=org_id, resource=asked.row.resource)
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.CELL_RESOURCE_REQUESTED,
            actor=actor,
            target_kind=TARGET_KIND,
            target_id=asked.row.resource.value,
            before=asked.before,
            after={
                "state": asked.row.state.value,
                "cause": asked.row.cause.value,
                "deployment_id": deployment_id,
            },
            policy_decision_id=policy_decision_id,
        ),
    )


async def request(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    resource: CellResource,
    cause: CellResourceCause,
    actor: Actor,
    policy_decision_id: str | None = None,
) -> CellResourceRow:
    """Turn ``resource`` on for the org's cell; the row after. Appends an audit row only when it
    was asked for now."""
    row, asked = await _ensure(conn, org_id, resource, cause, actor)
    if asked is not None:
        await _announce(
            conn,
            asked,
            org_id=org_id,
            actor=actor,
            deployment_id=None,
            policy_decision_id=policy_decision_id,
        )
    return row


async def on_approval(
    conn: AsyncConnection,
    *,
    org_id: str,
    kind: RequirementKind,
    actor: Actor,
    policy_decision_id: str | None,
) -> CellResourceRow | None:
    """An approved request: an internet host turns on ``egress``, a data source
    ``connections``. Other kinds turn nothing on."""
    trigger = APPROVAL_TRIGGERS.get(kind)
    if trigger is None:
        return None
    resource, cause = trigger
    return await request(
        conn,
        org_id=org_id,
        resource=resource,
        cause=cause,
        actor=actor,
        policy_decision_id=policy_decision_id,
    )


async def hold_deployment(
    conn: AsyncConnection, *, org_id: str, deployment_id: str, manifest: Manifest, actor: Actor
) -> Hold | None:
    """None when every resource ``manifest`` needs is ready; otherwise the deployment waits or,
    woken by a failure, fails. Call with the deployment row locked."""
    waiting: list[CellResource] = []
    announce: list[_Asked] = []
    for resource in needs_for(manifest):
        params = {"org": org_id, "res": resource.value, "dep": deployment_id}
        current = await lock(conn, org_id, resource)
        was_waiting = (await conn.execute(_IS_WAITING, params)).first() is not None
        if current is not None and current.state is CellResourceState.READY:
            await conn.execute(_STOP_WAITING, params)
            continue
        if current is not None and current.state is CellResourceState.FAILED and was_waiting:
            await conn.execute(_STOP_WAITING, params)
            return Hold((resource,), CELL_RESOURCE_FAILED)
        _, asked = await _ensure(conn, org_id, resource, CellResourceCause.DEPLOY, actor)
        await conn.execute(_WAIT, params)
        waiting.append(resource)
        if asked is not None:
            announce.append(asked)
    for asked in announce:
        await _announce(
            conn,
            asked,
            org_id=org_id,
            actor=actor,
            deployment_id=deployment_id,
            policy_decision_id=None,
        )
    return Hold(tuple(waiting)) if waiting else None
