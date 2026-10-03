"""One step of creating a lazy resource (SSC-087): the ``cell:create_resource`` job.

Each step locks the resource's row, does one thing and, unless the resource is ``ready`` or
``failed``, defers the next step. It is safe to run twice and safe to kill at any point:

1. ``requested``, or ``creating`` with no run recorded: count an attempt and mark ``creating``,
   commit, start a deployer run, record its execution. A step killed before recording it starts
   another run next time; the deployer waits out or recovers from the first (its state lock).
2. ``creating`` with a run: read it. Running: look again in ``POLL_SECONDS``. Failed: after
   ``MAX_ATTEMPTS`` attempts the resource is ``failed`` with ``CELL_DEPLOYER_FAILED``, before
   that a new run after a back-off. Succeeded: ``ready``.
3. ``ready`` or ``failed``: one transaction audits it (only when this step made it so), in the
   name of whoever asked for the resource, and re-defers every deployment waiting for it.

With no deployer configured the resource fails at once with ``CELL_DEPLOYER_UNAVAILABLE``.
"""

import logging
from datetime import timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import AuditAction
from ssc_contracts.cells import CellResource, CellResourceState
from ssc_control.audit import NewEvent, append_event
from ssc_control.cell.deployer import CellDeployerError
from ssc_control.cell.resources import TARGET_KIND, CellResourceRow, lock
from ssc_control.cell.tasks import defer_create
from ssc_control.db.bind import bound_org
from ssc_control.deploy.tasks import defer_deployment
from ssc_control.worker_ports import Ports

log = logging.getLogger(__name__)

POLL_SECONDS: Final = 30.0
MAX_ATTEMPTS: Final = 3
BACKOFF_SECONDS: Final = (60.0, 300.0)
CELL_DEPLOYER_FAILED: Final = "CELL_DEPLOYER_FAILED"
CELL_DEPLOYER_UNAVAILABLE: Final = "CELL_DEPLOYER_UNAVAILABLE"

_LABEL = text("select cell_label from ssc.org where id = :org")
_ATTEMPT = text(
    "update ssc.cell_resource set state = 'creating', attempts = attempts + 1, "
    "started_at = coalesce(started_at, now()), execution = null "
    "where org_id = :org and resource = :res and state in ('requested', 'creating')"
)
_RECORD = text(
    "update ssc.cell_resource set execution = :exec "
    "where org_id = :org and resource = :res and state = 'creating' and execution is null"
)
_RETRY = text(
    "update ssc.cell_resource set execution = null, last_error = :error "
    "where org_id = :org and resource = :res and state = 'creating' and execution = :exec"
)
_READY = text(
    "update ssc.cell_resource set state = 'ready', ready_at = now(), last_error = null "
    "where org_id = :org and resource = :res and state = 'creating'"
)
_FAILED = text(
    "update ssc.cell_resource set state = 'failed', failure_code = :code, failed_at = now(), "
    "started_at = coalesce(started_at, now()), last_error = :error "
    "where org_id = :org and resource = :res and state in ('requested', 'creating')"
)
_WAITERS = text(
    "select w.deployment_id, d.environment_id, d.state from ssc.cell_resource_waiter w "
    "join ssc.deployment d on d.org_id = w.org_id and d.id = w.deployment_id "
    "where w.org_id = :org and w.resource = :res order by w.created_at, w.deployment_id"
)
_FORGET = text(
    "delete from ssc.cell_resource_waiter "
    "where org_id = :org and resource = :res and deployment_id = :dep"
)


async def create_step(ports: Ports, *, org_id: str, resource: CellResource) -> str:
    """One step; the resource's state after it (or ``missing``)."""
    async with bound_org(ports.engine, org_id) as conn:
        row = await lock(conn, org_id, resource)
        if row is None:
            return "missing"
        if row.state in (CellResourceState.READY, CellResourceState.FAILED):
            await wake(conn, org_id, resource)
            return row.state.value
        if ports.cell_deployer is None:
            await _finish(conn, org_id, row, CELL_DEPLOYER_UNAVAILABLE, "no cell deployer")
            return CellResourceState.FAILED.value
        execution = row.execution
        label = str((await conn.execute(_LABEL, {"org": org_id})).scalar_one())
        if execution is None:
            await conn.execute(_ATTEMPT, {"org": org_id, "res": resource.value})
    if execution is None:
        return await _start(ports, org_id, resource, label)
    return await _poll(ports, org_id, resource, execution)


async def _start(ports: Ports, org_id: str, resource: CellResource, label: str) -> str:
    deployer = ports.cell_deployer
    if deployer is None:
        raise AssertionError("checked by create_step")
    params = {"org": org_id, "res": resource.value}
    try:
        execution = await deployer.start(label, resource)
    except CellDeployerError as exc:
        log.warning("cell deployer did not start", extra={"org_id": org_id, "error": str(exc)})
        return await _attempt_failed(ports, org_id, resource, None, str(exc))
    async with bound_org(ports.engine, org_id) as conn:
        await conn.execute(_RECORD, {**params, "exec": execution})
        await _later(ports, conn, org_id, resource, POLL_SECONDS)
    return CellResourceState.CREATING.value


async def _poll(ports: Ports, org_id: str, resource: CellResource, execution: str) -> str:
    deployer = ports.cell_deployer
    if deployer is None:
        raise AssertionError("checked by create_step")
    try:
        status = await deployer.status(execution)
    except CellDeployerError as exc:
        log.warning("cell deployer status unknown", extra={"org_id": org_id, "error": str(exc)})
        status = "running"
    if status == "failed":
        return await _attempt_failed(ports, org_id, resource, execution, "the deployer run failed")
    async with bound_org(ports.engine, org_id) as conn:
        row = await lock(conn, org_id, resource)
        if row is None or row.state is not CellResourceState.CREATING:
            return "missing" if row is None else row.state.value
        if status == "running" or row.execution != execution:
            await _later(ports, conn, org_id, resource, POLL_SECONDS)
            return row.state.value
        await conn.execute(_READY, {"org": org_id, "res": resource.value})
        await wake(conn, org_id, resource)
        await _audit(conn, org_id, row, AuditAction.CELL_RESOURCE_READY, "ready", None)
    return CellResourceState.READY.value


async def _attempt_failed(
    ports: Ports, org_id: str, resource: CellResource, execution: str | None, error: str
) -> str:
    """A run that failed or did not start: another after a back-off, or ``failed``."""
    async with bound_org(ports.engine, org_id) as conn:
        row = await lock(conn, org_id, resource)
        if row is None or row.state is not CellResourceState.CREATING:
            return "missing" if row is None else row.state.value
        if row.execution != execution:
            return row.state.value
        if row.attempts >= MAX_ATTEMPTS:
            await _finish(conn, org_id, row, CELL_DEPLOYER_FAILED, error)
            return CellResourceState.FAILED.value
        params = {"org": org_id, "res": resource.value, "exec": execution, "error": error[:200]}
        if execution is not None:
            await conn.execute(_RETRY, params)
        delay = BACKOFF_SECONDS[min(row.attempts, len(BACKOFF_SECONDS)) - 1]
        await _later(ports, conn, org_id, resource, delay)
    return CellResourceState.CREATING.value


async def _finish(
    conn: AsyncConnection, org_id: str, row: CellResourceRow, code: str, error: str
) -> None:
    params = {"org": org_id, "res": row.resource.value, "code": code, "error": error[:200]}
    await conn.execute(_FAILED, params)
    await wake(conn, org_id, row.resource)
    await _audit(conn, org_id, row, AuditAction.CELL_RESOURCE_FAILED, "failed", code)


async def _later(
    ports: Ports, conn: AsyncConnection, org_id: str, resource: CellResource, seconds: float
) -> None:
    at = ports.clock() + timedelta(seconds=seconds)
    await defer_create(conn, org_id=org_id, resource=resource, schedule_at=at)


async def wake(conn: AsyncConnection, org_id: str, resource: CellResource) -> list[str]:
    """Re-defer every deployment waiting for ``resource``; forget waiters no longer in flight.
    The deployment ids re-deferred."""
    woken: list[str] = []
    for dep_id, env_id, state in (
        await conn.execute(_WAITERS, {"org": org_id, "res": resource.value})
    ).all():
        if state in ("pending", "running"):
            await defer_deployment(
                conn, org_id=org_id, environment_id=str(env_id), deployment_id=str(dep_id)
            )
            woken.append(str(dep_id))
        else:
            await conn.execute(_FORGET, {"org": org_id, "res": resource.value, "dep": dep_id})
    return woken


async def _audit(  # noqa: PLR0913, PLR0917
    conn: AsyncConnection,
    org_id: str,
    row: CellResourceRow,
    action: AuditAction,
    state: str,
    code: str | None,
) -> None:
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=action,
            actor=row.actor,
            target_kind=TARGET_KIND,
            target_id=row.resource.value,
            before={"state": row.state.value, "attempts": row.attempts},
            after={
                "state": state,
                "attempts": row.attempts,
                "execution": row.execution,
                "failure_code": code,
            },
        ),
    )
