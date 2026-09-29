"""The schedule rows (SSC-041, decision 020).

A schedule is ``active`` (armed: ``next_run_at`` is set and a ``timers:run`` job waits for it),
``paused`` (with a ``pause_reason``) or ``deleted`` (terminal, SC007). Every change is audited in
the caller's transaction through the ``schedule`` view.

``Timers`` is the ``TimersPort``: the deploy job upserts an environment's schedules by name after
a deploy (a rollback keeps them), and the kill switch pauses and resumes an app's schedules. Preview
schedules are stored paused (``preview``) and only run by hand.

Authority: a schedule runs for its app's owner, as declared by the builder who deployed it. It is
paused (``owner_deactivated``, ``builder_access_revoked``) once the owner is no longer active or
the declaring builder may no longer change the environment (the ``require_builder`` rule). That
is checked when a deploy declares it, when the worker claims a run, and by the sweep; a person
who may change the environment resumes it and becomes its declarer, and so does a new deploy.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.ids import new_id
from ssc_control.audit.chain import Actor, NewEvent, append_event
from ssc_control.domain.schedule_time import next_after
from ssc_control.ports import DeclaredSchedule, KillReason, TimersPort
from ssc_control.timers.tasks import defer_manual_run, defer_scheduled_run

PauseReason = Literal[
    "manual",
    "preview",
    "app_disabled",
    "app_quarantined",
    "owner_deactivated",
    "builder_access_revoked",
]
Blocker = Literal["owner_deactivated", "builder_access_revoked"]
RefusalReason = Literal["not_found", "deleted", "in_flight", "app_inactive", "cannot_resume"]

KILL_PAUSES: Final[Mapping[KillReason, PauseReason]] = {
    "disable": "app_disabled",
    "quarantine": "app_quarantined",
}
AUTHORITY_PAUSES: Final[frozenset[str]] = frozenset({"owner_deactivated", "builder_access_revoked"})
_DEFINITION: Final = ("cron", "timezone", "path", "method", "timeout_seconds")
_VIEW: Final = (*_DEFINITION, "name", "state", "pause_reason", "environment_id")


class ScheduleRefusedError(Exception):
    def __init__(self, reason: RefusalReason, evidence: Mapping[str, object]) -> None:
        super().__init__(reason)
        self.reason: RefusalReason = reason
        self.evidence = dict(evidence)


def schedule_actor(schedule_id: str) -> Actor:
    """The actor of what a schedule's own runs and checks change."""
    return Actor(kind=ActorKind.SCHEDULE, id=schedule_id)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def may_build(user: str) -> str:
    """SQL: ``user`` (a column of ``ssc.user_account``) may change environment ``e`` of app
    ``a``. The rule of ``api.authz.require_builder``; a test keeps the two equal."""
    return (
        f"({user}.status = 'active' and ({user}.role = 'admin' or a.owner_user_id = {user}.id "  # noqa: S608  (constant SQL fragments)
        "or exists (select 1 from ssc.app_grant g where g.org_id = e.org_id "
        "and g.environment_id = e.id and g.role = 'builder' and (g.subject_kind = 'org' "
        f"or (g.subject_kind = 'user' and g.user_id = {user}.id) "
        "or (g.subject_kind = 'group' and exists (select 1 from ssc.group_member m "
        f"where m.org_id = g.org_id and m.group_id = g.group_id and m.user_id = {user}.id))))))"
    )


_COLUMNS: Final = (
    "s.id, s.environment_id, s.name, s.cron, s.timezone, s.path, s.method, s.timeout_seconds, "
    "s.state, s.pause_reason, s.next_run_at, s.last_scheduled_for, s.declared_by_user_id"
)
_ENV_AND_APP: Final = (
    "join ssc.environment e on e.org_id = s.org_id and e.id = s.environment_id "
    "join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
)
_SET_STATE = text(
    "update ssc.schedule set state = :state, pause_reason = :reason, next_run_at = :next, "
    "declared_by_user_id = coalesce(cast(:by as text), declared_by_user_id) "
    "where org_id = :org and id = :id"
)
_ENV = text(
    "select e.name, e.app_id, o.status = 'active' as owner_active, "  # noqa: S608  (constant SQL fragments)
    f"{may_build('d')} as declarer_may_build from ssc.environment e "
    "join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "join ssc.user_account o on o.org_id = a.org_id and o.id = a.owner_user_id "
    "left join ssc.user_account d on d.org_id = e.org_id and d.id = :by "
    "where e.org_id = :org and e.id = :env"
)
_LIVE_IN_ENV = text(
    f"select {_COLUMNS} from ssc.schedule s where s.org_id = :org and s.environment_id = :env "  # noqa: S608  (constant SQL fragments)
    "and s.state <> 'deleted' order by s.name for update"
)
_INSERT = text(
    "insert into ssc.schedule (id, org_id, environment_id, name, cron, timezone, path, method, "
    "timeout_seconds, state, pause_reason, next_run_at, declared_by_user_id) values (:id, :org, "
    ":env, :name, :cron, :timezone, :path, :method, :timeout_seconds, :state, :reason, :next, :by)"
)
_REDEFINE = text(
    "update ssc.schedule set cron = :cron, timezone = :timezone, path = :path, method = :method, "
    "timeout_seconds = :timeout_seconds, state = :state, pause_reason = :reason, "
    "next_run_at = :next, declared_by_user_id = :by where org_id = :org and id = :id"
)
_DELETE = text(
    "update ssc.schedule set state = 'deleted', pause_reason = null, next_run_at = null "
    "where org_id = :org and id = :id"
)
_ACTIVE_OF_APP = text(
    f"select {_COLUMNS} from ssc.schedule s {_ENV_AND_APP}"  # noqa: S608  (constant SQL fragments)
    "where s.org_id = :org and a.id = :app and s.state = 'active' order by s.id for update of s"
)
_KILL_PAUSED_OF_APP = text(
    f"select {_COLUMNS} from ssc.schedule s {_ENV_AND_APP}"  # noqa: S608  (constant SQL fragments)
    "where s.org_id = :org and a.id = :app and s.id = any(:ids) and s.state = 'paused' "
    "and s.pause_reason = :reason order by s.id for update of s"
)
# Active schedules whose owner or declarer lost authority. Rows another transaction holds are
# skipped, so a hook called after its audit never waits on a claim that waits on audit_head.
_BLOCKED = text(
    "select s.id, case when o.status <> 'active' then 'owner_deactivated' "  # noqa: S608  (constant SQL fragments)
    f"else 'builder_access_revoked' end as blocker, {_COLUMNS} from ssc.schedule s "
    f"{_ENV_AND_APP}"
    "join ssc.user_account o on o.org_id = a.org_id and o.id = a.owner_user_id "
    "join ssc.user_account d on d.org_id = s.org_id and d.id = s.declared_by_user_id "
    "where s.org_id = :org and s.state = 'active' "
    f"and (o.status <> 'active' or not {may_build('d')}) order by s.id for update of s skip locked"
)
_ONE = text(
    f"select {_COLUMNS}, e.name as environment_name, a.status as app_status, "  # noqa: S608  (constant SQL fragments)
    "o.status = 'active' as owner_active from ssc.schedule s "
    f"{_ENV_AND_APP}"
    "join ssc.user_account o on o.org_id = a.org_id and o.id = a.owner_user_id "
    "where s.org_id = :org and s.environment_id = :env and s.id = :id for update of s"
)
_IN_FLIGHT = text(
    "select id from ssc.timer_run where org_id = :org and schedule_id = :id "
    "and state in ('queued', 'running') limit 1"
)
_INSERT_MANUAL = text(
    "insert into ssc.timer_run (id, org_id, schedule_id, trigger, scheduled_for, "
    "requested_by_user_id, state) values (:id, :org, :sch, 'manual', :at, :by, 'queued')"
)


def view(row: RowMapping) -> dict[str, Any]:
    """The ``schedule`` audit view of a row."""
    return {k: row[k] for k in _VIEW}


async def _audit(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    actor: Actor,
    action: AuditAction,
    schedule_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=action,
            actor=actor,
            target_kind="schedule",
            target_id=schedule_id,
            before=before,
            after=after,
        ),
    )


async def set_state(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    row: RowMapping,
    reason: PauseReason | None,
    actor: Actor,
    now: datetime,
    declared_by: str | None = None,
) -> datetime | None:
    """Pause ``row`` for ``reason``, or arm it when ``reason`` is None, and audit the change as
    ``schedule.paused`` or ``schedule.resumed``. Returns the armed instant."""
    state = "paused" if reason is not None else "active"
    armed = None if reason is not None else next_after(row["cron"], row["timezone"], now)
    params = {"org": org_id, "id": row["id"], "state": state, "reason": reason}
    await conn.execute(_SET_STATE, {**params, "next": armed, "by": declared_by})
    if armed is not None:
        await defer_scheduled_run(conn, org_id=org_id, schedule_id=row["id"], instant=armed)
    await _audit(
        conn,
        org_id=org_id,
        actor=actor,
        action=AuditAction.SCHEDULE_RESUMED if armed else AuditAction.SCHEDULE_PAUSED,
        schedule_id=row["id"],
        before=view(row),
        after={**view(row), "state": state, "pause_reason": reason},
    )
    return armed


async def pause_blocked(conn: AsyncConnection, org_id: str) -> list[tuple[str, Blocker]]:
    """Pause every active schedule whose owner is no longer active or whose declarer may no
    longer build, as the schedule itself. A schedule locked elsewhere is left to the runner's
    claim or the next sweep. Returns what it paused, and why."""
    found = (await conn.execute(_BLOCKED, {"org": org_id})).mappings().all()
    paused: list[tuple[str, Blocker]] = []
    for row in found:
        blocker: Blocker = row["blocker"]
        await set_state(
            conn,
            org_id=org_id,
            row=row,
            reason=blocker,
            actor=schedule_actor(row["id"]),
            now=_utcnow(),
        )
        paused.append((str(row["id"]), blocker))
    return paused


def _blocker(env: RowMapping) -> Blocker | None:
    if not env["owner_active"]:
        return "owner_deactivated"
    if not env["declarer_may_build"]:
        return "builder_access_revoked"
    return None


def _declared_reason(
    environment_name: str, old: RowMapping | None, blocker: Blocker | None
) -> PauseReason | None:
    """Why a declared schedule is paused after a deploy; None when it is armed."""
    if environment_name != "prod":
        return "preview"
    if old is None or old["state"] == "active" or old["pause_reason"] in AUTHORITY_PAUSES:
        return blocker
    return old["pause_reason"]


def _change(old: RowMapping, new: Mapping[str, Any]) -> AuditAction | None:
    """The action that audits ``old`` becoming ``new``; None when the view is unchanged."""
    if any(old[k] != new[k] for k in _DEFINITION):
        return AuditAction.SCHEDULE_UPDATED
    if (old["state"], old["pause_reason"]) == (new["state"], new["pause_reason"]):
        return None
    if new["state"] == "active":
        return AuditAction.SCHEDULE_RESUMED
    return AuditAction.SCHEDULE_PAUSED


async def _declare(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    params: Mapping[str, str],
    s: DeclaredSchedule,
    old: RowMapping | None,
    reason: PauseReason | None,
    now: datetime,
    actor: Actor,
) -> None:
    """Insert or redefine one declared schedule. An active schedule whose cron and zone are
    unchanged keeps its armed instant; any other armed one is armed afresh from ``now``."""
    definition = {k: getattr(s, k) for k in _DEFINITION}
    state = "active" if reason is None else "paused"
    new = {**definition, "name": s.name, "state": state, "pause_reason": reason}
    new["environment_id"] = params["env"]
    armed = None if reason is not None else next_after(s.cron, s.timezone, now)
    rearm = armed is not None
    if (
        old is not None
        and armed is not None
        and old["state"] == "active"
        and (old["cron"], old["timezone"]) == (s.cron, s.timezone)
    ):
        armed, rearm = old["next_run_at"], False
    values = {**params, **definition, "name": s.name, "state": state, "reason": reason}
    values["next"] = armed
    org_id = params["org"]
    if old is None:
        schedule_id = new_id("sch")
        await conn.execute(_INSERT, {**values, "id": schedule_id})
        action, before = AuditAction.SCHEDULE_CREATED, None
    else:
        schedule_id = str(old["id"])
        await conn.execute(_REDEFINE, {**values, "id": schedule_id})
        action, before = _change(old, new), view(old)
    if rearm and armed is not None:
        await defer_scheduled_run(conn, org_id=org_id, schedule_id=schedule_id, instant=armed)
    if action is not None:
        await _audit(
            conn,
            org_id=org_id,
            actor=actor,
            action=action,
            schedule_id=schedule_id,
            before=before,
            after=new,
        )


class Timers(TimersPort):
    """The real ``TimersPort``. ``clock`` is now, in UTC."""

    def __init__(self, clock: Callable[[], datetime] = _utcnow) -> None:
        self._clock = clock

    async def sync_schedules(  # noqa: PLR0913  (keyword-only)
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        environment_id: str,
        declared: Sequence[DeclaredSchedule],
        declared_by_user_id: str,
        actor: Actor,
    ) -> None:
        """Create, redefine and delete ``environment_id``'s schedules to match ``declared``.

        ``declared_by_user_id`` becomes every schedule's declarer. In prod a new schedule is
        armed unless its owner or declarer lacks authority; a schedule paused for that reason
        is resumed when both have it now. Manual, preview and kill-switch pauses are kept."""
        params = {"org": org_id, "env": environment_id, "by": declared_by_user_id}
        env = (await conn.execute(_ENV, params)).mappings().one()
        rows = (await conn.execute(_LIVE_IN_ENV, params)).mappings().all()
        live = {str(r["name"]): r for r in rows}
        now, blocker = self._clock(), _blocker(env)
        for s in sorted(declared, key=lambda d: d.name):
            old = live.pop(s.name, None)
            await _declare(
                conn,
                params=params,
                s=s,
                old=old,
                reason=_declared_reason(env["name"], old, blocker),
                now=now,
                actor=actor,
            )
        for old in live.values():
            await conn.execute(_DELETE, {"org": org_id, "id": old["id"]})
            await _audit(
                conn,
                org_id=org_id,
                actor=actor,
                action=AuditAction.SCHEDULE_DELETED,
                schedule_id=str(old["id"]),
                before=view(old),
            )

    async def pause_for_kill(
        self, conn: AsyncConnection, *, org_id: str, app_id: str, reason: KillReason, actor: Actor
    ) -> list[str]:
        found = (await conn.execute(_ACTIVE_OF_APP, {"org": org_id, "app": app_id})).mappings()
        paused: list[str] = []
        for row in found.all():
            await set_state(
                conn,
                org_id=org_id,
                row=row,
                reason=KILL_PAUSES[reason],
                actor=actor,
                now=self._clock(),
            )
            paused.append(str(row["id"]))
        return paused

    async def resume_after_kill(  # noqa: PLR0913  (keyword-only)
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        app_id: str,
        schedule_ids: Sequence[str],
        reason: KillReason,
        actor: Actor,
    ) -> None:
        """Arm those of ``schedule_ids`` still paused for ``reason``; a schedule whose owner or
        declarer has lost authority meanwhile is re-paused for that instead."""
        params = {
            "org": org_id,
            "app": app_id,
            "ids": list(schedule_ids),
            "reason": KILL_PAUSES[reason],
        }
        found = (await conn.execute(_KILL_PAUSED_OF_APP, params)).mappings().all()
        now = self._clock()
        for row in found:
            by = {"org": org_id, "env": row["environment_id"], "by": row["declared_by_user_id"]}
            env = (await conn.execute(_ENV, by)).mappings().one()
            why = "preview" if env["name"] != "prod" else _blocker(env)
            await set_state(conn, org_id=org_id, row=row, reason=why, actor=actor, now=now)


# ── what the API does ────────────────────────────────────────────────────────


async def _locked(
    conn: AsyncConnection, org_id: str, environment_id: str, schedule_id: str
) -> RowMapping:
    params = {"org": org_id, "env": environment_id, "id": schedule_id}
    row = (await conn.execute(_ONE, params)).mappings().first()
    if row is None:
        raise ScheduleRefusedError("not_found", {"schedule_id": schedule_id})
    if row["state"] == "deleted":
        raise ScheduleRefusedError("deleted", {"schedule_id": schedule_id})
    return row


async def pause(
    conn: AsyncConnection, *, org_id: str, environment_id: str, schedule_id: str, actor: Actor
) -> None:
    """Pause by hand. A schedule already paused by hand, or in preview, is left as it is; one
    paused by SSC becomes paused by hand, so nothing but a person resumes it."""
    row = await _locked(conn, org_id, environment_id, schedule_id)
    if row["pause_reason"] in ("manual", "preview"):
        return
    await set_state(conn, org_id=org_id, row=row, reason="manual", actor=actor, now=_utcnow())


async def resume(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    environment_id: str,
    schedule_id: str,
    user_id: str,
    actor: Actor,
    now: datetime,
) -> None:
    """Resume by hand; ``user_id`` (who may change the environment) becomes the declarer.

    Refused (``cannot_resume``) in preview, while the kill switch holds it, while the app is not
    active, and while the app's owner is not active."""
    row = await _locked(conn, org_id, environment_id, schedule_id)
    if row["state"] == "active":
        return
    evidence = {"schedule_id": schedule_id, "pause_reason": row["pause_reason"]}
    if (
        row["environment_name"] != "prod"
        or row["pause_reason"] in KILL_PAUSES.values()
        or row["app_status"] != "active"
        or not row["owner_active"]
    ):
        raise ScheduleRefusedError("cannot_resume", evidence)
    await set_state(
        conn, org_id=org_id, row=row, reason=None, actor=actor, now=now, declared_by=user_id
    )


async def request_run(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    environment_id: str,
    schedule_id: str,
    user_id: str,
    actor: Actor,
    now: datetime,
) -> str:
    """Queue a manual run of a live schedule, paused ones and preview ones included; the new
    ``tmr_`` id. Refused while one is queued or running, and while the app is not active."""
    row = await _locked(conn, org_id, environment_id, schedule_id)
    if row["app_status"] != "active":
        raise ScheduleRefusedError("app_inactive", {"schedule_id": schedule_id})
    busy = (await conn.execute(_IN_FLIGHT, {"org": org_id, "id": schedule_id})).first()
    if busy is not None:
        raise ScheduleRefusedError("in_flight", {"schedule_id": schedule_id, "run_id": busy[0]})
    run_id = new_id("tmr")
    await conn.execute(
        _INSERT_MANUAL, {"id": run_id, "org": org_id, "sch": schedule_id, "at": now, "by": user_id}
    )
    await defer_manual_run(conn, org_id=org_id, schedule_id=schedule_id, run_id=run_id)
    await _audit(
        conn,
        org_id=org_id,
        actor=actor,
        action=AuditAction.SCHEDULE_RUN_REQUESTED,
        schedule_id=schedule_id,
        after={"environment_id": environment_id, "run_id": run_id},
    )
    return run_id
