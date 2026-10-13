"""The kill switch (SSC-025, C30): stop one app in a fixed order and time each step.

``start`` runs in the API's transaction. It sets the app's status, inserts the run, marks the
access snapshot dirty and defers the job, so the deny is committed before any step runs and holds
whatever happens after it: the reconciler keeps a disabled or quarantined app stopped. ``enable``
puts an app back and resumes, once, the timers its runs paused.

``run`` is the job. It reads where the run is from ``steps`` and is safe to run twice: every
transaction locks the run's row and checks ``steps`` is still what it read. The steps, in order:

1. ``gateway_deny`` asks for the next snapshot version, which carries the new status. It is
   ``done`` once the org's cell has that version (``SnapshotPort.confirmed``: its
   ``latest.json`` names it, which an awake gateway, or one starting from zero, reads before
   it decides) and ``unconfirmed`` after ``confirm_within``. The job polls within itself, every
   ``confirm_every``, holding one worker slot but no transaction while it waits: a job
   scheduled for later waits for the queue's next poll, which is far slower than the deny.
2. ``datagw_suspend`` is the same version: the data gateway (SSC-050) reads it on demand, so it
   refuses each of the app's connections with ``APP_NOT_ACTIVE`` and its kill watch ends the
   queries already running; the version confirms it.
3. ``egress_remove`` is the same version: the egress proxy (SSC-053) polls it and drops the
   credentials of the app's environments, and its drain closes their open tunnels within
   ``ssc_contracts.egress.DRAIN_SECONDS``; the version confirms it.
4. ``scale_to_zero`` stops each environment, prod first, in a job holding that environment's
   lock; ``observe`` must then report it stopped or gone.
5. ``pause_timers`` pauses the app's schedules and keeps their ids for ``enable``.

A step that raises is tried again after a doubling backoff; after ``max_attempts`` tries it is
``failed``, the later steps still run and the run ends ``failed``. Each finished step writes one
``kill_switch.step`` audit row in the transaction that records it, with the step's own time
and the time since the command: the last step's is the drill's.

``sweep`` re-defers a running run whose job is gone (one that raised, say while the database was
down), so no run stays ``running`` for ever and blocks ``enable``.

Removing an app (GA-12) is a run in mode ``delete``. ``start_delete`` marks the app ``deleting``,
which denies it as a disable does, and the run goes through ``DELETE_STEPS``: the first four
steps above, then ``lifecycle.removal``'s, which delete what the app had. Its cell steps work
like ``scale_to_zero``, one environment per job holding that environment's lock. Unlike the
switch, a removal stops at a step that failed and ends ``failed`` there: nothing later is
deleted on top of something that is still in place. Asking again starts a new run, whose steps
find what is already gone and go on; the last one marks the app ``deleted``.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final, Literal, cast

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.db.bind import bound_org
from ssc_control.db.orgs import all_org_ids
from ssc_control.lifecycle import removal
from ssc_control.lifecycle.tasks import defer_kill_switch, queueing_lock
from ssc_control.ports import KillReason, TimersPort
from ssc_control.runtime.cells import CELL_UNAVAILABLE, CellUnavailableError, OrgCell
from ssc_control.runtime.driver import (
    RuntimeDriver,
    RuntimeDriverError,
    ServiceNotFoundError,
    service_name,
)
from ssc_control.snapshot.service import mark_dirty
from ssc_control.worker_ports import Ports

log = logging.getLogger(__name__)

Mode = KillReason
RunMode = Literal["disable", "quarantine", "delete"]
"""A run's mode: the switch's two, or the removal of the app."""
StepName = Literal[
    "gateway_deny", "datagw_suspend", "egress_remove", "scale_to_zero", "pause_timers"
]
StopStepName = Literal["gateway_deny", "datagw_suspend", "egress_remove", "scale_to_zero"]
DeleteStepName = Literal[StopStepName, removal.StepName]
AnyStepName = Literal[StepName, removal.StepName]
StepState = Literal["running", "done", "unconfirmed", "failed"]

STEPS: Final[tuple[StepName, ...]] = (
    "gateway_deny",
    "datagw_suspend",
    "egress_remove",
    "scale_to_zero",
    "pause_timers",
)
STOP_STEPS: Final[tuple[StopStepName, ...]] = (
    "gateway_deny",
    "datagw_suspend",
    "egress_remove",
    "scale_to_zero",
)
DELETE: Final = "delete"
DELETE_STEPS: Final[tuple[DeleteStepName, ...]] = (*STOP_STEPS, *removal.STEPS)
"""A removal's steps: deny and stop as the switch does, then delete."""
ALL_STEPS: Final[tuple[AnyStepName, ...]] = (*STEPS, *removal.STEPS)
STEP_STATES: Final[frozenset[str]] = frozenset({"running", "done", "unconfirmed", "failed"})
MODE_STATUS: Final[Mapping[Mode, str]] = {"disable": "disabled", "quarantine": "quarantined"}


def steps_for(mode: RunMode) -> tuple[AnyStepName, ...]:
    """The steps a run in ``mode`` goes through, in order."""
    return DELETE_STEPS if mode == DELETE else STEPS


RUNNING: Final = "running"
MISSING: Final = "missing"
RUNTIME_UNAVAILABLE: Final = "RUNTIME_UNAVAILABLE"
RUNTIME_ERROR: Final = "RUNTIME_ERROR"
NOT_STOPPED: Final = "NOT_STOPPED"
STEP_ERROR: Final = "STEP_ERROR"


@dataclass(frozen=True, slots=True, kw_only=True)
class Timings:
    confirm_within: timedelta = timedelta(seconds=10)
    confirm_every: timedelta = timedelta(milliseconds=250)
    backoff: timedelta = timedelta(seconds=2)
    """Before the second try; doubled for each try after it."""
    max_attempts: int = 5
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    """Seconds to wait between two checks of the gateway's version; tests replace it."""


TIMINGS: Final = Timings()


class RefusedError(Exception):
    """``start`` or ``enable`` refused; the API answers ``code``."""

    def __init__(self, code: ErrorCode, **evidence: str) -> None:
        super().__init__(code.value)
        self.code = code
        self.evidence: dict[str, str] = evidence


class StepFailedError(Exception):
    """A try that failed with a reason code; ``final`` fails the step without another try."""

    def __init__(self, code: str, *, final: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.final = final


class _LostRaceError(Exception):
    """Another run of the job moved the run on first."""


# ── steps ────────────────────────────────────────────────────────────────────


def _ts(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def span_ms(start: datetime, end: datetime) -> int:
    return max(0, round((end - start) / timedelta(milliseconds=1)))


@dataclass(frozen=True, slots=True, kw_only=True)
class Step:
    """One entry of ``steps``. ``confirm_by`` is the gateway's deadline; ``stopped`` the
    environments ``scale_to_zero``, or a removal's cell step, has done so far; ``removed`` how
    many things a removal's step deleted."""

    name: AnyStepName
    state: StepState
    started_at: datetime
    finished_at: datetime | None = None
    elapsed_ms: int | None = None
    snapshot_version: int | None = None
    attempts: int = 0
    error: str | None = None
    confirm_by: datetime | None = None
    stopped: tuple[str, ...] = ()
    removed: int | None = None

    def to_json(self) -> dict[str, Any]:
        counted = {} if self.removed is None else {"removed": self.removed}
        return {
            "name": self.name,
            "state": self.state,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "elapsed_ms": self.elapsed_ms,
            "snapshot_version": self.snapshot_version,
            "attempts": self.attempts,
            "error": self.error,
            "confirm_by": _iso(self.confirm_by),
            "stopped": list(self.stopped),
            **counted,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Step:
        name, state, started = str(raw["name"]), str(raw["state"]), _ts(raw["started_at"])
        if name not in ALL_STEPS or state not in STEP_STATES or started is None:
            raise ValueError(f"not a kill-switch step: {raw!r}")
        version, elapsed = raw.get("snapshot_version"), raw.get("elapsed_ms")
        removed = raw.get("removed")
        return cls(
            name=name,
            state=cast("StepState", state),
            started_at=started,
            finished_at=_ts(raw.get("finished_at")),
            elapsed_ms=None if elapsed is None else int(elapsed),
            snapshot_version=None if version is None else int(version),
            attempts=int(raw.get("attempts") or 0),
            error=None if raw.get("error") is None else str(raw["error"]),
            confirm_by=_ts(raw.get("confirm_by")),
            stopped=tuple(str(e) for e in cast("list[object]", raw.get("stopped") or [])),
            removed=None if removed is None else int(removed),
        )

    def finish(self, at: datetime, state: StepState, *, tried: bool = True) -> Step:
        """Finished at ``at``; ``tried`` counts this try in ``attempts``."""
        return replace(
            self,
            state=state,
            finished_at=at,
            elapsed_ms=span_ms(self.started_at, at),
            attempts=self.attempts + (1 if tried else 0),
        )


def steps_of(raw: object) -> tuple[Step, ...]:
    """The stored ``steps`` array, parsed."""
    return tuple(Step.from_json(s) for s in cast("list[Mapping[str, Any]]", raw or []))


# ── the run ──────────────────────────────────────────────────────────────────

_SELECT_RUN = text(
    "select id, app_id, mode, state, steps, paused_schedule_ids, actor_kind, actor_id, "
    "actor_via_agent, actor_client_id, started_at from ssc.kill_switch_run "
    "where org_id = :org and id = :id"
)
_SELECT_RUNNING = text(
    "select id from ssc.kill_switch_run where org_id = :org and app_id = :app and state = 'running'"
)
_SELECT_ENVS = text(
    "select id from ssc.environment where org_id = :org and app_id = :app "
    "order by name = 'prod' desc, id"
)
_LOCK_RUN = text(
    "select state, steps from ssc.kill_switch_run where org_id = :org and id = :id for update"
)
_WRITE = text(
    "update ssc.kill_switch_run set steps = cast(:steps as jsonb), "
    "paused_schedule_ids = cast(:paused as jsonb) where org_id = :org and id = :id"
)
_FINISH_RUN = text(
    "update ssc.kill_switch_run set state = :state, finished_at = :at "
    "where org_id = :org and id = :id and state = 'running'"
)
_NOW = text("select clock_timestamp()")


@dataclass(frozen=True, slots=True, kw_only=True)
class _Run:
    id: str
    app_id: str
    mode: RunMode
    state: str
    raw_steps: object
    """``steps`` as read, for the compare-and-set."""
    steps: tuple[Step, ...]
    paused: tuple[str, ...]
    actor: Actor
    env_ids: tuple[str, ...]
    started_at: datetime

    @property
    def current(self) -> Step | None:
        """The step under way, if any."""
        return self.steps[-1] if self.steps and self.steps[-1].state == RUNNING else None

    @property
    def version(self) -> int | None:
        return next((s.snapshot_version for s in self.steps if s.name == "gateway_deny"), None)


async def _load(conn: AsyncConnection, org_id: str, run_id: str) -> _Run | None:
    row = (await conn.execute(_SELECT_RUN, {"org": org_id, "id": run_id})).first()
    if row is None:
        return None
    envs = await conn.execute(_SELECT_ENVS, {"org": org_id, "app": row.app_id})
    return _Run(
        id=str(row.id),
        app_id=str(row.app_id),
        mode=cast("RunMode", str(row.mode)),
        state=str(row.state),
        raw_steps=row.steps,
        steps=steps_of(row.steps),
        paused=tuple(str(i) for i in cast("list[object]", row.paused_schedule_ids)),
        actor=Actor(
            ActorKind(str(row.actor_kind)),
            str(row.actor_id),
            via_agent=bool(row.actor_via_agent),
            client_id=None if row.actor_client_id is None else str(row.actor_client_id),
        ),
        env_ids=tuple(str(e) for e in envs.scalars()),
        started_at=cast("datetime", row.started_at),
    )


async def _now(conn: AsyncConnection) -> datetime:
    return cast("datetime", (await conn.execute(_NOW)).scalar_one())


async def _lock(conn: AsyncConnection, org_id: str, run: _Run) -> None:
    """Lock the run's row; ``_LostRaceError`` unless it is still as ``run`` read it."""
    row = (await conn.execute(_LOCK_RUN, {"org": org_id, "id": run.id})).first()
    if row is None or row.state != RUNNING or row.steps != run.raw_steps:
        raise _LostRaceError(run.id)


async def _write(
    conn: AsyncConnection,
    org_id: str,
    run: _Run,
    steps: Sequence[Step],
    paused: Sequence[str] | None = None,
) -> None:
    await conn.execute(
        _WRITE,
        {
            "org": org_id,
            "id": run.id,
            "steps": json.dumps([s.to_json() for s in steps]),
            "paused": json.dumps(list(run.paused if paused is None else paused)),
        },
    )


async def _record(
    conn: AsyncConnection,
    org_id: str,
    run: _Run,
    step: Step,
    paused: Sequence[str] | None = None,
) -> str | None:
    """Write the finished ``step`` in place of the running one and audit it. After the last
    step, or a removal's failed one, the run finishes too: its state is returned; otherwise
    None, to go on."""
    steps = (*run.steps[:-1], step)
    await _write(conn, org_id, run, steps, paused)
    after: dict[str, Any] = {
        "app_id": run.app_id,
        "mode": run.mode,
        "step": step.name,
        "state": step.state,
        "snapshot_version": step.snapshot_version,
        "elapsed_ms": step.elapsed_ms,
        "since_command_ms": None
        if step.finished_at is None
        else span_ms(run.started_at, step.finished_at),
        "attempts": step.attempts,
    }
    if step.error is not None:
        after["error"] = step.error
    if step.removed is not None:
        after["removed"] = step.removed
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=AuditAction.KILL_SWITCH_STEP,
            actor=run.actor,
            target_kind="kill_switch_run",
            target_id=run.id,
            after=after,
        ),
    )
    stops_here = run.mode == DELETE and step.state == "failed"
    if step.name != steps_for(run.mode)[-1] and not stops_here:
        return None
    return await _finish_run(conn, org_id, run, steps, step.finished_at)


async def _finish_run(
    conn: AsyncConnection, org_id: str, run: _Run, steps: Sequence[Step], at: datetime | None
) -> str:
    state = "failed" if any(s.state == "failed" for s in steps) else "completed"
    at = at or await _now(conn)
    await conn.execute(_FINISH_RUN, {"org": org_id, "id": run.id, "state": state, "at": at})
    log.info("kill switch finished", extra={"run_id": run.id, "state": state})
    return state


@dataclass(frozen=True, slots=True, kw_only=True)
class _Job:
    """One job's context: ``held_env`` is the environment whose lock it holds."""

    ports: Ports
    org_id: str
    held_env: str | None
    timings: Timings

    def tx(self) -> AbstractAsyncContextManager[AsyncConnection]:
        return bound_org(self.ports.engine, self.org_id)

    async def defer(self, run: _Run, *, env_id: str | None) -> None:
        async with self.tx() as conn:
            await _lock(conn, self.org_id, run)
            await defer_kill_switch(
                conn,
                org_id=self.org_id,
                app_id=run.app_id,
                run_id=run.id,
                env_id=env_id,
            )


async def run(
    ports: Ports,
    *,
    org_id: str,
    run_id: str,
    env_id: str | None = None,
    timings: Timings = TIMINGS,
) -> str:
    """Drive the run as far as this job can. ``env_id`` is the environment whose lock the job
    holds. Returns the run's state after (``running`` when a later job goes on) or ``missing``."""
    job = _Job(ports=ports, org_id=org_id, held_env=env_id, timings=timings)
    while True:
        try:
            outcome = await _advance(job, run_id)
        except _LostRaceError:
            log.info("kill-switch run moved on elsewhere", extra={"run_id": run_id})
            return RUNNING
        if outcome is not None:
            return outcome


async def _advance(job: _Job, run_id: str) -> str | None:
    """Begin the next step or work on the current one; None to go on."""
    async with job.tx() as conn:
        run = await _load(conn, job.org_id, run_id)
        if run is None:
            log.warning("kill-switch run not found", extra={"org_id": job.org_id, "run_id": run_id})
            return MISSING
        if run.state != RUNNING:
            # A stale job, left from a finished run, carries on the app's running one if any.
            other = (
                await conn.execute(_SELECT_RUNNING, {"org": job.org_id, "app": run.app_id})
            ).scalar_one_or_none()
            found = None if other is None else await _load(conn, job.org_id, str(other))
            if found is None:
                return run.state
            run = found
        step = run.current
        if step is None:
            return await _begin(job, conn, run)
    try:
        return await _work(job, run, step)
    except _LostRaceError:
        raise
    except Exception as exc:
        code, final = (
            (exc.code, exc.final)
            if isinstance(exc, StepFailedError)
            else (RUNTIME_ERROR if isinstance(exc, RuntimeDriverError) else STEP_ERROR, False)
        )
        log.warning(
            "kill-switch step failed",
            extra={"run_id": run.id, "step": step.name, "code": code},
            exc_info=True,
        )
        return await _failed_try(job, run, step, code, final=final)


async def _begin(job: _Job, conn: AsyncConnection, run: _Run) -> str | None:
    """Write the next step as running. The gateway's asks for its snapshot version here."""
    await _lock(conn, job.org_id, run)
    now = await _now(conn)
    names = steps_for(run.mode)
    if len(run.steps) >= len(names):
        return await _finish_run(conn, job.org_id, run, run.steps, None)
    step = Step(name=names[len(run.steps)], state="running", started_at=now)
    if step.name == "gateway_deny":
        version = await job.ports.snapshot.request(conn, job.org_id)
        step = replace(step, snapshot_version=version, confirm_by=now + job.timings.confirm_within)
    elif step.name in ("datagw_suspend", "egress_remove"):
        step = replace(step, snapshot_version=run.version)
    await _write(conn, job.org_id, run, (*run.steps, step))
    return None


async def _work(job: _Job, run: _Run, step: Step) -> str | None:  # noqa: PLR0911  (one return per step)
    match step.name:
        case "gateway_deny" | "datagw_suspend" | "egress_remove":
            return await _confirm(job, run, step)
        case "scale_to_zero":
            return await _scale(job, run, step)
        case "pause_timers":
            return await _pause(job, run, step)
        case "drop_databases" | "delete_files" | "delete_secrets" | "delete_services" as name:
            return await _remove_in_cell(job, run, step, name)
        case "remove_timers":
            return await _remove_timers(job, run, step)
        case "delete_bundles":
            return await _delete_bundles(job, run, step)
        case "remove_records":
            return await _remove_records(job, run, step)


async def _confirm(job: _Job, run: _Run, step: Step) -> str | None:
    """Done once the org's cell has the step's snapshot version. Only the gateway waits for it,
    checking every ``confirm_every`` until ``confirm_by`` within this job, outside any
    transaction; the steps after it take the answer as it is."""
    version = step.snapshot_version
    while True:
        confirmed = version is not None and await job.ports.snapshot.confirmed(job.org_id, version)
        if confirmed or step.confirm_by is None:
            break
        async with job.tx() as conn:  # the database's clock, which set ``confirm_by``; no lock
            left = (step.confirm_by - await _now(conn)).total_seconds()
        if left <= 0:
            break
        await job.timings.sleep(min(job.timings.confirm_every.total_seconds(), left))
    async with job.tx() as conn:
        await _lock(conn, job.org_id, run)
        done = step.finish(await _now(conn), "done" if confirmed else "unconfirmed")
        return await _record(conn, job.org_id, run, done)


async def _scale(job: _Job, run: _Run, step: Step) -> str | None:
    """Stop the next environment if this job holds its lock, else hand over to a job that does."""

    async def stop(driver: RuntimeDriver, _: OrgCell, env_id: str) -> None:
        await _stop(driver, service_name(env_id))

    return await _each_env(job, run, step, stop)


async def _each_env(
    job: _Job,
    run: _Run,
    step: Step,
    work: Callable[[RuntimeDriver, OrgCell, str], Awaitable[int | None]],
) -> str | None:
    """``work`` on the next environment the step has not done, prod first, in the job that holds
    that environment's lock; the step is done once none is left. What ``work`` counts adds up in
    ``removed``."""
    if job.ports.cells is None:
        raise StepFailedError(RUNTIME_UNAVAILABLE, final=True)
    try:
        cell = await job.ports.cells.for_org(job.org_id)
    except CellUnavailableError:
        raise StepFailedError(CELL_UNAVAILABLE, final=True) from None
    if cell.runtime is None:
        raise StepFailedError(RUNTIME_UNAVAILABLE, final=True)
    pending = [e for e in run.env_ids if e not in step.stopped]
    if not pending:
        async with job.tx() as conn:
            await _lock(conn, job.org_id, run)
            return await _record(conn, job.org_id, run, step.finish(await _now(conn), "done"))
    if job.held_env != pending[0]:
        await job.defer(run, env_id=pending[0])
        return RUNNING
    counted = await work(cell.runtime, cell, pending[0])
    async with job.tx() as conn:
        await _lock(conn, job.org_id, run)
        removed = step.removed if counted is None else (step.removed or 0) + counted
        stopped = replace(step, stopped=(*step.stopped, pending[0]), removed=removed)
        await _write(conn, job.org_id, run, (*run.steps[:-1], stopped))
    return None


async def _stop(driver: RuntimeDriver, service: str) -> None:
    """Scale ``service`` to zero, then check it serves nothing. A missing service is stopped."""
    try:
        await driver.scale_to_zero(service)
    except ServiceNotFoundError:
        pass
    seen = await driver.observe(service)
    if seen is not None and not seen.stopped:
        raise StepFailedError(NOT_STOPPED)


async def _pause(job: _Job, run: _Run, step: Step) -> str | None:
    async with job.tx() as conn:
        await _lock(conn, job.org_id, run)
        reason = cast("Mode", run.mode)  # no removal has this step
        paused = await job.ports.timers.pause_for_kill(
            conn, org_id=job.org_id, app_id=run.app_id, reason=reason, actor=run.actor
        )
        ids = tuple(dict.fromkeys((*run.paused, *paused)))
        done = step.finish(await _now(conn), "done")
        return await _record(conn, job.org_id, run, done, ids)


# ── a removal's steps (``lifecycle.removal`` does the deleting) ───────────────


async def _remove_in_cell(job: _Job, run: _Run, step: Step, name: removal.CellStep) -> str | None:
    """One environment's part of a cell step, in the job holding its lock like ``_scale``."""

    async def remove(_: RuntimeDriver, cell: OrgCell, env_id: str) -> int:
        try:
            return await removal.in_cell(
                job.ports.engine, cell, name, org_id=job.org_id, env_id=env_id, actor=run.actor
            )
        except removal.RemovalError as exc:
            raise StepFailedError(exc.code) from exc

    return await _each_env(job, run, step, remove)


async def _remove_timers(job: _Job, run: _Run, step: Step) -> str | None:
    async with job.tx() as conn:
        await _lock(conn, job.org_id, run)
        removed = await removal.remove_timers(
            conn, job.ports.timers, org_id=job.org_id, app_id=run.app_id, actor=run.actor
        )
        done = replace(step.finish(await _now(conn), "done"), removed=removed)
        return await _record(conn, job.org_id, run, done)


async def _delete_bundles(job: _Job, run: _Run, step: Step) -> str | None:
    removed = await removal.delete_bundles(job.ports, org_id=job.org_id, app_id=run.app_id)
    async with job.tx() as conn:
        await _lock(conn, job.org_id, run)
        done = replace(step.finish(await _now(conn), "done"), removed=removed)
        return await _record(conn, job.org_id, run, done)


async def _remove_records(job: _Job, run: _Run, step: Step) -> str | None:
    """The last step: the app is ``deleted`` in the transaction that completes the run."""
    async with job.tx() as conn:
        await _lock(conn, job.org_id, run)
        removed = await removal.remove_records(
            conn, org_id=job.org_id, app_id=run.app_id, actor=run.actor
        )
        done = replace(step.finish(await _now(conn), "done"), removed=removed)
        return await _record(conn, job.org_id, run, done)


async def _failed_try(job: _Job, run: _Run, step: Step, code: str, *, final: bool) -> str | None:
    """Count the try; fail the step after the last one, else try again after the backoff in a
    job holding the same lock."""
    tried = replace(step, attempts=step.attempts + 1, error=code)
    t = job.timings
    async with job.tx() as conn:
        await _lock(conn, job.org_id, run)
        now = await _now(conn)
        if final or tried.attempts >= t.max_attempts:
            return await _record(conn, job.org_id, run, tried.finish(now, "failed", tried=False))
        await _write(conn, job.org_id, run, (*run.steps[:-1], tried))
        await defer_kill_switch(
            conn,
            org_id=job.org_id,
            app_id=run.app_id,
            run_id=run.id,
            env_id=job.held_env,
            schedule_at=now + t.backoff * 2 ** (tried.attempts - 1),
        )
    return RUNNING


# ── start and enable, in the API's transaction ───────────────────────────────

_LOCK_APP = text("select status from ssc.app where org_id = :org and id = :app for update")
_LOCK_APP_SLUG = text(
    "select status, slug from ssc.app where org_id = :org and id = :app for update"
)
_WORK_IN_FLIGHT = text(
    "select 'deployment' as kind, id from ssc.deployment "
    "where org_id = :org and app_id = :app and state in ('pending', 'running') "
    "union all select 'build', id from ssc.build "
    "where org_id = :org and app_id = :app and state in ('queued', 'running') limit 1"
)
_SET_STATUS = text("update ssc.app set status = :status where org_id = :org and id = :app")
_INSERT_RUN = text(
    "insert into ssc.kill_switch_run (id, org_id, app_id, mode, actor_kind, actor_id, "
    "actor_via_agent, actor_client_id) values (:id, :org, :app, :mode, :kind, :actor, :via, "
    ":client)"
)
_UNRESUMED = text(
    "select id, mode, paused_schedule_ids from ssc.kill_switch_run "
    "where org_id = :org and app_id = :app and state <> 'running' and resumed_at is null "
    "order by started_at, id for update"
)
_MARK_RESUMED = text(
    "update ssc.kill_switch_run set resumed_at = now() where org_id = :org and id = :id"
)


def next_status(status: str, mode: Mode) -> str | None:
    """The app's status after the switch is pulled in ``mode``, or None when that is refused.
    An active app stops in either mode; a disabled one can still be quarantined."""
    if status == "active" or (status == "disabled" and mode == "quarantine"):
        return MODE_STATUS[mode]
    return None


@dataclass(frozen=True, slots=True)
class Started:
    run_id: str
    before: str
    after: str


async def _lock_app(conn: AsyncConnection, org_id: str, app_id: str) -> str:
    status = (await conn.execute(_LOCK_APP, {"org": org_id, "app": app_id})).scalar_one_or_none()
    if status is None or status == removal.DELETED:
        raise RefusedError(ErrorCode.NOT_FOUND, app_id=app_id)
    if status == removal.DELETING:
        raise RefusedError(ErrorCode.APP_DELETING, app_id=app_id)
    return str(status)


async def _refuse_in_flight(conn: AsyncConnection, org_id: str, app_id: str) -> None:
    running = (
        await conn.execute(_SELECT_RUNNING, {"org": org_id, "app": app_id})
    ).scalar_one_or_none()
    if running is not None:
        raise RefusedError(ErrorCode.KILL_SWITCH_IN_FLIGHT, run_id=str(running))


async def start(
    conn: AsyncConnection, *, org_id: str, app_id: str, mode: Mode, actor: Actor
) -> Started:
    """Stop the app in the caller's transaction and defer the saga. The caller audits the
    status change."""
    before = await _lock_app(conn, org_id, app_id)
    after = next_status(before, mode)
    if after is None:
        raise RefusedError(ErrorCode.APP_NOT_ACTIVE, app_id=app_id, status=before, mode=mode)
    await _refuse_in_flight(conn, org_id, app_id)
    await conn.execute(_SET_STATUS, {"org": org_id, "app": app_id, "status": after})
    run_id = new_id("kil")
    await conn.execute(
        _INSERT_RUN,
        {
            "id": run_id,
            "org": org_id,
            "app": app_id,
            "mode": mode,
            "kind": actor.kind.value,
            "actor": actor.id,
            "via": actor.via_agent,
            "client": actor.client_id,
        },
    )
    await mark_dirty(conn, org_id)
    await defer_kill_switch(conn, org_id=org_id, app_id=app_id, run_id=run_id)
    return Started(run_id=run_id, before=before, after=after)


async def start_delete(
    conn: AsyncConnection, *, org_id: str, app_id: str, confirm: str, actor: Actor
) -> Started:
    """Mark the app ``deleting`` in the caller's transaction and defer its removal run; the
    caller audits it. ``confirm`` must be the app's slug. Refused while a deployment (a deploy,
    promote or rollback) or a build of the app is under way, or a run of its kill switch. An app
    whose last removal failed is ``deleting`` already: this starts the run that finishes it."""
    params = {"org": org_id, "app": app_id}
    row = (await conn.execute(_LOCK_APP_SLUG, params)).first()
    if row is None or row.status == removal.DELETED:
        raise RefusedError(ErrorCode.NOT_FOUND, app_id=app_id)
    if confirm != row.slug:
        raise RefusedError(ErrorCode.CONFIRMATION_MISMATCH, app_id=app_id)
    await _refuse_in_flight(conn, org_id, app_id)
    work = (await conn.execute(_WORK_IN_FLIGHT, params)).first()
    if work is not None:
        raise RefusedError(
            ErrorCode.APP_OPERATION_IN_FLIGHT, app_id=app_id, kind=str(work.kind), id=str(work.id)
        )
    await conn.execute(_SET_STATUS, {**params, "status": removal.DELETING})
    run_id = new_id("kil")
    await conn.execute(
        _INSERT_RUN,
        {
            "id": run_id,
            "org": org_id,
            "app": app_id,
            "mode": DELETE,
            "kind": actor.kind.value,
            "actor": actor.id,
            "via": actor.via_agent,
            "client": actor.client_id,
        },
    )
    await mark_dirty(conn, org_id)
    await defer_kill_switch(conn, org_id=org_id, app_id=app_id, run_id=run_id)
    return Started(run_id=run_id, before=str(row.status), after=removal.DELETING)


async def enable(
    conn: AsyncConnection, *, org_id: str, app_id: str, timers: TimersPort, actor: Actor
) -> str:
    """Make the app active again in the caller's transaction and resume the schedules its
    finished runs paused; the status it had. The caller audits the change."""
    before = await _lock_app(conn, org_id, app_id)
    if before == "active":
        raise RefusedError(ErrorCode.APP_ALREADY_ACTIVE, app_id=app_id)
    await _refuse_in_flight(conn, org_id, app_id)
    await conn.execute(_SET_STATUS, {"org": org_id, "app": app_id, "status": "active"})
    for row in (await conn.execute(_UNRESUMED, {"org": org_id, "app": app_id})).all():
        ids = [str(i) for i in cast("list[object]", row.paused_schedule_ids)]
        if ids:
            await timers.resume_after_kill(
                conn,
                org_id=org_id,
                app_id=app_id,
                schedule_ids=ids,
                reason=cast("Mode", str(row.mode)),
                actor=actor,
            )
        await conn.execute(_MARK_RESUMED, {"org": org_id, "id": row.id})
    await mark_dirty(conn, org_id)
    return before


# ── the sweep ────────────────────────────────────────────────────────────────

_ALL_RUNNING = text(
    "select id, app_id from ssc.kill_switch_run where org_id = :org and state = 'running'"
)
_HAS_JOB = text(
    "select 1 from procrastinate.procrastinate_jobs "
    "where queueing_lock = :lock and status in ('todo', 'doing') limit 1"
)


async def sweep(engine: AsyncEngine) -> int:
    """Defer a job for every running run that has none waiting or running; how many."""
    deferred = 0
    for org_id in await all_org_ids(engine):
        try:
            async with bound_org(engine, org_id) as conn:
                for run_id, app_id in (await conn.execute(_ALL_RUNNING, {"org": org_id})).all():
                    lock = queueing_lock(str(app_id))
                    if (await conn.execute(_HAS_JOB, {"lock": lock})).first() is not None:
                        continue
                    job = await defer_kill_switch(
                        conn, org_id=org_id, app_id=str(app_id), run_id=str(run_id)
                    )
                    if job is not None:
                        log.warning("kill-switch run re-deferred", extra={"run_id": run_id})
                        deferred += 1
        except DBAPIError:
            log.exception("kill-switch sweep failed for one org", extra={"org_id": org_id})
    return deferred
