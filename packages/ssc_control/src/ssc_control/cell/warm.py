"""The warm option (SSC-092): the production environments an org keeps at one instance, and
whether its cell's gateway is kept at one instance too.

``set_warm`` is the org admin's change, in the caller's org-bound transaction. It names the
environments (``ssc.environment.warm``), which the runtime driver reads on its next pass
(``desired_for``), and the gateway's wish (``ssc.warm_gateway.wanted``). It refuses a preview
environment, an environment the org does not have, and a cost that is not what the setting
costs, so the audit row (``org.updated`` on ``warm``) records the cost the person was shown.
Setting what is already set changes nothing and audits nothing.

The gateway's part is the cell stack's ``warm`` flag, which only the cell deployer sets. When
``wanted`` differs from what the last run applied, ``gateway_step`` (the ``cell:warm_gateway``
job) brings them together, one step per job like ``create.create_step``:

1. no run in flight: count an attempt, commit, start a deployer run with ``warm=true`` or
   ``warm=false``, record its execution and what it sets.
2. a run in flight: read it. Running: look again in ``POLL_SECONDS``. Failed: after
   ``MAX_ATTEMPTS`` attempts ``CELL_DEPLOYER_FAILED``, before that another run after a back-off.
   Succeeded: ``applied`` is what it set; if the admin changed ``wanted`` meanwhile, another run.

With no deployer configured it fails at once with ``CELL_DEPLOYER_UNAVAILABLE``. Changing
``wanted`` clears a failure and starts again. Charging for any of this is not built (A6).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import AuditAction
from ssc_contracts.cells import WarmGateway, warm_monthly_usd
from ssc_contracts.errors import ErrorCode
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.cell.create import (
    BACKOFF_SECONDS,
    CELL_DEPLOYER_FAILED,
    CELL_DEPLOYER_UNAVAILABLE,
    MAX_ATTEMPTS,
    POLL_SECONDS,
)
from ssc_control.cell.deployer import CellDeployerError
from ssc_control.cell.tasks import defer_warm_gateway
from ssc_control.db.bind import bound_org
from ssc_control.worker_ports import Ports

log = logging.getLogger(__name__)

TARGET_KIND: Final = "warm"
MAX_ENVIRONMENTS: Final = 200
"""The most environments one change names."""

type GatewayState = Literal["off", "on", "turning_on", "turning_off", "failed"]

_LABEL = text("select cell_label from ssc.org where id = :org")
_ENSURE = text(
    "insert into ssc.warm_gateway (org_id, wanted) values (:org, false) "
    "on conflict (org_id) do nothing"
)
_LOCK = text("select * from ssc.warm_gateway where org_id = :org for update")
_READ = text("select * from ssc.warm_gateway where org_id = :org")
_WARM_ENVS = text(
    'select id from ssc.environment where org_id = :org and warm order by id collate "C"'
)
_NAMED = text(
    "select id, name from ssc.environment where org_id = :org and id = any(cast(:ids as text[])) "
    "for no key update"
)
_SET_ENVS = text(
    "update ssc.environment set warm = (id = any(cast(:ids as text[]))) "
    "where org_id = :org and (warm or id = any(cast(:ids as text[])))"
)
_WANT = text(
    "update ssc.warm_gateway set wanted = :wanted, attempts = 0, failure_code = null, "
    "last_error = null, updated_at = now() where org_id = :org"
)
_ATTEMPT = text(
    "update ssc.warm_gateway set attempts = attempts + 1, updated_at = now() "
    "where org_id = :org and execution is null"
)
_RECORD = text(
    "update ssc.warm_gateway set execution = :exec, execution_wants = :wants, updated_at = now() "
    "where org_id = :org and execution is null"
)
_APPLIED = text(
    "update ssc.warm_gateway set applied = execution_wants, applied_at = now(), execution = null, "
    "execution_wants = null, last_error = null, updated_at = now() "
    "where org_id = :org and execution = :exec returning wanted, applied"
)
_AGAIN = text("update ssc.warm_gateway set attempts = 0, updated_at = now() where org_id = :org")
_RETRY = text(
    "update ssc.warm_gateway set execution = null, execution_wants = null, last_error = :error, "
    "updated_at = now() where org_id = :org and execution is not distinct from cast(:exec as text)"
)
_FAILED = text(
    "update ssc.warm_gateway set execution = null, execution_wants = null, failure_code = :code, "
    "last_error = :error, updated_at = now() where org_id = :org"
)


class WarmError(ValueError):
    """A change ``set_warm`` refuses: ``code`` for the API's refusal, ``evidence`` says why."""

    def __init__(self, code: ErrorCode, evidence: dict[str, object]) -> None:
        super().__init__(code.value)
        self.code = code
        self.evidence = evidence


@dataclass(frozen=True, slots=True, kw_only=True)
class GatewayRow:
    wanted: bool
    applied: bool
    execution: str | None
    execution_wants: bool | None
    attempts: int
    failure_code: str | None
    updated_at: datetime | None
    applied_at: datetime | None

    @property
    def state(self) -> GatewayState:
        """``on`` or ``off`` once a run applied what is wanted; ``failed`` when the runs ran
        out; ``turning_on`` or ``turning_off`` while they go on."""
        if self.execution is None and self.wanted == self.applied:
            return "on" if self.applied else "off"
        if self.execution is None and self.failure_code is not None:
            return "failed"
        return "turning_on" if self.wanted else "turning_off"


OFF: Final = GatewayRow(
    wanted=False,
    applied=False,
    execution=None,
    execution_wants=None,
    attempts=0,
    failure_code=None,
    updated_at=None,
    applied_at=None,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class WarmSetting:
    environment_ids: tuple[str, ...]
    gateway: GatewayRow

    @property
    def monthly_usd(self) -> int:
        return warm_monthly_usd(len(self.environment_ids), gateway=self.gateway.wanted)


def _row(row: Any) -> GatewayRow:
    return GatewayRow(
        wanted=bool(row.wanted),
        applied=bool(row.applied),
        execution=row.execution,
        execution_wants=row.execution_wants,
        attempts=int(row.attempts),
        failure_code=row.failure_code,
        updated_at=row.updated_at,
        applied_at=row.applied_at,
    )


async def read(conn: AsyncConnection, org_id: str) -> WarmSetting:
    """The org's warm option now; everything off when no admin has set it."""
    found = (await conn.execute(_READ, {"org": org_id})).first()
    envs = (await conn.execute(_WARM_ENVS, {"org": org_id})).scalars().all()
    return WarmSetting(
        environment_ids=tuple(str(e) for e in envs),
        gateway=OFF if found is None else _row(found),
    )


async def set_warm(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    environment_ids: Sequence[str],
    gateway: bool,
    monthly_usd_shown: int,
    actor: Actor,
) -> WarmSetting:
    """Name the warm environments and the gateway's wish; the setting after. ``WarmError`` with
    ``REFERENCE_NOT_FOUND`` for an environment the org does not have, ``VALIDATION_FAILED`` for
    a preview environment or a ``monthly_usd_shown`` that is not what the setting costs."""
    params = {"org": org_id}
    await conn.execute(_ENSURE, params)
    gw = _row((await conn.execute(_LOCK, params)).one())
    wanted = sorted(set(environment_ids))
    rows = (await conn.execute(_NAMED, {**params, "ids": wanted})).all()
    named = {str(env_id): str(name) for env_id, name in rows}
    for env_id in wanted:
        if env_id not in named:
            raise WarmError(ErrorCode.REFERENCE_NOT_FOUND, {"environment_id": env_id})
        if named[env_id] != "prod":
            raise WarmError(
                ErrorCode.VALIDATION_FAILED,
                {"environment_id": env_id, "rule": "preview environments are never warm"},
            )
    cost = warm_monthly_usd(len(wanted), gateway=gateway)
    if monthly_usd_shown != cost:
        shown: dict[str, object] = {"monthly_usd_shown": monthly_usd_shown, "monthly_usd": cost}
        raise WarmError(ErrorCode.VALIDATION_FAILED, shown)
    before = await read(conn, org_id)
    if list(before.environment_ids) == wanted and gw.wanted == gateway:
        return before
    await conn.execute(_SET_ENVS, {**params, "ids": wanted})
    if gw.wanted != gateway:
        await conn.execute(_WANT, {**params, "wanted": gateway})
        await defer_warm_gateway(conn, org_id=org_id)
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.ORG_UPDATED,
            actor=actor,
            target_kind=TARGET_KIND,
            target_id=org_id,
            before={
                "environment_ids": list(before.environment_ids),
                "gateway": before.gateway.wanted,
            },
            after={"environment_ids": wanted, "gateway": gateway, "monthly_usd_shown": cost},
        ),
    )
    return await read(conn, org_id)


async def gateway_step(ports: Ports, *, org_id: str) -> str:
    """One step; the gateway's state after it (or ``missing``)."""
    params = {"org": org_id}
    async with bound_org(ports.engine, org_id) as conn:
        found = (await conn.execute(_LOCK, params)).first()
        if found is None:
            return "missing"
        row = _row(found)
        idle = row.execution is None
        if idle and (row.wanted == row.applied or row.failure_code is not None):
            return row.state
        if ports.cell_deployer is None:
            await _fail(conn, org_id, CELL_DEPLOYER_UNAVAILABLE, "no cell deployer")
            return "failed"
        label = str((await conn.execute(_LABEL, params)).scalar_one())
        if idle:
            await conn.execute(_ATTEMPT, params)
    if row.execution is None:
        return await _start(ports, org_id, label, wants=row.wanted)
    return await _poll(ports, org_id, row.execution)


async def _start(ports: Ports, org_id: str, label: str, *, wants: bool) -> str:
    deployer = ports.cell_deployer
    if deployer is None:
        raise AssertionError("checked by gateway_step")
    flag = WarmGateway.ON if wants else WarmGateway.OFF
    try:
        execution = await deployer.start(label, flag)
    except CellDeployerError as exc:
        log.warning("cell deployer did not start", extra={"org_id": org_id, "error": str(exc)})
        return await _attempt_failed(ports, org_id, None, str(exc))
    async with bound_org(ports.engine, org_id) as conn:
        await conn.execute(_RECORD, {"org": org_id, "exec": execution, "wants": wants})
        await _later(ports, conn, org_id, POLL_SECONDS)
    return "turning_on" if wants else "turning_off"


async def _poll(ports: Ports, org_id: str, execution: str) -> str:
    deployer = ports.cell_deployer
    if deployer is None:
        raise AssertionError("checked by gateway_step")
    try:
        status = await deployer.status(execution)
    except CellDeployerError as exc:
        log.warning("cell deployer status unknown", extra={"org_id": org_id, "error": str(exc)})
        status = "running"
    if status == "failed":
        return await _attempt_failed(ports, org_id, execution, "the deployer run failed")
    params = {"org": org_id, "exec": execution}
    async with bound_org(ports.engine, org_id) as conn:
        if status == "running":
            found = (await conn.execute(_LOCK, params)).first()
            if found is None:
                return "missing"
            await _later(ports, conn, org_id, POLL_SECONDS)
            return _row(found).state
        done = (await conn.execute(_APPLIED, params)).first()
        if done is None:
            return "missing"
        wanted, applied = bool(done.wanted), bool(done.applied)
        if wanted != applied:
            await conn.execute(_AGAIN, params)
            await defer_warm_gateway(conn, org_id=org_id)
            return "turning_on" if wanted else "turning_off"
    return "on" if applied else "off"


async def _attempt_failed(ports: Ports, org_id: str, execution: str | None, error: str) -> str:
    """A run that failed or did not start: another after a back-off, or ``failed``."""
    async with bound_org(ports.engine, org_id) as conn:
        found = (await conn.execute(_LOCK, {"org": org_id})).first()
        if found is None:
            return "missing"
        row = _row(found)
        if row.execution != execution:
            return row.state
        if row.attempts >= MAX_ATTEMPTS:
            await _fail(conn, org_id, CELL_DEPLOYER_FAILED, error)
            return "failed"
        await conn.execute(_RETRY, {"org": org_id, "exec": execution, "error": error[:200]})
        delay = BACKOFF_SECONDS[min(row.attempts, len(BACKOFF_SECONDS)) - 1]
        await _later(ports, conn, org_id, delay)
    return "turning_on" if row.wanted else "turning_off"


async def _fail(conn: AsyncConnection, org_id: str, code: str, error: str) -> None:
    await conn.execute(_FAILED, {"org": org_id, "code": code, "error": error[:200]})
    log.warning("gateway warm flag not set", extra={"org_id": org_id, "failure_code": code})


async def _later(ports: Ports, conn: AsyncConnection, org_id: str, seconds: float) -> None:
    at = ports.clock() + timedelta(seconds=seconds)
    await defer_warm_gateway(conn, org_id=org_id, schedule_at=at)
