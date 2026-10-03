"""One deployment, from ``pending`` to ``healthy`` or ``failed`` (SSC-016, decision 014).

``run_deployment`` is the whole job. It holds the environment's lock (``env:<id>``), so no other
job calls the runtime for that environment meanwhile, and it is safe to run twice: each state
change is a compare-and-set on the deployment row, and ``apply`` is idempotent on the spec.

1. Claim: ``pending`` to ``running`` (a rerun resumes a ``running`` row; anything else stops).
   In the same transaction: the app must be active, the release must have a manifest, and a
   ``prod`` deployment must clear the production gate. Any refusal fails the deployment with a
   reason code and commits, keeping the approval requests the gate opened. The driver is never
   called before the gate clears. A manifest that needs a lazy cell resource not yet ready
   (SSC-087) leaves the deployment ``running`` and waiting; the resource's job re-defers it.
2. ``apply`` the release's spec, then poll ``observe`` until the new revision is ready, fails, or
   the health timeout passes. A deployment another one pre-empted (``superseded``) stops at the
   next poll without touching traffic. So does one whose app stopped (the kill switch): it
   fails with ``APP_NOT_ACTIVE`` and frees ``env:<id>`` within one poll.
3. Healthy: one transaction checks the app is still active (``FOR SHARE``, so it waits for a
   kill switch pulled at the same moment; ``APP_NOT_ACTIVE`` otherwise), marks it ``healthy``
   (only if still ``running``), supersedes the previous live deployment, moves the
   environment's pointer, records ``first_url`` for the environment's first live deployment,
   for a forward deploy only syncs the manifest's schedules, and audits ``*.finished``. Then
   traffic moves; if that call fails the reconciler finishes it. Unhealthy or stopped:
   ``failed`` with the reason code and ``*.failed``; the pointer and traffic are untouched, and
   a service that never had a live deployment is scaled to zero.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.cell.resources import hold_deployment
from ssc_control.db.bind import bound_org
from ssc_control.ports import MetricKind
from ssc_control.runtime.driver import (
    EnvironmentRow,
    ReleaseRow,
    RuntimeDriver,
    ServiceObservation,
    ServiceSpec,
    desired_for,
)
from ssc_control.runtime.specs import ReleaseSpec, ReleaseSpecUnavailableError
from ssc_control.worker_ports import Ports

log = logging.getLogger(__name__)

HEALTH_TIMEOUT_SECONDS: Final = 180.0
HEALTH_POLL_SECONDS: Final = 1.0

APPROVAL_REQUIRED: Final = "APPROVAL_REQUIRED"
APP_NOT_ACTIVE: Final = "APP_NOT_ACTIVE"
HEALTH_CHECK_FAILED: Final = "HEALTH_CHECK_FAILED"
RELEASE_SPEC_UNAVAILABLE: Final = "RELEASE_SPEC_UNAVAILABLE"
RUNTIME_UNAVAILABLE: Final = "RUNTIME_UNAVAILABLE"
RUNTIME_ERROR: Final = "RUNTIME_ERROR"

Kind = Literal["deploy", "rollback"]
type _Verdict = Literal["ready", "unhealthy", "stopped", "preempted"]
_FINISHED: Final = {
    "deploy": AuditAction.DEPLOY_FINISHED,
    "rollback": AuditAction.ROLLBACK_FINISHED,
}
_FAILED: Final = {"deploy": AuditAction.DEPLOY_FAILED, "rollback": AuditAction.ROLLBACK_FAILED}
_VERDICT_CODE: Final = {"unhealthy": HEALTH_CHECK_FAILED, "stopped": APP_NOT_ACTIVE}

_CLAIM = text(
    "update ssc.deployment set state = 'running' "
    "where org_id = :org and id = :id and state = 'pending'"
)
_LOAD = text(
    "select d.state, d.kind, d.app_id, d.environment_id, d.release_id, d.actor_kind, "
    "d.actor_id, d.actor_via_agent, d.actor_client_id, e.name as env_name, "
    "a.status as app_status, a.owner_user_id, r.image_digest "
    "from ssc.deployment d "
    "join ssc.environment e on e.org_id = d.org_id and e.id = d.environment_id "
    "join ssc.app a on a.org_id = d.org_id and a.id = d.app_id "
    "join ssc.release r on r.org_id = d.org_id and r.app_id = d.app_id and r.id = d.release_id "
    "where d.org_id = :org and d.id = :id for update of d"
)
_STATE = text("select state from ssc.deployment where org_id = :org and id = :id")
_POLL = text(
    "select d.state, a.status as app_status from ssc.deployment d "
    "join ssc.app a on a.org_id = d.org_id and a.id = d.app_id "
    "where d.org_id = :org and d.id = :id"
)
_SHARE_APP_STATUS = text("select status from ssc.app where org_id = :org and id = :app for share")
_FINISH = text(
    "update ssc.deployment set state = :state, failure_code = :code, finished_at = now() "
    "where org_id = :org and id = :id and state = 'running'"
)
_LOCK_POINTER = text(
    "select current_deployment_id from ssc.environment where org_id = :org and id = :env for update"
)
_SUPERSEDE_LIVE = text(
    "update ssc.deployment set state = 'superseded' "
    "where org_id = :org and id = :id and state = 'healthy'"
)
_MOVE_POINTER = text(
    "update ssc.environment set current_deployment_id = :id where org_id = :org and id = :env"
)

type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Deployment:
    id: str
    kind: Kind
    app_id: str
    environment_id: str
    release_id: str
    actor: Actor
    env_name: str
    owner_user_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _Ready:
    deployment: _Deployment
    desired: ServiceSpec
    spec: ReleaseSpec
    driver: RuntimeDriver


def _deployment(deployment_id: str, row: Any) -> _Deployment:
    return _Deployment(
        id=deployment_id,
        kind="rollback" if row.kind == "rollback" else "deploy",
        app_id=str(row.app_id),
        environment_id=str(row.environment_id),
        release_id=str(row.release_id),
        actor=Actor(
            ActorKind(str(row.actor_kind)),
            str(row.actor_id),
            via_agent=bool(row.actor_via_agent),
            client_id=None if row.actor_client_id is None else str(row.actor_client_id),
        ),
        env_name=str(row.env_name),
        owner_user_id=str(row.owner_user_id),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthWait:
    """How long a new revision has to become ready, how often to look, and how to wait."""

    within: float = HEALTH_TIMEOUT_SECONDS
    every: float = HEALTH_POLL_SECONDS
    sleep: Sleep = asyncio.sleep


@dataclass(frozen=True, slots=True)
class _Refused:
    code: str
    policy_decision_id: str | None = None


async def run_deployment(
    ports: Ports, *, org_id: str, deployment_id: str, health: HealthWait | None = None
) -> str:
    """The deployment job; returns the deployment's state when it stops (or ``missing``)."""
    ready = await _claim(ports, org_id, deployment_id)
    if isinstance(ready, str):
        return ready
    dep, driver, desired = ready.deployment, ready.driver, ready.desired
    try:
        revision = await driver.apply(desired)
        verdict = await _wait_healthy(ports, org_id, ready, revision, health or HealthWait())
    except Exception:
        log.exception("runtime call failed", extra={"deployment_id": dep.id})
        return await _fail_after_runtime(ports, org_id, ready, RUNTIME_ERROR)
    if verdict == "preempted":
        return "superseded"
    if verdict != "ready":
        return await _fail_after_runtime(ports, org_id, ready, _VERDICT_CODE[verdict])
    state = await _go_live(ports, org_id, ready)
    if state != "healthy":
        return state
    try:
        await driver.set_traffic(desired.service, revision)
    except Exception:
        log.exception("set_traffic failed; the reconciler repairs it", extra={"id": dep.id})
    return "healthy"


async def _claim(ports: Ports, org_id: str, deployment_id: str) -> _Ready | str:
    """Step 1 in one transaction: the ready deployment, or the state it stopped in."""
    async with bound_org(ports.engine, org_id) as conn:
        params = {"org": org_id, "id": deployment_id}
        row = (await conn.execute(_LOAD, params)).first()
        if row is None:
            log.warning("deployment not found", extra={"deployment_id": deployment_id})
            return "missing"
        if row.state not in ("pending", "running"):
            return str(row.state)
        await conn.execute(_CLAIM, params)
        dep = _deployment(deployment_id, row)
        prepared = await _prepare(conn, ports, org_id, dep, row)
        if isinstance(prepared, _Refused):
            return await _fail(
                conn, org_id, dep, prepared.code, policy_decision_id=prepared.policy_decision_id
            )
    return prepared


async def _prepare(
    conn: AsyncConnection, ports: Ports, org_id: str, dep: _Deployment, row: Any
) -> _Ready | _Refused | Literal["running"]:
    """The checks before any runtime call: an active app, a manifest, the production gate for
    ``prod``, a driver, the cell resources the manifest needs."""
    if row.app_status != "active":
        return _Refused(APP_NOT_ACTIVE)
    try:
        spec = await ports.release_specs.get(
            conn, org_id=org_id, app_id=dep.app_id, release_id=dep.release_id
        )
    except ReleaseSpecUnavailableError:
        return _Refused(RELEASE_SPEC_UNAVAILABLE)
    decision: str | None = None
    if dep.env_name == "prod":
        gate = await ports.prod_gate.check(
            conn,
            org_id=org_id,
            app_id=dep.app_id,
            environment_id=dep.environment_id,
            release_id=dep.release_id,
        )
        if gate.outcome != "clear":
            return _Refused(APPROVAL_REQUIRED, gate.policy_decision_id)
        decision = gate.policy_decision_id
    if ports.runtime_driver is None:
        return _Refused(RUNTIME_UNAVAILABLE, decision)
    held = await hold_deployment(
        conn, org_id=org_id, deployment_id=dep.id, manifest=spec.manifest, actor=dep.actor
    )
    if held is not None:
        return "running" if held.failure_code is None else _Refused(held.failure_code, decision)
    desired = desired_for(
        env=EnvironmentRow(
            id=dep.environment_id, org_id=org_id, app_id=dep.app_id, name=row.env_name
        ),
        release=ReleaseRow(id=dep.release_id, image_digest=str(row.image_digest)),
        manifest=spec.manifest,
        app_status="active",
        framework=spec.framework,
    )
    if not isinstance(desired, ServiceSpec):
        raise AssertionError("an active app has a service spec")
    return _Ready(deployment=dep, desired=desired, spec=spec, driver=ports.runtime_driver)


async def _wait_healthy(
    ports: Ports, org_id: str, ready: _Ready, revision: str, health: HealthWait
) -> _Verdict:
    """``ready`` when ``revision`` is; ``unhealthy`` when it failed, vanished or timed out;
    ``preempted`` when the deployment stopped being ``running``; ``stopped`` when the app did.
    Both are read before each ``observe``."""
    deadline = time.monotonic() + health.within
    params = {"org": org_id, "id": ready.deployment.id}
    while True:
        async with bound_org(ports.engine, org_id) as conn:
            row = (await conn.execute(_POLL, params)).first()
        if row is None or row.state != "running":
            return "preempted"
        if row.app_status != "active":
            return "stopped"
        verdict = _health(await ready.driver.observe(ready.desired.service), revision)
        if verdict is not None:
            return "ready" if verdict else "unhealthy"
        if time.monotonic() >= deadline:
            return "unhealthy"
        await health.sleep(health.every)


def _health(observed: ServiceObservation | None, revision: str) -> bool | None:
    """Ready: True. Failed or gone: False. Still starting: None."""
    if observed is None:
        return False
    match = next((r for r in observed.revisions if r.revision == revision), None)
    if match is None or match.failed:
        return False
    return True if match.ready is True else None


async def _go_live(ports: Ports, org_id: str, ready: _Ready) -> str:
    """Step 3 in one transaction. The state after it: ``healthy``, ``superseded`` when the
    deployment was pre-empted first, or ``failed`` (``APP_NOT_ACTIVE``) when the app stopped."""
    dep = ready.deployment
    async with bound_org(ports.engine, org_id) as conn:
        app = {"org": org_id, "app": dep.app_id}
        if (await conn.execute(_SHARE_APP_STATUS, app)).scalar() == "active":
            return "healthy" if await _mark_healthy(conn, ports, org_id, ready) else "superseded"
        state, never_live = await _record_failure(conn, org_id, dep, APP_NOT_ACTIVE)
    return await _scale_down_if_never_live(ready, state, never_live=never_live)


async def _mark_healthy(conn: AsyncConnection, ports: Ports, org_id: str, ready: _Ready) -> bool:
    """Step 3's writes; False when the deployment is no longer ``running``."""
    dep = ready.deployment
    params = {"org": org_id, "id": dep.id, "env": dep.environment_id}
    done = await conn.execute(_FINISH, {**params, "state": "healthy", "code": None})
    if done.rowcount == 0:
        return False
    previous = (await conn.execute(_LOCK_POINTER, params)).scalar()
    if previous is not None and previous != dep.id:
        await conn.execute(_SUPERSEDE_LIVE, {"org": org_id, "id": previous})
    await conn.execute(_MOVE_POINTER, params)
    person = dep.actor.id if dep.actor.kind is ActorKind.USER else None
    if previous is None:
        await ports.metrics.record_event(
            conn,
            org_id=org_id,
            kind=MetricKind.FIRST_URL,
            app_id=dep.app_id,
            user_id=person,
            properties={"environment": dep.env_name},
        )
    if dep.kind == "deploy":
        await ports.timers.sync_schedules(
            conn,
            org_id=org_id,
            environment_id=dep.environment_id,
            declared=ready.spec.manifest.schedules,
            declared_by_user_id=person or dep.owner_user_id,
            actor=dep.actor,
        )
    # Last: every row lock above is taken before audit_head (decision 020).
    await _audit(conn, org_id, dep, _FINISHED[dep.kind], state="healthy", code=None)
    return True


async def _fail_after_runtime(ports: Ports, org_id: str, ready: _Ready, code: str) -> str:
    """Record the failure; scale a service that never had a live deployment to zero."""
    async with bound_org(ports.engine, org_id) as conn:
        state, never_live = await _record_failure(conn, org_id, ready.deployment, code)
    return await _scale_down_if_never_live(ready, state, never_live=never_live)


async def _record_failure(
    conn: AsyncConnection, org_id: str, dep: _Deployment, code: str
) -> tuple[str, bool]:
    """Fail the deployment; its state after, and whether its environment has no live one."""
    state = await _fail(conn, org_id, dep, code)
    pointer = (
        await conn.execute(_LOCK_POINTER, {"org": org_id, "env": dep.environment_id})
    ).scalar()
    return state, pointer is None


async def _scale_down_if_never_live(ready: _Ready, state: str, *, never_live: bool) -> str:
    if state == "failed" and never_live:
        try:
            await ready.driver.scale_to_zero(ready.desired.service)
        except Exception:
            log.exception("scale_to_zero failed", extra={"deployment_id": ready.deployment.id})
    return state


async def _fail(
    conn: AsyncConnection,
    org_id: str,
    dep: _Deployment,
    code: str,
    *,
    policy_decision_id: str | None = None,
) -> str:
    params = {"org": org_id, "id": dep.id, "state": "failed", "code": code}
    if (await conn.execute(_FINISH, params)).rowcount == 0:
        state = (await conn.execute(_STATE, params)).scalar()
        return "missing" if state is None else str(state)
    await _audit(
        conn,
        org_id,
        dep,
        _FAILED[dep.kind],
        state="failed",
        code=code,
        policy_decision_id=policy_decision_id,
    )
    log.info("deployment failed", extra={"deployment_id": dep.id, "code": code})
    return "failed"


async def _audit(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    org_id: str,
    dep: _Deployment,
    action: AuditAction,
    *,
    state: str,
    code: str | None,
    policy_decision_id: str | None = None,
) -> None:
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=action,
            actor=dep.actor,
            target_kind="deployment",
            target_id=dep.id,
            before={"state": "running"},
            after={
                "state": state,
                "failure_code": code,
                "release_id": dep.release_id,
                "environment_id": dep.environment_id,
            },
            policy_decision_id=policy_decision_id,
        ),
    )
