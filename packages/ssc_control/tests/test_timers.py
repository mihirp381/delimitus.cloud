"""SSC-041 (A5): timers, against postgres:18, the fake dispatcher and the real ``Timers`` port.

Ticket "done when" checks (decision 020):
  * a deploy upserts schedules by name     -> test_a_deploy_creates_redefines_and_deletes_by_name
  * prod armed only on live authority      -> test_a_deploy_arms_prod_only_on_live_authority
  * preview is paused and runs by hand     -> test_preview_schedules_are_paused_and_run_by_hand
  * an instant fires once                  -> test_a_due_instant_runs_once_and_arms_the_next
  * missed instants coalesce               -> test_missed_instants_coalesce_into_one_late_run
  * no overlap                             -> test_a_run_never_overlaps_another
  * timeouts                               -> test_a_run_past_its_timeout_is_cancelled
  * outcomes                               -> test_what_the_app_answers_decides_the_outcome
  * the sweep                              -> test_the_sweep_closes_abandoned_runs_and_rearms
  * a stopped app                          -> test_a_stopped_app_skips_instants_and_manual_runs
  * the kill switch pauses, enable resumes -> test_the_kill_switch_pauses_and_enable_resumes
  * the owner is deactivated               -> test_a_deactivated_owner_pauses_the_apps_schedules,
                                              test_the_runner_and_sweep_catch_what_hooks_missed
  * the declarer loses access              -> test_a_revoked_builder_pauses_what_they_declared,
                                              test_directory_changes_pause_what_lost_authority,
                                              test_an_owner_transfer_is_caught_by_the_sweep
Plus: pause and resume by hand, the API's authorisation, deletion and paging, cross-org reads,
``may_build`` against ``require_builder``, the fake dispatcher, the worker's wiring and a real
worker pass, and revision 0013's round trip.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from procrastinate import App, PsycopgConnector
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from ssc_testkit import ISSUER, Dsns, SigningKey, assert_problem, auth, make_org, mint, new_key

from ssc_contracts.audit import ActorKind
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_contracts.manifest import Schedule
from ssc_control import directory
from ssc_control.api import Settings, create_app
from ssc_control.api.authz import _SELECT_BUILDER  # pyright: ignore[reportPrivateUsage]
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.audit import Actor
from ssc_control.db import (
    MIGRATE_ROLE,
    NewOrg,
    bind_org_sync,
    bound_org,
    create_org,
    downgrade,
    make_engine,
    upgrade,
)
from ssc_control.lifecycle import kill_switch
from ssc_control.lifecycle import tasks as lifecycle_tasks
from ssc_control.lifecycle.kill_switch import Timings
from ssc_control.metrics import metrics_port
from ssc_control.runtime.fake import FakeRuntimeDriver
from ssc_control.timers import jobs as timers_jobs
from ssc_control.timers import service, tasks
from ssc_control.timers.dispatch import (
    DispatchResult,
    FakeScheduleDispatcher,
    ScheduleDispatcher,
    Scripted,
    TimerCall,
)
from ssc_control.timers.runner import RunDeps, run_timer, sweep_org
from ssc_control.timers.service import Timers, may_build
from ssc_control.worker import (
    CompositionError,
    build_app,
    compose_ports,
    queue_conninfo,
    refuse_fakes,
    timer_dispatcher_from_env,
)
from ssc_control.worker_ports import PORTS_KEY, Ports

MASTER = bytes(range(32))
T0 = datetime(2026, 9, 29, 10, 2, tzinfo=UTC)
FAST = Timings(
    confirm_within=timedelta(seconds=5), confirm_every=timedelta(0), backoff=timedelta(0)
)
OPERATOR = Actor(ActorKind.OPERATOR, "directory-sync")
OWNER_SUBJECT = "owner-subject"


def at(minute: int, second: int = 0) -> datetime:
    """``minute`` past 10:00 UTC on T0's day."""
    return T0.replace(minute=minute, second=second)


# ── world ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class World:
    org: str
    admin: str  # the org's first admin
    owner: str  # a member, synced from the directory, who owns the app
    builder: str  # a member with a builder grant on prod and preview
    member: str  # an active member with no grant
    app: str
    prod: str
    preview: str


@dataclass(frozen=True)
class Tokens:
    admin: str
    owner: str
    builder: str
    member: str


def owner_entry(status: str = "active") -> directory.DirectoryUser:
    return directory.DirectoryUser(
        issuer=ISSUER,
        subject=OWNER_SUBJECT,
        display_name="Olu Owner",
        email="olu@example.com",
        role="member",
        status=status,  # type: ignore[arg-type]
    )


def add_account(conn: psycopg.Connection[Any], org: str, role: str) -> str:
    uid = new_id("usr")
    conn.execute(
        "insert into ssc.user_account (id, org_id, display_name, email, role, status) "
        "values (%s, %s, 'Some One', 'someone@example.com', %s, 'active')",
        (uid, org, role),
    )
    return uid


async def make_world(dsn: str) -> World:
    engine = make_engine(dsn)
    try:
        spec = NewOrg("Timers", "Ada Admin", "ada@example.com", ISSUER, new_id("usr"))
        created = await create_org(engine, spec)
        org, admin = created.org_id, created.admin_user_id
        async with bound_org(engine, org) as conn:
            owner = (await directory.upsert_user(conn, org, owner_entry(), actor=OPERATOR)).user_id
    finally:
        await engine.dispose()
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        builder, member = add_account(conn, org, "member"), add_account(conn, org, "member")
        app, prod, preview = new_id("app"), new_id("env"), new_id("env")
        conn.execute(
            "insert into ssc.app (id, org_id, slug, owner_user_id) values (%s, %s, 'ledger', %s)",
            (app, org, owner),
        )
        for env, name in ((prod, "prod"), (preview, "preview")):
            conn.execute(
                "insert into ssc.environment (id, org_id, app_id, name) values (%s, %s, %s, %s)",
                (env, org, app, name),
            )
            conn.execute(
                "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, "
                "user_id, granted_by_user_id) values (%s, %s, %s, 'builder', 'user', %s, %s)",
                (new_id("gnt"), org, env, builder, admin),
            )
    return World(org, admin, owner, builder, member, app, prod, preview)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Bench:
    client: TestClient
    w: World
    t: Tokens
    dsn: str
    engine: AsyncEngine
    clock: Clock
    timers: Timers
    dispatcher: ScheduleDispatcher | None


@pytest.fixture
async def world(dsns: Dsns) -> World:
    return await make_world(dsns.app)


def token_for(signing_key: SigningKey, org: str, sub: str) -> str:
    return mint(signing_key, org=org, sub=sub, jti=f"cred_{new_key()[:16]}")


@pytest.fixture
async def b(dsns: Dsns, signing_key: SigningKey, world: World) -> AsyncIterator[Bench]:
    settings = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
        metrics_key=MASTER,
    )
    people = (world.admin, world.owner, world.builder, world.member)
    tokens = Tokens(*(token_for(signing_key, world.org, u) for u in people))
    engine = make_engine(dsns.app)
    clock = Clock(T0)
    with TestClient(create_app(settings)) as client:
        yield Bench(
            client, world, tokens, dsns.app, engine, clock, Timers(clock), FakeScheduleDispatcher()
        )
    await engine.dispose()


# ── helpers ──────────────────────────────────────────────────────────────────


def rows_of(dsn: str, org: str, sql: str, *args: object) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        bind_org_sync(conn, org)
        return conn.execute(sql, args).fetchall()


def execute(dsn: str, org: str, sql: str, *args: object) -> None:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        conn.execute(sql, args)


def declared(name: str = "tick", cron: str = "*/5 * * * *", **fields: Any) -> Schedule:
    return Schedule.model_validate({"name": name, "cron": cron, "path": f"/tasks/{name}", **fields})


async def sync(b: Bench, env: str, *schedules: Schedule, by: str | None = None) -> None:
    """What the deploy job does after a healthy deploy of ``schedules`` to ``env``."""
    person = by or b.w.builder
    async with bound_org(b.engine, b.w.org) as conn:
        await b.timers.sync_schedules(
            conn,
            org_id=b.w.org,
            environment_id=env,
            declared=schedules,
            declared_by_user_id=person,
            actor=Actor(ActorKind.USER, person),
        )


def live(b: Bench, env: str) -> dict[str, dict[str, Any]]:
    rows = rows_of(
        b.dsn,
        b.w.org,
        "select * from ssc.schedule where environment_id = %s and state <> 'deleted'",
        env,
    )
    return {r["name"]: r for r in rows}


def schedule(b: Bench, sid: str) -> dict[str, Any]:
    (row,) = rows_of(b.dsn, b.w.org, "select * from ssc.schedule where id = %s", sid)
    return row


async def armed(b: Bench, *schedules: Schedule) -> str:
    """Deploy ``schedules`` (default one ``tick`` every five minutes) to prod; the first's id."""
    wanted = schedules or (declared(),)
    await sync(b, b.w.prod, *wanted)
    return str(live(b, b.w.prod)[wanted[0].name]["id"])


def runs(b: Bench, sid: str) -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select * from ssc.timer_run where schedule_id = %s order by scheduled_for, created_at",
        sid,
    )


def audit(b: Bench, target: str) -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select action, actor_kind, actor_id, before, after from ssc.audit_event "
        "where target_id = %s order by seq",
        target,
    )


def actions(b: Bench, target: str) -> list[tuple[str, str, str]]:
    return [(r["action"], r["actor_kind"], r["actor_id"]) for r in audit(b, target)]


def jobs(dsn: str, sid: str, status: str | None = "todo") -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "select id, status::text as status, lock, queueing_lock, priority, scheduled_at, "
            "args from procrastinate.procrastinate_jobs where task_name = %s "
            "and args->>'schedule_id' = %s and (%s::text is null or status::text = %s) "
            "order by id",
            (tasks.RUN, sid, status, status),
        ).fetchall()


def due(b: Bench, sid: str) -> dict[str, Any]:
    (job,) = [j for j in jobs(b.dsn, sid) if "scheduled_for" in j["args"]]
    return job


def manual(b: Bench, sid: str) -> dict[str, Any]:
    (job,) = [j for j in jobs(b.dsn, sid) if "run_id" in j["args"]]
    return job


def set_job(dsn: str, job_id: int, status: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("set search_path to procrastinate")  # its triggers name types unqualified
        conn.execute("update procrastinate_jobs set status = %s where id = %s", (status, job_id))


async def fire(b: Bench, job: dict[str, Any], via: ScheduleDispatcher | None = None) -> str:
    """Run one ``timers:run`` job as the worker would; what became of it."""
    set_job(b.dsn, job["id"], "doing")
    deps = RunDeps(
        engine=b.engine,
        dispatcher=via or b.dispatcher,
        metrics=metrics_port(MASTER),
        clock=b.clock,
    )
    outcome = await run_timer(deps, **job["args"])
    set_job(b.dsn, job["id"], "succeeded")
    return outcome


def url(b: Bench, env: str, sid: str = "", suffix: str = "") -> str:
    base = f"/v1/apps/{b.w.app}/environments/{env}/schedules"
    return base + (f"/{sid}" if sid else "") + suffix


def post(b: Bench, path: str, token: str | None = None, body: Any = None) -> Response:
    headers = auth(token or b.t.admin, **{IDEMPOTENCY_HEADER: new_key()})
    return b.client.post(path, json={} if body is None else body, headers=headers)


def get(b: Bench, path: str, token: str | None = None) -> Response:
    return b.client.get(path, headers=auth(token or b.t.admin))


def run_now(b: Bench, env: str, sid: str, token: str | None = None) -> str:
    r = post(b, url(b, env, sid, "/runs"), token or b.t.builder)
    assert r.status_code == 202, r.text
    return str(r.json()["run_id"])


async def sync_person(b: Bench, entry: directory.DirectoryUser) -> str:
    async with bound_org(b.engine, b.w.org) as conn:
        return (await directory.upsert_user(conn, b.w.org, entry, actor=OPERATOR)).user_id


def add_run(  # noqa: PLR0913
    b: Bench,
    sid: str,
    state: str,
    *,
    scheduled_for: datetime,
    started_at: datetime | None = None,
    by: str | None = None,
) -> str:
    rid = new_id("tmr")
    execute(
        b.dsn,
        b.w.org,
        "insert into ssc.timer_run (id, org_id, schedule_id, trigger, scheduled_for, "
        "requested_by_user_id, state, started_at) values (%s, %s, %s, %s, %s, %s, %s, %s)",
        rid,
        b.w.org,
        sid,
        "manual" if by else "schedule",
        scheduled_for,
        by,
        state,
        started_at,
    )
    return rid


# ── declared by a deploy ─────────────────────────────────────────────────────


async def test_a_deploy_creates_redefines_and_deletes_by_name(b: Bench) -> None:
    london = declared("nightly", "0 3 * * *", timezone="Europe/London", method="GET")
    await sync(b, b.w.prod, declared(), london)
    rows = live(b, b.w.prod)
    tick, nightly = rows["tick"], rows["nightly"]
    assert (tick["state"], tick["pause_reason"], tick["next_run_at"]) == ("active", None, at(5))
    assert tick["declared_by_user_id"] == b.w.builder
    assert (tick["path"], tick["method"], tick["timeout_seconds"]) == ("/tasks/tick", "POST", 60)
    # 03:00 in London is 02:00 UTC in September (BST).
    assert nightly["next_run_at"] == datetime(2026, 9, 30, 2, tzinfo=UTC)
    assert nightly["method"] == "GET"
    (job,) = jobs(b.dsn, tick["id"])
    assert job["queueing_lock"] == f"sch:{tick['id']}:20260929T1005Z"
    assert (job["lock"], job["priority"], job["scheduled_at"]) == (None, 0, at(5))
    assert job["args"] == {
        "org_id": b.w.org,
        "schedule_id": tick["id"],
        "scheduled_for": "2026-09-29T10:05:00+00:00",
    }
    assert actions(b, tick["id"]) == [("schedule.created", "user", b.w.builder)]
    assert audit(b, tick["id"])[0]["after"] == {
        "name": "tick",
        "cron": "*/5 * * * *",
        "timezone": "UTC",
        "path": "/tasks/tick",
        "method": "POST",
        "timeout_seconds": 60,
        "state": "active",
        "pause_reason": None,
        "environment_id": b.w.prod,
    }

    # The same manifest again changes nothing: no audit, no second job, the same instant.
    b.clock.now = at(3)
    await sync(b, b.w.prod, declared(), london)
    assert live(b, b.w.prod)["tick"]["next_run_at"] == at(5)
    assert len(jobs(b.dsn, tick["id"])) == 1
    assert len(audit(b, tick["id"])) == 1

    # A new cron re-arms, a missing name is deleted, a new name is created.
    await sync(b, b.w.prod, declared(cron="*/10 * * * *"), declared("hourly", "0 * * * *"))
    rows = live(b, b.w.prod)
    assert set(rows) == {"tick", "hourly"}
    assert rows["tick"]["id"] == tick["id"]
    assert rows["tick"]["next_run_at"] == at(10)
    old, new = jobs(b.dsn, tick["id"])
    assert new["queueing_lock"] == f"sch:{tick['id']}:20260929T1010Z"
    gone = schedule(b, nightly["id"])
    assert (gone["state"], gone["pause_reason"], gone["next_run_at"]) == ("deleted", None, None)
    assert [a for a, _, _ in actions(b, tick["id"])] == ["schedule.created", "schedule.updated"]
    updated = audit(b, tick["id"])[1]
    assert (updated["before"]["cron"], updated["after"]["cron"]) == ("*/5 * * * *", "*/10 * * * *")
    assert [a for a, _, _ in actions(b, nightly["id"])] == ["schedule.created", "schedule.deleted"]
    b.clock.now = at(5)
    assert await fire(b, old) == "stale"  # 10:05 is no longer armed
    assert runs(b, tick["id"]) == []

    # A deleted name may be declared again, as a new schedule; the deleted one stays deleted.
    await sync(b, b.w.prod, declared(cron="*/10 * * * *"), declared("hourly", "0 * * * *"), london)
    again = live(b, b.w.prod)["nightly"]
    assert again["id"] != nightly["id"] and again["state"] == "active"
    assert schedule(b, nightly["id"])["state"] == "deleted"


async def test_a_deploy_arms_prod_only_on_live_authority(b: Bench) -> None:
    # A declarer who may not build (an agent deploy falls back to the owner, say).
    await sync(b, b.w.prod, declared(), by=b.w.member)
    tick = live(b, b.w.prod)["tick"]
    assert (tick["state"], tick["pause_reason"]) == ("paused", "builder_access_revoked")
    assert jobs(b.dsn, tick["id"]) == []
    assert audit(b, tick["id"])[0]["after"]["pause_reason"] == "builder_access_revoked"
    # The next deploy by a builder arms it.
    await sync(b, b.w.prod, declared())
    assert live(b, b.w.prod)["tick"]["state"] == "active"
    assert [a for a, _, _ in actions(b, tick["id"])] == ["schedule.created", "schedule.resumed"]
    # While the owner is not active, a deploy holds every schedule it declares.
    execute(
        b.dsn,
        b.w.org,
        "update ssc.user_account set status = 'deactivated', deactivated_at = now() where id = %s",
        b.w.owner,
    )
    await sync(b, b.w.prod, declared(), declared("more"))
    rows = live(b, b.w.prod)
    assert (rows["more"]["state"], rows["more"]["pause_reason"]) == ("paused", "owner_deactivated")
    assert (rows["tick"]["state"], rows["tick"]["pause_reason"]) == ("paused", "owner_deactivated")
    assert actions(b, tick["id"])[-1] == ("schedule.paused", "user", b.w.builder)


async def test_preview_schedules_are_paused_and_run_by_hand(b: Bench) -> None:
    await sync(b, b.w.preview, declared())
    row = live(b, b.w.preview)["tick"]
    sid = row["id"]
    assert (row["state"], row["pause_reason"], row["next_run_at"]) == ("paused", "preview", None)
    assert jobs(b.dsn, sid) == []

    r = post(b, url(b, b.w.preview, sid, "/runs"), b.t.builder)
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    assert r.json() == {"run_id": run_id, "state": "queued"}
    assert r.headers["location"] == url(b, b.w.preview, sid, f"/runs/{run_id}")
    assert_problem(
        post(b, url(b, b.w.preview, sid, "/runs"), b.t.builder), ErrorCode.TIMER_RUN_IN_FLIGHT
    )
    job = manual(b, sid)
    assert job["queueing_lock"] == f"tmr:{run_id}"
    assert (job["lock"], job["priority"]) == (None, tasks.MANUAL_PRIORITY)
    assert job["args"] == {"org_id": b.w.org, "schedule_id": sid, "run_id": run_id}
    queued = get(b, r.headers["location"], b.t.builder).json()
    assert (queued["state"], queued["trigger"], queued["requested_by_user_id"]) == (
        "queued",
        "manual",
        b.w.builder,
    )

    assert await fire(b, job) == "succeeded"
    assert await fire(b, job) == "stale"  # a retry of the same job
    done = get(b, r.headers["location"], b.t.builder).json()
    assert (done["state"], done["http_status"], done["error"]) == ("succeeded", 200, None)
    assert done["started_at"] is not None and done["finished_at"] is not None
    assert cast("FakeScheduleDispatcher", b.dispatcher).calls == [
        TimerCall(
            org_id=b.w.org,
            environment_id=b.w.preview,
            schedule_id=sid,
            run_id=run_id,
            method="POST",
            path="/tasks/tick",
        )
    ]
    assert actions(b, sid) == [
        ("schedule.created", "user", b.w.builder),
        ("schedule.run_requested", "user", b.w.builder),
    ]
    assert audit(b, sid)[1]["after"] == {"environment_id": b.w.preview, "run_id": run_id}
    (event,) = rows_of(
        b.dsn,
        b.w.org,
        "select app_id, pseudonym, properties from ssc.metrics_event where kind = 'timer_run'",
    )
    assert event["app_id"] == b.w.app and event["pseudonym"] is not None
    assert event["properties"] == {
        "trigger": "manual",
        "environment": "preview",
        "outcome": "succeeded",
        "duration_ms": done["duration_ms"],
    }
    # Preview never resumes; pausing it by hand leaves it as it is.
    assert_problem(
        post(b, url(b, b.w.preview, sid, "/resume"), b.t.builder), ErrorCode.SCHEDULE_CANNOT_RESUME
    )
    paused = post(b, url(b, b.w.preview, sid, "/pause"), b.t.builder)
    assert paused.status_code == 200, paused.text
    assert (paused.json()["state"], paused.json()["pause_reason"]) == ("paused", "preview")
    assert paused.json()["last_run"]["run_id"] == run_id
    assert len(audit(b, sid)) == 2


async def test_pause_and_resume_by_hand(b: Bench) -> None:
    sid = await armed(b)
    job = due(b, sid)
    r = post(b, url(b, b.w.prod, sid, "/pause"), b.t.builder)
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["pause_reason"], r.json()["next_run_at"]) == (
        "paused",
        "manual",
        None,
    )
    assert actions(b, sid)[-1] == ("schedule.paused", "user", b.w.builder)
    b.clock.now = at(5)
    assert await fire(b, job) == "stale"
    assert runs(b, sid) == []
    # A deploy keeps a pause by hand.
    await sync(b, b.w.prod, declared())
    assert schedule(b, sid)["pause_reason"] == "manual"

    before = datetime.now(UTC)
    r = post(b, url(b, b.w.prod, sid, "/resume"), b.t.owner)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["state"], body["pause_reason"], body["declared_by_user_id"]) == (
        "active",
        None,
        b.w.owner,
    )
    armed_at = datetime.fromisoformat(body["next_run_at"])
    assert before < armed_at <= before + timedelta(minutes=5)
    assert actions(b, sid)[-1] == ("schedule.resumed", "user", b.w.owner)
    assert len(jobs(b.dsn, sid)) == 1
    again = post(b, url(b, b.w.prod, sid, "/resume"), b.t.owner)
    assert again.status_code == 200 and again.json()["next_run_at"] == body["next_run_at"]
    assert len(audit(b, sid)) == 3


# ── one run ──────────────────────────────────────────────────────────────────


async def test_a_due_instant_runs_once_and_arms_the_next(b: Bench) -> None:
    sid = await armed(b)
    job = due(b, sid)
    b.clock.now = at(5, 1)
    assert await fire(b, job) == "succeeded"
    assert await fire(b, job) == "stale"  # the same instant again: a retry or a duplicate job
    assert len(cast("FakeScheduleDispatcher", b.dispatcher).calls) == 1
    (run,) = runs(b, sid)
    assert (run["trigger"], run["state"], run["scheduled_for"]) == ("schedule", "succeeded", at(5))
    assert (run["started_at"], run["requested_by_user_id"], run["http_status"]) == (
        at(5, 1),
        None,
        200,
    )
    s = schedule(b, sid)
    assert (s["next_run_at"], s["last_scheduled_for"]) == (at(10), at(5))
    assert due(b, sid)["queueing_lock"] == f"sch:{sid}:20260929T1010Z"
    # The database refuses a second run of one instant whatever the code does.
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_run(b, sid, "running", scheduled_for=at(5), started_at=at(5))
    # A run's outcome is only in timer_run and metrics: the schedule's audit is its definition.
    assert [a for a, _, _ in actions(b, sid)] == ["schedule.created"]


async def test_missed_instants_coalesce_into_one_late_run(b: Bench) -> None:
    sid = await armed(b)
    b.clock.now = at(27)  # 10:05 to 10:25 were missed
    assert await fire(b, due(b, sid)) == "succeeded"
    (run,) = runs(b, sid)
    assert run["scheduled_for"] == at(5)
    assert schedule(b, sid)["next_run_at"] == at(30)
    assert due(b, sid)["queueing_lock"] == f"sch:{sid}:20260929T1030Z"


class Held:
    """A dispatcher that answers 200 once ``release`` is set."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def dispatch(self, call: TimerCall) -> DispatchResult:
        self.entered.set()
        await self.release.wait()
        return DispatchResult(http_status=200)


async def test_a_run_never_overlaps_another(b: Bench) -> None:
    sid = await armed(b)
    scheduled = due(b, sid)
    first = run_now(b, b.w.prod, sid)
    held = Held()
    b.clock.now = at(4, 30)
    running = asyncio.create_task(fire(b, manual(b, sid), held))
    await asyncio.wait_for(held.entered.wait(), 10)
    # While the manual run runs, the instant is skipped (and the next armed) and no run is taken.
    b.clock.now = at(5)
    assert await fire(b, scheduled) == "skipped"
    assert_problem(
        post(b, url(b, b.w.prod, sid, "/runs"), b.t.builder), ErrorCode.TIMER_RUN_IN_FLIGHT
    )
    held.release.set()
    assert await running == "succeeded"
    assert {(r["trigger"], r["state"], r["error"]) for r in runs(b, sid)} == {
        ("schedule", "skipped", "overlap"),
        ("manual", "succeeded", None),
    }
    assert schedule(b, sid)["next_run_at"] == at(10)

    # A manual run that waited while an instant started is skipped when its turn comes.
    second = run_now(b, b.w.prod, sid)
    waiting = manual(b, sid)
    held = Held()
    b.clock.now = at(10)
    running = asyncio.create_task(fire(b, due(b, sid), held))
    await asyncio.wait_for(held.entered.wait(), 10)
    assert await fire(b, waiting) == "skipped"
    held.release.set()
    assert await running == "succeeded"
    states = {r["id"]: (r["state"], r["error"]) for r in runs(b, sid)}
    assert states[first] == ("succeeded", None)
    assert states[second] == ("skipped", "overlap")
    assert cast("FakeScheduleDispatcher", b.dispatcher).calls == []


async def test_a_run_past_its_timeout_is_cancelled(b: Bench) -> None:
    sid = await armed(b, declared(timeout_seconds=1))
    slow = FakeScheduleDispatcher([Scripted(delay=5)])
    b.clock.now = at(5)
    started = time.monotonic()
    assert await fire(b, due(b, sid), slow) == "timed_out"
    assert time.monotonic() - started < 4
    (run,) = runs(b, sid)
    assert (run["state"], run["error"], run["http_status"]) == ("timed_out", "timeout", None)
    assert 900 <= run["duration_ms"] < 3000
    assert slow.cancelled == slow.calls and len(slow.calls) == 1 and slow.answered == []


class Answering:
    def __init__(self, result: DispatchResult) -> None:
        self.result = result

    async def dispatch(self, call: TimerCall) -> DispatchResult:
        return self.result


@pytest.mark.parametrize(
    ("dispatcher", "state", "error", "status"),
    [
        (lambda: None, "failed", "dispatch_unavailable", None),
        (lambda: FakeScheduleDispatcher([Scripted(status=204)]), "succeeded", None, 204),
        (lambda: FakeScheduleDispatcher([Scripted(status=302)]), "failed", "http_error", 302),
        (lambda: FakeScheduleDispatcher([Scripted(status=503)]), "failed", "http_error", 503),
        (
            lambda: FakeScheduleDispatcher([Scripted(raises=OSError())]),
            "failed",
            "dispatch_error",
            None,
        ),
        (
            lambda: FakeScheduleDispatcher([Scripted(raises=TimeoutError())]),
            "failed",
            "dispatch_error",
            None,
        ),
        (
            lambda: Answering(DispatchResult(error="dispatch_error")),
            "failed",
            "dispatch_error",
            None,
        ),
    ],
    ids=["none", "204", "302", "503", "oserror", "own-timeout", "no-answer"],
)
async def test_what_the_app_answers_decides_the_outcome(  # noqa: PLR0913
    b: Bench, dispatcher: Any, state: str, error: str | None, status: int | None
) -> None:
    b.dispatcher = dispatcher()
    sid = await armed(b)
    b.clock.now = at(5)
    assert await fire(b, due(b, sid)) == state
    (run,) = runs(b, sid)
    assert (run["state"], run["error"], run["http_status"]) == (state, error, status)
    assert schedule(b, sid)["next_run_at"] == at(10)  # a failure never stops the schedule
    (event,) = rows_of(
        b.dsn,
        b.w.org,
        "select pseudonym, properties from ssc.metrics_event where kind = 'timer_run'",
    )
    assert event["pseudonym"] is None
    assert (event["properties"]["trigger"], event["properties"]["outcome"]) == ("schedule", state)


async def test_the_sweep_closes_abandoned_runs_and_rearms(b: Bench) -> None:
    names = ("stuck", "fresh", "lost", "waited")
    await sync(b, b.w.prod, *(declared(n) for n in names))
    rows = live(b, b.w.prod)
    stuck, fresh, lost, waited = (str(rows[n]["id"]) for n in names)
    now = at(30)
    b.clock.now = now
    # timeout 60 s plus a minute of slack: 121 s after it started a run is abandoned.
    dead = add_run(
        b, stuck, "running", scheduled_for=at(5), started_at=now - timedelta(seconds=121)
    )
    alive = add_run(
        b, fresh, "running", scheduled_for=at(5), started_at=now - timedelta(seconds=119)
    )
    # A manual run waits at most an hour for the worker.
    old = add_run(b, waited, "queued", scheduled_for=now - timedelta(minutes=61), by=b.w.builder)
    young = add_run(b, fresh, "queued", scheduled_for=now - timedelta(minutes=59), by=b.w.builder)
    # 10:05's job for "lost" ran out of retries; the others still wait.
    set_job(b.dsn, due(b, lost)["id"], "failed")
    async with bound_org(b.engine, b.w.org) as conn:
        assert await sweep_org(conn, b.w.org, now=now) == 1
    async with bound_org(b.engine, b.w.org) as conn:
        assert await sweep_org(conn, b.w.org, now=now) == 0
    states = {
        r["id"]: (r["state"], r["error"], r["finished_at"])
        for sid in (stuck, fresh, waited)
        for r in runs(b, sid)
    }
    assert states[dead] == ("timed_out", "abandoned", now)
    assert states[alive] == ("running", None, None)
    assert states[old] == ("skipped", "abandoned", now)
    assert states[young] == ("queued", None, None)
    job = due(b, lost)
    assert (job["queueing_lock"], job["scheduled_at"]) == (f"sch:{lost}:20260929T1005Z", at(5))
    b.clock.now = at(30, 5)
    assert await fire(b, job) == "succeeded"
    assert schedule(b, lost)["next_run_at"] == at(35)


async def test_a_stopped_app_skips_instants_and_manual_runs(b: Bench) -> None:
    sid = await armed(b)
    run_now(b, b.w.prod, sid)
    # Disabled, and the kill switch has not reached its pause step yet.
    execute(b.dsn, b.w.org, "update ssc.app set status = 'disabled' where id = %s", b.w.app)
    b.clock.now = at(5)
    assert await fire(b, due(b, sid)) == "skipped"
    assert await fire(b, manual(b, sid)) == "skipped"
    assert cast("FakeScheduleDispatcher", b.dispatcher).calls == []
    assert {(r["trigger"], r["state"], r["error"]) for r in runs(b, sid)} == {
        ("schedule", "skipped", "app_inactive"),
        ("manual", "skipped", "app_inactive"),
    }
    s = schedule(b, sid)
    assert (s["state"], s["next_run_at"]) == ("active", at(10))  # skipped, not paused
    assert_problem(post(b, url(b, b.w.prod, sid, "/runs")), ErrorCode.APP_NOT_ACTIVE)


# ── the kill switch (B5) through the real port ───────────────────────────────


class Confirming:
    """A snapshot port whose every version the cell confirms at once."""

    def __init__(self) -> None:
        self.version = 0

    async def request(self, conn: AsyncConnection, org_id: str) -> int:
        self.version += 1
        return self.version

    async def confirmed(self, org_id: str, version: int) -> bool:
        return True


def kill_jobs(dsn: str, app_id: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "select id, args from procrastinate.procrastinate_jobs where task_name = %s "
            "and queueing_lock = %s and status = 'todo' order by id",
            (lifecycle_tasks.RUN_KILL_SWITCH, lifecycle_tasks.queueing_lock(app_id)),
        ).fetchall()


async def pull(b: Bench, mode: str) -> str:
    """Pull the kill switch through the API and run its jobs to the end with the real port."""
    r = post(b, f"/v1/apps/{b.w.app}/kill-switch", body={"mode": mode})
    assert r.status_code == 202, r.text
    ports = Ports(
        engine=b.engine,
        runtime_driver=FakeRuntimeDriver(),
        snapshot=Confirming(),
        timers=b.timers,
    )
    for _ in range(30):
        waiting = kill_jobs(b.dsn, b.w.app)
        if not waiting:
            return str(r.json()["run_id"])
        (job,) = waiting
        set_job(b.dsn, job["id"], "doing")
        args = job["args"]
        await kill_switch.run(
            ports, org_id=args["org_id"], run_id=args["run_id"], env_id=args["env_id"], timings=FAST
        )
        set_job(b.dsn, job["id"], "succeeded")
    raise AssertionError("the kill switch never finished")


def kill_run(b: Bench, run_id: str) -> dict[str, Any]:
    (row,) = rows_of(
        b.dsn,
        b.w.org,
        "select state, paused_schedule_ids, resumed_at from ssc.kill_switch_run where id = %s",
        run_id,
    )
    return row


@pytest.mark.parametrize(
    ("mode", "reason"), [("disable", "app_disabled"), ("quarantine", "app_quarantined")]
)
async def test_the_kill_switch_pauses_and_enable_resumes(b: Bench, mode: str, reason: str) -> None:
    assert isinstance(cast("FastAPI", b.client.app).state.runtime.timers, Timers)
    await sync(b, b.w.prod, declared(), declared("held"))
    await sync(b, b.w.preview, declared())
    rows = live(b, b.w.prod)
    tick, held = str(rows["tick"]["id"]), str(rows["held"]["id"])
    preview = str(live(b, b.w.preview)["tick"]["id"])
    assert post(b, url(b, b.w.prod, held, "/pause")).status_code == 200
    stale = due(b, tick)

    run_id = await pull(b, mode)
    kil = kill_run(b, run_id)
    assert (kil["state"], list(kil["paused_schedule_ids"]), kil["resumed_at"]) == (
        "completed",
        [tick],
        None,
    )
    s = schedule(b, tick)
    assert (s["state"], s["pause_reason"], s["next_run_at"]) == ("paused", reason, None)
    assert actions(b, tick)[-1] == ("schedule.paused", "user", b.w.admin)
    assert schedule(b, held)["pause_reason"] == "manual"
    assert schedule(b, preview)["pause_reason"] == "preview"
    b.clock.now = at(5)
    assert await fire(b, stale) == "stale"
    assert runs(b, tick) == []
    assert_problem(post(b, url(b, b.w.prod, tick, "/resume")), ErrorCode.SCHEDULE_CANNOT_RESUME)
    assert_problem(post(b, url(b, b.w.prod, tick, "/runs")), ErrorCode.APP_NOT_ACTIVE)
    # No deploy reaches a stopped app; a sync that raced one keeps the kill switch's pause.
    await sync(b, b.w.prod, declared(), declared("held"))
    assert schedule(b, tick)["pause_reason"] == reason

    before = datetime.now(UTC)
    r = post(b, f"/v1/apps/{b.w.app}/enable")
    assert r.status_code == 200, r.text
    s = schedule(b, tick)
    assert (s["state"], s["pause_reason"], s["declared_by_user_id"]) == (
        "active",
        None,
        b.w.builder,
    )
    assert before < s["next_run_at"] <= before + timedelta(minutes=5)
    assert len(jobs(b.dsn, tick)) == 1
    assert actions(b, tick)[-1] == ("schedule.resumed", "user", b.w.admin)
    assert schedule(b, held)["pause_reason"] == "manual"
    assert schedule(b, preview)["pause_reason"] == "preview"
    assert kill_run(b, run_id)["resumed_at"] is not None


async def test_enable_holds_a_schedule_that_lost_authority_meanwhile(b: Bench) -> None:
    sid = await armed(b)
    await pull(b, "disable")
    execute(
        b.dsn,
        b.w.org,
        "delete from ssc.app_grant where environment_id = %s and user_id = %s",
        b.w.prod,
        b.w.builder,
    )
    assert post(b, f"/v1/apps/{b.w.app}/enable").status_code == 200
    s = schedule(b, sid)
    assert (s["state"], s["pause_reason"]) == ("paused", "builder_access_revoked")
    assert actions(b, sid)[-1] == ("schedule.paused", "user", b.w.admin)
    b.clock.now = at(5)
    assert await fire(b, due(b, sid)) == "stale"  # the instant it was armed for before the pull
    assert jobs(b.dsn, sid) == []


# ── authority: the owner and the declarer ────────────────────────────────────


async def test_a_deactivated_owner_pauses_the_apps_schedules(b: Bench) -> None:
    sid = await armed(b)
    stale = due(b, sid)
    await sync_person(b, owner_entry("deactivated"))
    s = schedule(b, sid)
    assert (s["state"], s["pause_reason"], s["next_run_at"]) == (
        "paused",
        "owner_deactivated",
        None,
    )
    assert actions(b, sid)[-1] == ("schedule.paused", "schedule", sid)
    b.clock.now = at(5)
    assert await fire(b, stale) == "stale"
    assert_problem(post(b, url(b, b.w.prod, sid, "/resume")), ErrorCode.SCHEDULE_CANNOT_RESUME)
    # A manual run is taken, then skipped: the owner is not active.
    run_now(b, b.w.prod, sid)
    assert await fire(b, manual(b, sid)) == "skipped"
    assert [(r["state"], r["error"]) for r in runs(b, sid)] == [("skipped", "owner_inactive")]
    # Back in the directory, nothing resumes by itself; a builder resumes it.
    await sync_person(b, owner_entry("active"))
    assert schedule(b, sid)["pause_reason"] == "owner_deactivated"
    r = post(b, url(b, b.w.prod, sid, "/resume"), b.t.builder)
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["declared_by_user_id"]) == ("active", b.w.builder)


async def test_the_runner_and_sweep_catch_what_hooks_missed(b: Bench) -> None:
    await sync(b, b.w.prod, declared(), declared("other"))
    rows = live(b, b.w.prod)
    tick, other = str(rows["tick"]["id"]), str(rows["other"]["id"])
    execute(
        b.dsn,
        b.w.org,
        "update ssc.user_account set status = 'deactivated', deactivated_at = now() where id = %s",
        b.w.owner,
    )
    b.clock.now = at(5)
    assert await fire(b, due(b, tick)) == "skipped"
    s = schedule(b, tick)
    assert (s["state"], s["pause_reason"], s["next_run_at"]) == (
        "paused",
        "owner_deactivated",
        None,
    )
    assert jobs(b.dsn, tick) == []
    assert [(r["state"], r["error"]) for r in runs(b, tick)] == [("skipped", "owner_inactive")]
    assert actions(b, tick)[-1] == ("schedule.paused", "schedule", tick)
    assert schedule(b, other)["state"] == "active"
    async with bound_org(b.engine, b.w.org) as conn:
        assert await sweep_org(conn, b.w.org, now=at(5)) == 0
    assert schedule(b, other)["pause_reason"] == "owner_deactivated"
    assert actions(b, other)[-1] == ("schedule.paused", "schedule", other)


async def test_a_revoked_builder_pauses_what_they_declared(b: Bench) -> None:
    sid = await armed(b)
    grants = f"/v1/apps/{b.w.app}/environments/{b.w.prod}/grants"
    etag = get(b, grants).headers["etag"]
    r = b.client.put(grants, json={"grants": []}, headers=auth(b.t.admin, **{"If-Match": etag}))
    assert r.status_code == 200, r.text
    s = schedule(b, sid)
    assert (s["state"], s["pause_reason"]) == ("paused", "builder_access_revoked")
    assert actions(b, sid)[-1] == ("schedule.paused", "schedule", sid)
    assert_problem(post(b, url(b, b.w.prod, sid, "/resume"), b.t.builder), ErrorCode.FORBIDDEN)
    # An admin's deploy declares it again, on the admin's authority.
    await sync(b, b.w.prod, declared(), by=b.w.admin)
    s = schedule(b, sid)
    assert (s["state"], s["declared_by_user_id"]) == ("active", b.w.admin)
    assert actions(b, sid)[-1] == ("schedule.resumed", "user", b.w.admin)


async def test_the_runner_skips_runs_whose_declarer_or_requester_lost_access(b: Bench) -> None:
    sid = await armed(b)
    run_now(b, b.w.prod, sid)
    execute(
        b.dsn,
        b.w.org,
        "delete from ssc.app_grant where environment_id = %s and user_id = %s",
        b.w.prod,
        b.w.builder,
    )
    b.clock.now = at(5)
    assert await fire(b, due(b, sid)) == "skipped"
    assert await fire(b, manual(b, sid)) == "skipped"
    assert {(r["trigger"], r["error"]) for r in runs(b, sid)} == {
        ("schedule", "builder_access_revoked"),
        ("manual", "builder_access_revoked"),
    }
    assert schedule(b, sid)["pause_reason"] == "builder_access_revoked"


async def test_directory_changes_pause_what_lost_authority(b: Bench) -> None:
    async with bound_org(b.engine, b.w.org) as conn:
        group = await directory.upsert_group(
            conn, b.w.org, directory_ref="dir-builders", display_name="Builders", actor=OPERATOR
        )
    person = owner_entry()
    grouped = await sync_person(b, replace(person, subject="grouped"))
    boss_entry = replace(person, subject="boss", role="admin")
    boss = await sync_person(b, boss_entry)
    execute(
        b.dsn,
        b.w.org,
        "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, group_id, "
        "granted_by_user_id) values (%s, %s, %s, 'builder', 'group', %s, %s)",
        new_id("gnt"),
        b.w.org,
        b.w.prod,
        group.group_id,
        b.w.admin,
    )
    async with bound_org(b.engine, b.w.org) as conn:
        await directory.set_group_members(conn, b.w.org, group.group_id, [grouped], actor=OPERATOR)
    await sync(b, b.w.prod, declared(), by=grouped)
    sid = str(live(b, b.w.prod)["tick"]["id"])
    assert schedule(b, sid)["state"] == "active"
    # Out of the group: no longer a builder.
    async with bound_org(b.engine, b.w.org) as conn:
        await directory.set_group_members(conn, b.w.org, group.group_id, [], actor=OPERATOR)
    assert schedule(b, sid)["pause_reason"] == "builder_access_revoked"
    # An admin with no grant redeploys; demoted, they are no longer a builder either.
    await sync(b, b.w.prod, declared(), by=boss)
    assert (schedule(b, sid)["state"], schedule(b, sid)["declared_by_user_id"]) == ("active", boss)
    await sync_person(b, replace(boss_entry, role="member"))
    assert schedule(b, sid)["pause_reason"] == "builder_access_revoked"
    assert actions(b, sid)[-1] == ("schedule.paused", "schedule", sid)


async def test_an_owner_transfer_is_caught_by_the_sweep(b: Bench) -> None:
    # The owner deployed it on their own authority; once the app changes hands they have none.
    await sync(b, b.w.prod, declared(), by=b.w.owner)
    sid = str(live(b, b.w.prod)["tick"]["id"])
    r = b.client.put(
        f"/v1/apps/{b.w.app}/owner", json={"user_id": b.w.builder}, headers=auth(b.t.admin)
    )
    assert r.status_code == 200, r.text
    assert schedule(b, sid)["state"] == "active"
    async with bound_org(b.engine, b.w.org) as conn:
        await sweep_org(conn, b.w.org, now=at(3))
    assert schedule(b, sid)["pause_reason"] == "builder_access_revoked"


async def test_may_build_is_the_require_builder_rule(b: Bench) -> None:
    w = b.w
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, w.org)
        grouped, viewer = add_account(conn, w.org, "member"), add_account(conn, w.org, "member")
        gone = new_id("usr")
        conn.execute(
            "insert into ssc.user_account (id, org_id, display_name, email, role, status, "
            "deactivated_at) values (%s, %s, 'Gone', 'gone@example.com', 'admin', 'deactivated', "
            "now())",
            (gone, w.org),
        )
        group = new_id("grp")
        conn.execute(
            "insert into ssc.user_group (id, org_id, directory_ref, display_name) "
            "values (%s, %s, 'dir-g', 'G')",
            (group, w.org),
        )
        conn.execute(
            "insert into ssc.group_member (org_id, group_id, user_id) values (%s, %s, %s)",
            (w.org, group, grouped),
        )
        grant = (
            "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, user_id, "
            "group_id, granted_by_user_id) values (%s, %s, %s, %s, %s, %s, %s, %s)"
        )
        conn.execute(
            grant, (new_id("gnt"), w.org, w.prod, "builder", "group", None, group, w.admin)
        )
        conn.execute(grant, (new_id("gnt"), w.org, w.prod, "user", "user", viewer, None, w.admin))
        conn.execute(
            grant, (new_id("gnt"), w.org, w.preview, "builder", "org", None, None, w.admin)
        )
    people = {
        "admin": w.admin,
        "owner": w.owner,
        "builder": w.builder,
        "member": w.member,
        "grouped": grouped,
        "viewer": viewer,
        "gone": gone,
    }
    ours = text(
        f"select {may_build('u')} from ssc.environment e "
        "join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
        "join ssc.user_account u on u.org_id = e.org_id and u.id = :id "
        "where e.org_id = :org and e.id = :env"
    )
    allowed: set[tuple[str, str]] = set()
    async with bound_org(b.engine, w.org) as conn:
        for env_name, env in (("prod", w.prod), ("preview", w.preview)):
            for who, uid in people.items():
                params = {"org": w.org, "id": uid, "env": env}
                theirs = (await conn.execute(_SELECT_BUILDER, params)).first() is not None
                mine = (await conn.execute(ours, params)).scalar_one()
                assert mine is theirs, (env_name, who)
                if mine:
                    allowed.add((env_name, who))
    assert allowed == {
        *(("prod", who) for who in ("admin", "owner", "builder", "grouped")),
        *(("preview", who) for who in people if who != "gone"),  # an org-wide builder grant
    }


# ── the API ──────────────────────────────────────────────────────────────────


async def test_the_api_is_for_builders_and_finds_only_what_is_there(b: Bench) -> None:
    sid = await armed(b)
    listing = url(b, b.w.prod)
    for path in (listing, url(b, b.w.prod, sid), url(b, b.w.prod, sid, "/runs")):
        assert_problem(get(b, path, b.t.member), ErrorCode.FORBIDDEN)
    for suffix in ("/runs", "/pause", "/resume"):
        assert_problem(post(b, url(b, b.w.prod, sid, suffix), b.t.member), ErrorCode.FORBIDDEN)
    for missing in (
        f"/v1/apps/{b.w.app}/environments/{new_id('env')}/schedules",
        f"/v1/apps/{new_id('app')}/environments/{b.w.prod}/schedules",
        url(b, b.w.prod, new_id("sch")),
        url(b, b.w.preview, sid),  # another environment's
        url(b, b.w.prod, sid, f"/runs/{new_id('tmr')}"),
    ):
        assert_problem(get(b, missing), ErrorCode.NOT_FOUND)
    assert_problem(post(b, url(b, b.w.prod, new_id("sch"), "/runs")), ErrorCode.NOT_FOUND)
    body = get(b, listing, b.t.owner).json()
    assert body["environment_id"] == b.w.prod
    (item,) = body["items"]
    assert datetime.fromisoformat(item.pop("next_run_at")) == at(5)
    assert item == {
        "schedule_id": sid,
        "environment_id": b.w.prod,
        "name": "tick",
        "cron": "*/5 * * * *",
        "timezone": "UTC",
        "path": "/tasks/tick",
        "method": "POST",
        "timeout_seconds": 60,
        "state": "active",
        "pause_reason": None,
        "declared_by_user_id": b.w.builder,
        "last_run": None,
    }

    # A deploy that declares none deletes it; its queued manual run is skipped.
    queued = run_now(b, b.w.prod, sid)
    await sync(b, b.w.prod)
    assert get(b, listing).json()["items"] == []
    gone = get(b, url(b, b.w.prod, sid)).json()
    assert (gone["state"], gone["next_run_at"], gone["pause_reason"]) == ("deleted", None, None)
    for suffix in ("/runs", "/pause", "/resume"):
        assert_problem(post(b, url(b, b.w.prod, sid, suffix)), ErrorCode.SCHEDULE_DELETED)
    assert await fire(b, manual(b, sid)) == "skipped"
    b.clock.now = at(5)
    assert await fire(b, due(b, sid)) == "stale"
    (run,) = get(b, url(b, b.w.prod, sid, "/runs")).json()["items"]
    assert (run["run_id"], run["state"], run["error"]) == (queued, "skipped", "deleted")
    assert gone["last_run"] is not None and gone["last_run"]["run_id"] == queued


async def test_runs_page_newest_first(b: Bench) -> None:
    sid = await armed(b)
    for minute in (5, 10, 15, 20, 25):
        b.clock.now = at(minute)
        assert await fire(b, due(b, sid)) == "succeeded"
    pages: list[list[int]] = []
    before: str | None = None
    while True:
        query = "?limit=2" + (f"&before={before}" if before else "")
        page = get(b, url(b, b.w.prod, sid, "/runs") + query, b.t.builder).json()
        pages.append([datetime.fromisoformat(r["scheduled_for"]).minute for r in page["items"]])
        before = page["next_before"]
        if before is None:
            break
    assert pages == [[25, 20], [15, 10], [5]]
    (item,) = get(b, url(b, b.w.prod)).json()["items"]
    assert datetime.fromisoformat(item["last_run"]["scheduled_for"]) == at(25)
    for query in (f"?before={new_id('tmr')}", "?before=nope", "?limit=0", "?limit=101"):
        assert_problem(get(b, url(b, b.w.prod, sid, "/runs") + query), ErrorCode.VALIDATION_FAILED)


async def test_another_org_sees_none_of_it(b: Bench, dsns: Dsns, signing_key: SigningKey) -> None:
    sid = await armed(b)
    run_id = run_now(b, b.w.prod, sid)
    other = await make_world(dsns.app)
    theirs = token_for(signing_key, other.org, other.admin)
    for path in (url(b, b.w.prod), url(b, b.w.prod, sid), url(b, b.w.prod, sid, f"/runs/{run_id}")):
        assert_problem(get(b, path, theirs), ErrorCode.NOT_FOUND)
    assert_problem(post(b, url(b, b.w.prod, sid, "/runs"), theirs), ErrorCode.NOT_FOUND)
    count = "select count(*) as n from ssc.timer_run where id = %s"
    assert rows_of(dsns.app, other.org, count, run_id) == [{"n": 0}]
    assert rows_of(dsns.app, b.w.org, count, run_id) == [{"n": 1}]


# ── the dispatcher and the worker ────────────────────────────────────────────


async def test_the_fake_dispatcher_answers_from_its_script() -> None:
    call = TimerCall(
        org_id="org_x",
        environment_id="env_x",
        schedule_id="sch_x",
        run_id="tmr_x",
        method="GET",
        path="/x",
    )
    fake = FakeScheduleDispatcher([Scripted(status=201), Scripted(delay=5)])
    assert await fake.dispatch(call) == DispatchResult(http_status=201)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await fake.dispatch(call)
    assert (fake.calls, fake.answered, fake.cancelled) == ([call, call], [call], [call])
    with pytest.raises(ValueError, match="at least one"):
        FakeScheduleDispatcher([])


def test_the_worker_registers_the_timer_tasks_and_composes_the_ports() -> None:
    app = build_app("postgresql://ssc_app@localhost/ssc")
    assert {tasks.RUN, "timers:sweep"} <= set(app.tasks)
    ((periodic,),) = [
        [p for key, p in app.periodic_registry.periodic_tasks.items() if key[0] == "timers:sweep"]
    ]
    first = periodic.croniter.get_next(float, start_time=1_790_000_000.0)
    assert periodic.croniter.get_next(float, start_time=first) - first == 60
    assert timers_jobs.SWEEP_CRON == "* * * * *"
    base = {"SSC_DATABASE_DSN": "postgresql://ssc_app@localhost/ssc"}
    ports = compose_ports(base)
    assert isinstance(ports.timers, Timers) and ports.timer_dispatcher is None
    faked = compose_ports({**base, "SSC_TIMER_DISPATCHER": "fake", "SSC_ENV": "test"})
    assert isinstance(faked.timer_dispatcher, FakeScheduleDispatcher)
    with pytest.raises(CompositionError, match="fake timer_dispatcher"):
        compose_ports({**base, "SSC_TIMER_DISPATCHER": "fake"})
    with pytest.raises(CompositionError, match="fake timer_dispatcher"):
        refuse_fakes(faked, {"SSC_ENV": "prod"})
    with pytest.raises(CompositionError, match="unknown"):
        timer_dispatcher_from_env({"SSC_TIMER_DISPATCHER": "https"})


def fresh_db(dsns: Dsns) -> Dsns:
    """A new database in the session's container, migrated to head: the sweep sees every org."""
    name = f"t{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")

    def on(dsn: str) -> str:
        return make_url(dsn).set(database=name).render_as_string(hide_password=False)

    d = Dsns(on(dsns.superuser), on(dsns.migrate), on(dsns.app))
    upgrade(d.migrate)
    return d


async def test_a_worker_runs_a_manual_run_and_the_sweep_recovers_a_lost_instant(
    dsns: Dsns,
) -> None:
    db = await asyncio.to_thread(fresh_db, dsns)
    w = await make_world(db.app)
    engine = make_engine(db.app)
    now = datetime.now(UTC)
    fake = FakeScheduleDispatcher()
    builder = Actor(ActorKind.USER, w.builder)
    try:
        async with bound_org(engine, w.org) as conn:
            await Timers(lambda: now - timedelta(minutes=10)).sync_schedules(
                conn,
                org_id=w.org,
                environment_id=w.prod,
                declared=[declared()],
                declared_by_user_id=w.builder,
                actor=builder,
            )
        ((sid, lost_at),) = [
            (r["id"], r["next_run_at"])
            for r in rows_of(db.app, w.org, "select id, next_run_at from ssc.schedule")
        ]
        assert lost_at < now - timedelta(minutes=4)
        (job,) = jobs(db.app, sid)
        set_job(db.app, job["id"], "failed")  # the instant's job ran out of retries
        async with bound_org(engine, w.org) as conn:
            run_id = await service.request_run(
                conn,
                org_id=w.org,
                environment_id=w.prod,
                schedule_id=sid,
                user_id=w.builder,
                actor=builder,
                now=now,
            )
        app = App(connector=PsycopgConnector(conninfo=queue_conninfo(db.app)))
        app.add_tasks_from(timers_jobs.blueprint(), namespace="timers")
        ports = Ports(engine=engine, timer_dispatcher=fake, metrics=metrics_port(MASTER))
        async with app.open_async():
            await app.configure_task("timers:sweep").defer_async(timestamp=int(now.timestamp()))
            await app.run_worker_async(
                additional_context={PORTS_KEY: ports}, wait=False, install_signal_handlers=False
            )
    finally:
        await engine.dispose()
    done = rows_of(db.app, w.org, "select id, trigger, state, scheduled_for from ssc.timer_run")
    assert {(r["trigger"], r["state"]) for r in done} == {
        ("manual", "succeeded"),
        ("schedule", "succeeded"),
    }
    assert next(r["id"] for r in done if r["trigger"] == "manual") == run_id
    assert next(r["scheduled_for"] for r in done if r["trigger"] == "schedule") == lost_at
    assert len(fake.calls) == 2
    ((armed_at,),) = [
        tuple(r.values()) for r in rows_of(db.app, w.org, "select next_run_at from ssc.schedule")
    ]
    assert armed_at > now


# ── revision 0013 ────────────────────────────────────────────────────────────


def shape(dsn: str) -> tuple[int, bool, list[bool], set[str]]:
    with psycopg.connect(dsn) as conn:
        (named,) = conn.execute(
            "select count(*) from pg_constraint "
            "where conname = 'schedule_org_id_environment_id_name_key'"
        ).fetchone() or (0,)
        (runs_table,) = conn.execute(
            "select to_regclass('ssc.timer_run') is not null"
        ).fetchone() or (False,)
        forced = [
            bool(f)
            for (f,) in conn.execute(
                "select relforcerowsecurity from pg_class where oid in "
                "('ssc.schedule'::regclass, 'ssc.user_account'::regclass)"
            ).fetchall()
        ]
        columns = {
            c
            for (c,) in conn.execute(
                "select column_name from information_schema.columns "
                "where table_schema = 'ssc' and table_name = 'schedule'"
            ).fetchall()
        }
    return int(named), bool(runs_table), forced, columns


def test_revision_0013_round_trips(dsns: Dsns) -> None:
    name = f"r{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database=name).render_as_string(hide_password=False)
    added = {
        "path",
        "method",
        "timeout_seconds",
        "pause_reason",
        "next_run_at",
        "last_scheduled_for",
        "declared_by_user_id",
    }
    upgrade(dsn, "0012_kill_switch")
    named, runs_table, forced, columns = shape(dsn)
    assert (named, runs_table, forced) == (1, False, [True, True])
    assert columns.isdisjoint(added)
    upgrade(dsn, "0013_timers")
    named, runs_table, forced, columns = shape(dsn)
    assert (named, runs_table, forced) == (0, True, [True, True])
    assert added <= columns
    # Down empties the table, so up finds it empty again even after a name was declared twice.
    in_db = {"database": name}
    su = make_url(dsns.superuser).set(**in_db).render_as_string(hide_password=False)
    org = make_org(make_url(dsns.app).set(**in_db).render_as_string(hide_password=False))
    app_id, env_id = new_id("app"), new_id("env")
    with psycopg.connect(su) as conn:
        conn.execute(
            "insert into ssc.app (id, org_id, slug, owner_user_id) values (%s, %s, 'ledger', %s)",
            (app_id, org.org_id, org.admin_user_id),
        )
        conn.execute(
            "insert into ssc.environment (id, org_id, app_id, name) values (%s, %s, %s, 'prod')",
            (env_id, org.org_id, app_id),
        )
        for state, reason in (("deleted", None), ("deleted", None), ("paused", "manual")):
            conn.execute(
                "insert into ssc.schedule (id, org_id, environment_id, name, cron, path, state, "
                "pause_reason, declared_by_user_id) "
                "values (%s, %s, %s, 'nightly', '0 2 * * *', '/tick', %s, %s, %s)",
                (new_id("sch"), org.org_id, env_id, state, reason, org.admin_user_id),
            )
    downgrade(dsn, "0012_kill_switch")
    assert shape(dsn)[:3] == (1, False, [True, True])
    with psycopg.connect(su) as conn:
        assert conn.execute("select count(*) from ssc.schedule").fetchall() == [(0,)]
    assert shape(dsn)[3].isdisjoint(added)
    upgrade(dsn)
    assert shape(dsn)[:3] == (0, True, [True, True])
