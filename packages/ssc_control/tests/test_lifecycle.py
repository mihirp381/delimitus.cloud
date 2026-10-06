"""SSC-025 (B5): the admin's inventory, the kill switch, enable and owner transfer, against the
fake runtime driver, spy snapshot and timers ports, and postgres:18; the end-to-end kills add
the real snapshot port, a published snapshot and the gateway's gate and stream relay.

Ticket "done when" checks:
  * inventory lists an app within 1 s  -> test_inventory_lists_a_new_app_within_a_second
  * inventory fields and paging        -> test_inventory_shows_owner_releases_sharing_and_last_use,
                                          test_inventory_pages_by_slug
  * saga order, step audits, timings   -> test_the_saga_runs_in_order_and_times_each_step
  * the time each drill takes          -> test_the_saga_runs_in_order_and_times_each_step
  * deny comes first                   -> test_the_deny_is_committed_before_any_step
  * end to end, awake gateway: denial -> test_kill_end_to_end_with_an_awake_gateway
    within 10 s, open stream cut, full
    stop within 60 s (fakes)
  * end to end, gateway and app at zero
                                       -> test_kill_end_to_end_with_the_gateway_and_app_at_zero
  * each step confirmed by the next    -> both above, test_a_deny_no_pointer_names_is_unconfirmed,
    snapshot version, no heartbeat        test_the_pointer_confirms_with_no_heartbeat (test_access)
  * full stop within 60 s (fakes)      -> test_the_saga_runs_in_order_and_times_each_step
  * a failed scale is retried          -> test_a_failed_scale_is_retried_and_completes
  * the reconciler keeps it stopped    -> test_the_reconciler_keeps_a_stopped_app_down
  * quarantine freezes sharing         -> test_quarantine_freezes_sharing
  * no deploy or build when stopped    -> test_a_stopped_app_takes_no_deploy_or_build
  * enable                             -> test_enable_resumes_paused_timers_once,
                                          test_kill_switch_refusals
  * transfer                           -> test_owner_transfer
Plus: refusals, the gateway's poll and deadline, final failures, a real snapshot version and
heartbeat, stale and concurrent jobs, the sweep and a real worker.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from ssc_testkit import ISSUER, Dsns, SigningKey, assert_problem, auth, mint, new_key

from ssc_contracts.audit import ActorKind
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_contracts.manifest import default_manifest
from ssc_control.api import Settings, create_app
from ssc_control.api.dberrors import classify
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.audit import Actor
from ssc_control.db import (
    MIGRATE_ROLE,
    NewOrg,
    bind_org_sync,
    bound_org,
    create_org,
    make_engine,
    upgrade,
)
from ssc_control.deploy.build_driver import FakeBuildDriver
from ssc_control.deploy.builds import run_build
from ssc_control.deploy.deployments import APP_NOT_ACTIVE, HealthWait, run_deployment
from ssc_control.deploy.gates import approvals_prod_gate
from ssc_control.lifecycle import kill_switch, tasks
from ssc_control.lifecycle.kill_switch import Timings
from ssc_control.metrics import metrics_port
from ssc_control.ports import KillReason, NullTimersPort
from ssc_control.runtime.driver import RuntimeDriverError, service_name
from ssc_control.runtime.fake import FakeRuntimeDriver, changed
from ssc_control.runtime.reconciler import reconcile_env
from ssc_control.runtime.specs import ReleaseSpec, StaticReleaseSpecs
from ssc_control.snapshot.compiler import point_latest, publish
from ssc_control.snapshot.service import Snapshots, record_ack
from ssc_control.worker import WorkerSettings, build_app, run_worker
from ssc_control.worker_ports import Ports
from ssc_edge.gate import STREAM_HEADER, Allow, Deny, Facts, Gate, GateConfig
from ssc_edge.keys import new_keyring, parse_keyring
from ssc_edge.server import RECHECK_SECONDS, OnDemandView, gate_for
from ssc_edge.session import Session, SessionCodec, new_sid
from ssc_edge.streams import WATCH_SECONDS, Streams
from ssc_shared.access import ViewHolder
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.canonical import manifest_digest
from ssc_shared.clock import SystemClock
from ssc_shared.snapshot_feed import SnapshotFeed

MASTER = bytes(range(32))
IMAGE = "sha256:" + "a" * 64
FAST = Timings(
    confirm_within=timedelta(seconds=5), confirm_every=timedelta(0), backoff=timedelta(0)
)
POLL = replace(FAST, confirm_every=timedelta(milliseconds=20))
HEALTH = HealthWait(within=2.0, every=0.01)
PAUSED = ("sch_" + "a" * 20, "sch_" + "b" * 20)
STEP_NAMES = ["gateway_deny", "datagw_suspend", "egress_remove", "scale_to_zero", "pause_timers"]

# ── spies ────────────────────────────────────────────────────────────────────

Event = tuple[Any, ...]


class SpySnapshot:
    """``request`` hands out the next version; ``confirmed`` answers from ``answers``, then
    ``default``. Both log to the shared ``events``."""

    def __init__(self, events: list[Event], *, default: bool = True) -> None:
        self.events = events
        self.default = default
        self.answers: list[bool] = []
        self.version = 40

    async def request(self, conn: AsyncConnection, org_id: str) -> int:
        self.version += 1
        self.events.append(("request", self.version))
        return self.version

    async def confirmed(self, org_id: str, version: int) -> bool:
        self.events.append(("confirmed", version))
        return self.answers.pop(0) if self.answers else self.default


class SpyTimers(NullTimersPort):
    def __init__(self, events: list[Event]) -> None:
        self.events = events
        self.paused: tuple[str, ...] = PAUSED

    async def pause_for_kill(
        self, conn: AsyncConnection, *, org_id: str, app_id: str, reason: KillReason, actor: Actor
    ) -> list[str]:
        self.events.append(("pause_for_kill", app_id, reason, actor.id))
        return list(self.paused)

    async def resume_after_kill(  # noqa: PLR0913
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        app_id: str,
        schedule_ids: Sequence[str],
        reason: KillReason,
        actor: Actor,
    ) -> None:
        self.events.append(("resume_after_kill", app_id, tuple(schedule_ids), reason, actor.id))


class Recording(FakeRuntimeDriver):
    """The fake driver, logging ``scale_to_zero`` to the shared ``events``. ``stubborn`` services
    take the call and keep serving."""

    def __init__(self, events: list[Event]) -> None:
        super().__init__()
        self.events = events
        self.stubborn: set[str] = set()

    async def scale_to_zero(self, service: str) -> None:
        self.events.append(("scale_to_zero", service))
        if service in self.stubborn:
            self.calls.append(("scale_to_zero", service))
            return
        await super().scale_to_zero(service)


# ── world ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class World:
    org: str
    admin: str  # the org's first admin and the app's owner
    member: str  # an active member with no grant
    builder: str  # an active member with a builder grant on prod and preview
    app: str
    prod: str
    preview: str


@dataclass(frozen=True)
class Tokens:
    admin: str
    member: str
    builder: str


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
        spec = NewOrg("Lifecycle", "Ada Admin", "ada@example.com", ISSUER, new_id("usr"))
        created = await create_org(engine, spec)
    finally:
        await engine.dispose()
    org, admin = created.org_id, created.admin_user_id
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        member, builder = add_account(conn, org, "member"), add_account(conn, org, "member")
        app, prod, preview = new_id("app"), new_id("env"), new_id("env")
        conn.execute(
            "insert into ssc.app (id, org_id, slug, owner_user_id) values (%s, %s, 'ledger', %s)",
            (app, org, admin),
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
    return World(org, admin, member, builder, app, prod, preview)


@dataclass
class Bench:
    client: TestClient
    w: World
    t: Tokens
    dsn: str
    events: list[Event]
    driver: Recording
    snapshot: SpySnapshot
    timers: SpyTimers
    specs: StaticReleaseSpecs
    ports: Ports


@pytest.fixture
async def world(dsns: Dsns) -> World:
    return await make_world(dsns.app)


@pytest.fixture
def tokens(world: World, signing_key: SigningKey) -> Tokens:
    def token(sub: str) -> str:
        return mint(signing_key, org=world.org, sub=sub, jti=f"cred_{new_key()[:16]}")

    return Tokens(
        admin=token(world.admin), member=token(world.member), builder=token(world.builder)
    )


@pytest.fixture
async def b(
    dsns: Dsns, signing_key: SigningKey, world: World, tokens: Tokens
) -> AsyncIterator[Bench]:
    settings = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
        metrics_key=MASTER,
    )
    engine: AsyncEngine = make_engine(dsns.app)
    events: list[Event] = []
    driver, snapshot, timers = Recording(events), SpySnapshot(events), SpyTimers(events)
    specs = StaticReleaseSpecs()
    ports = Ports(
        engine=engine,
        runtime_driver=driver,
        release_specs=specs,
        snapshot=snapshot,
        timers=timers,
        prod_gate=approvals_prod_gate(),
        build_driver=FakeBuildDriver(),
        metrics=metrics_port(MASTER),
    )
    with TestClient(create_app(settings)) as client:
        app = cast("FastAPI", client.app)
        app.state.runtime = replace(app.state.runtime, timers=timers)
        yield Bench(client, world, tokens, dsns.app, events, driver, snapshot, timers, specs, ports)
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


def post(b: Bench, path: str, body: dict[str, Any], token: str | None = None) -> Response:
    headers = auth(token or b.t.admin, **{IDEMPOTENCY_HEADER: new_key()})
    return b.client.post(path, json=body, headers=headers)


def get(b: Bench, path: str, token: str | None = None) -> Response:
    return b.client.get(path, headers=auth(token or b.t.admin))


def pull(b: Bench, mode: str = "disable", token: str | None = None) -> Response:
    return post(b, f"/v1/apps/{b.w.app}/kill-switch", {"mode": mode}, token)


def pulled(b: Bench, mode: str = "disable") -> str:
    r = pull(b, mode)
    assert r.status_code == 202, r.text
    return str(r.json()["run_id"])


def enable(b: Bench, token: str | None = None) -> Response:
    return post(b, f"/v1/apps/{b.w.app}/enable", {}, token)


def run_of(b: Bench, run_id: str) -> dict[str, Any]:
    r = get(b, f"/v1/apps/{b.w.app}/kill-switch/{run_id}")
    assert r.status_code == 200, r.text
    return cast("dict[str, Any]", r.json())


def app_status(b: Bench) -> str:
    (row,) = rows_of(b.dsn, b.w.org, "select status from ssc.app where id = %s", b.w.app)
    return str(row["status"])


def audit_of(b: Bench, target: str) -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select action, actor_kind, actor_id, before, after from ssc.audit_event "
        "where target_id = %s order by seq",
        target,
    )


def kill_jobs(dsn: str, app_id: str, status: str | None = None) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "select id, status, lock, queueing_lock, args from procrastinate.procrastinate_jobs "
            "where task_name = %s and queueing_lock = %s "
            "and (%s::text is null or status::text = %s) order by id",
            (tasks.RUN_KILL_SWITCH, tasks.queueing_lock(app_id), status, status),
        ).fetchall()


def set_job(dsn: str, job_id: int, status: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("set search_path to procrastinate")  # its triggers name types unqualified
        conn.execute("update procrastinate_jobs set status = %s where id = %s", (status, job_id))


@dataclass(frozen=True)
class Ran:
    lock: str | None
    env_id: str | None
    outcome: str


async def drain(
    b: Bench, ports: Ports | None = None, *, timings: Timings = FAST, most: int | None = None
) -> list[Ran]:
    """Run the app's waiting kill-switch jobs one at a time, as a worker would (``doing``
    first, so a job can defer its successor), until none is left or ``most`` have run."""
    ran: list[Ran] = []
    while most is None or len(ran) < most:
        waiting = kill_jobs(b.dsn, b.w.app, "todo")
        if not waiting:
            return ran
        assert len(waiting) == 1, waiting
        (job,) = waiting
        assert len(ran) < 60, ran
        set_job(b.dsn, job["id"], "doing")
        args = job["args"]
        outcome = await kill_switch.run(
            ports or b.ports,
            org_id=args["org_id"],
            run_id=args["run_id"],
            env_id=args["env_id"],
            timings=timings,
        )
        set_job(b.dsn, job["id"], "succeeded")
        ran.append(Ran(job["lock"], args["env_id"], outcome))
    return ran


async def drain_until_gateway_waits(
    b: Bench,
    ports: Ports,
    run_id: str,
    then: Callable[[], Awaitable[None]],
    *,
    timings: Timings = POLL,
) -> list[Ran]:
    """``drain``, in a task: the gateway's job polls within itself for the version, so ``then``
    (publishing it) runs once the step is waiting, while the job is still in its poll."""
    task = asyncio.create_task(drain(b, ports, timings=timings))
    try:
        for _ in range(500):
            steps = run_of(b, run_id)["steps"]
            if steps and steps[0]["state"] == "running" and steps[0]["snapshot_version"]:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the gateway step never began")
        await then()
        return await asyncio.wait_for(task, 30)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def seed_live(b: Bench) -> dict[str, str]:
    """A healthy deployment of release R1 (prod) and R2 (preview); the release per environment."""
    releases: dict[str, str] = {}
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        for number, env in enumerate((b.w.prod, b.w.preview), start=1):
            rel, dep = new_id("rel"), new_id("dep")
            conn.execute(
                "insert into ssc.release (id, org_id, app_id, number, image_digest, "
                "manifest_digest, source_digest, actor_kind, actor_id) "
                "values (%s, %s, %s, %s, %s, %s, %s, 'user', %s)",
                (
                    rel,
                    b.w.org,
                    b.w.app,
                    number,
                    IMAGE,
                    "sha256:" + "1" * 64,
                    "sha256:" + "2" * 64,
                    b.w.builder,
                ),
            )
            conn.execute(
                "insert into ssc.deployment (id, org_id, app_id, environment_id, release_id, "
                "kind, state, config_version, grants_version, actor_kind, actor_id, finished_at) "
                "values (%s, %s, %s, %s, %s, 'deploy', 'healthy', 1, 1, 'user', %s, now())",
                (dep, b.w.org, b.w.app, env, rel, b.w.builder),
            )
            conn.execute(
                "update ssc.environment set current_deployment_id = %s where id = %s", (dep, env)
            )
            releases[env] = rel
            b.specs.put(rel, ReleaseSpec(manifest=default_manifest()))
    return releases


async def reconcile(b: Bench) -> None:
    for env in (b.w.prod, b.w.preview):
        await reconcile_env(b.ports.engine, b.driver, b.specs, org_id=b.w.org, env_id=env)


async def bring_up(b: Bench) -> None:
    """Both environments serving on the fake runtime; the spies' logs cleared."""
    seed_live(b)
    for _ in range(3):
        await reconcile(b)
    for env in (b.w.prod, b.w.preview):
        assert not b.driver.services[service_name(env)].stopped
    b.driver.reset_calls()
    b.events.clear()


def stopped(b: Bench, env: str) -> bool:
    return b.driver.services[service_name(env)].stopped


# ── inventory ────────────────────────────────────────────────────────────────


def inventory(b: Bench, token: str | None = None, **params: object) -> Response:
    return b.client.get("/v1/inventory", params=params, headers=auth(token or b.t.admin))


async def test_inventory_lists_a_new_app_within_a_second(b: Bench) -> None:
    made = post(b, "/v1/apps", {"slug": "fresh"})
    assert made.status_code == 201, made.text
    created = time.monotonic()
    r = inventory(b)
    elapsed = time.monotonic() - created
    assert r.status_code == 200, r.text
    (item,) = [i for i in r.json()["items"] if i["slug"] == "fresh"]
    assert elapsed < 1.0
    assert item["app_id"] == made.json()["id"]
    assert item["owner"] == {"user_id": b.w.admin, "display_name": "Ada Admin"}
    assert item["status"] == "active" and item["last_used_at"] is None
    assert {e["name"] for e in item["environments"]} == {
        e["name"] for e in made.json()["environments"]
    }
    assert_problem(inventory(b, b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(inventory(b, b.t.builder), ErrorCode.FORBIDDEN)


async def test_inventory_shows_owner_releases_sharing_and_last_use(b: Bench) -> None:
    releases = seed_live(b)
    group, failed = new_id("grp"), new_id("dep")
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        # A later deployment failed: it is the last deploy, R1 stays current.
        conn.execute(
            "insert into ssc.deployment (id, org_id, app_id, environment_id, release_id, kind, "
            "state, failure_code, config_version, grants_version, actor_kind, actor_id, "
            "started_at, finished_at) values (%s, %s, %s, %s, %s, 'deploy', 'failed', "
            "'HEALTH_CHECK_FAILED', 1, 1, 'user', %s, now() + interval '1 minute', "
            "now() + interval '2 minutes')",
            (failed, b.w.org, b.w.app, b.w.prod, releases[b.w.prod], b.w.builder),
        )
        conn.execute(
            "insert into ssc.user_group (id, org_id, directory_ref, display_name) "
            "values (%s, %s, %s, 'Finance')",
            (group, b.w.org, f"ref-{group}"),
        )
        for kind, subject in (("org", None), ("group", group)):
            conn.execute(
                "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, "
                "group_id, granted_by_user_id) values (%s, %s, %s, 'user', %s, %s, %s)",
                (new_id("gnt"), b.w.org, b.w.prod, kind, subject, b.w.admin),
            )
        for kind, at in (
            ("app_opened", "2026-09-20T10:00:00+00:00"),
            ("app_opened", "2026-09-18T10:00:00+00:00"),
            ("deploy", "2026-09-25T10:00:00+00:00"),  # not a use
        ):
            conn.execute(
                "insert into ssc.metrics_event (org_id, at, kind, app_id) values (%s, %s, %s, %s)",
                (b.w.org, at, kind, b.w.app),
            )
    r = inventory(b)
    assert r.status_code == 200, r.text
    (item,) = r.json()["items"]
    assert item["slug"] == "ledger" and item["app_id"] == b.w.app
    assert item["owner"] == {"user_id": b.w.admin, "display_name": "Ada Admin"}
    assert datetime.fromisoformat(item["last_used_at"]) == datetime(2026, 9, 20, 10, tzinfo=UTC)
    prod, preview = sorted(item["environments"], key=lambda e: e["name"] != "prod")
    assert prod["environment_id"] == b.w.prod
    assert prod["current_release"] == {"release_id": releases[b.w.prod], "number": 1}
    assert prod["last_deploy"]["operation_id"] == failed
    assert (prod["last_deploy"]["kind"], prod["last_deploy"]["state"]) == ("deploy", "failed")
    assert prod["sharing"] == {"org_wide": True, "users": 1, "groups": 1}
    assert preview["current_release"] == {"release_id": releases[b.w.preview], "number": 2}
    assert preview["last_deploy"]["state"] == "healthy"
    assert preview["sharing"] == {"org_wide": False, "users": 1, "groups": 0}


async def test_inventory_pages_by_slug(b: Bench) -> None:
    for slug in ("alpha", "zulu"):
        execute(
            b.dsn,
            b.w.org,
            "insert into ssc.app (id, org_id, slug, owner_user_id) values (%s, %s, %s, %s)",
            new_id("app"),
            b.w.org,
            slug,
            b.w.builder,
        )
    first = inventory(b, limit=2).json()
    assert [i["slug"] for i in first["items"]] == ["alpha", "ledger"]
    assert first["next_cursor"] == "ledger"
    assert first["items"][0]["environments"] == []
    second = inventory(b, limit=2, cursor=first["next_cursor"]).json()
    assert [i["slug"] for i in second["items"]] == ["zulu"]
    assert second["next_cursor"] is None
    assert [i["slug"] for i in inventory(b).json()["items"]] == ["alpha", "ledger", "zulu"]
    assert_problem(inventory(b, limit=101), ErrorCode.VALIDATION_FAILED)
    assert_problem(inventory(b, cursor="Not A Slug"), ErrorCode.VALIDATION_FAILED)


# ── the kill switch ──────────────────────────────────────────────────────────


async def test_the_deny_is_committed_before_any_step(b: Bench) -> None:
    await bring_up(b)
    r = pull(b)
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    assert r.json() == {"run_id": run_id, "state": "running"}
    assert r.headers["Location"] == f"/v1/apps/{b.w.app}/kill-switch/{run_id}"
    # Committed and answered with no step begun and nothing asked of any port.
    assert app_status(b) == "disabled"
    got = run_of(b, run_id)
    assert (got["state"], got["steps"], got["finished_at"], got["total_ms"]) == (
        "running",
        [],
        None,
        None,
    )
    assert (got["mode"], got["app_id"]) == ("disable", b.w.app)
    assert b.events == [] and b.driver.calls == []
    (job,) = kill_jobs(b.dsn, b.w.app)
    assert (job["status"], job["lock"]) == ("todo", None)
    assert job["args"] == {"org_id": b.w.org, "run_id": run_id, "env_id": None}
    compiles = rows_of(
        b.dsn,
        b.w.org,
        "select count(*) as n from procrastinate.procrastinate_jobs "
        "where queueing_lock = %s and status = 'todo'",
        f"snapshot:{b.w.org}",
    )
    assert compiles == [{"n": 1}]  # the snapshot is marked dirty in the same transaction
    (audit,) = audit_of(b, b.w.app)
    assert (audit["action"], audit["actor_id"]) == ("app.disabled", b.w.admin)
    assert (audit["before"], audit["after"]) == ({"status": "active"}, {"status": "disabled"})


async def test_the_saga_runs_in_order_and_times_each_step(b: Bench) -> None:
    await bring_up(b)
    run_id = pulled(b)
    ran = await drain(b)
    assert [(r.lock, r.outcome) for r in ran] == [
        (None, "running"),
        (f"env:{b.w.prod}", "running"),
        (f"env:{b.w.preview}", "completed"),
    ]
    v = b.snapshot.version
    assert b.events == [
        ("request", v),
        ("confirmed", v),
        ("confirmed", v),
        ("confirmed", v),
        ("scale_to_zero", service_name(b.w.prod)),
        ("scale_to_zero", service_name(b.w.preview)),
        ("pause_for_kill", b.w.app, "disable", b.w.admin),
    ]
    assert stopped(b, b.w.prod) and stopped(b, b.w.preview)
    got = run_of(b, run_id)
    assert got["state"] == "completed"
    assert [s["name"] for s in got["steps"]] == STEP_NAMES
    assert all(s["state"] == "done" and s["attempts"] == 1 for s in got["steps"])
    assert all(s["error"] is None for s in got["steps"])
    assert [s["snapshot_version"] for s in got["steps"]] == [v, v, v, None, None]
    for s in got["steps"]:
        assert isinstance(s["elapsed_ms"], int) and s["elapsed_ms"] >= 0
        assert s["finished_at"] is not None
    starts = [datetime.fromisoformat(s["started_at"]) for s in got["steps"]]
    assert starts == sorted(starts)
    assert isinstance(got["total_ms"], int) and 0 <= got["total_ms"] < 60_000
    audits = audit_of(b, run_id)
    assert [a["action"] for a in audits] == ["kill_switch.step"] * 5
    assert [a["after"]["step"] for a in audits] == STEP_NAMES
    assert all(a["actor_id"] == b.w.admin and a["actor_kind"] == "user" for a in audits)
    assert audits[0]["after"] == {
        "app_id": b.w.app,
        "mode": "disable",
        "step": "gateway_deny",
        "state": "done",
        "snapshot_version": v,
        "elapsed_ms": got["steps"][0]["elapsed_ms"],
        "since_command_ms": audits[0]["after"]["since_command_ms"],
        "attempts": 1,
    }
    since = [a["after"]["since_command_ms"] for a in audits]
    assert all(isinstance(ms, int) for ms in since) and since == sorted(since)
    assert since[0] >= got["steps"][0]["elapsed_ms"]
    assert since[-1] == got["total_ms"]
    (paused,) = rows_of(
        b.dsn, b.w.org, "select paused_schedule_ids from ssc.kill_switch_run where id = %s", run_id
    )
    assert paused["paused_schedule_ids"] == list(PAUSED)
    # A rerun of the finished run changes nothing.
    b.events.clear()
    b.driver.reset_calls()
    assert await kill_switch.run(b.ports, org_id=b.w.org, run_id=run_id, timings=FAST) == (
        "completed"
    )
    assert b.events == [] and b.driver.calls == []
    assert len(audit_of(b, run_id)) == 5


async def test_a_failed_scale_is_retried_and_completes(b: Bench) -> None:
    await bring_up(b)
    b.driver.fail_next("scale_to_zero", RuntimeDriverError("runtime said no"))
    run_id = pulled(b)
    ran = await drain(b)
    # The retry holds the same environment's lock.
    assert [r.lock for r in ran] == [
        None,
        f"env:{b.w.prod}",
        f"env:{b.w.prod}",
        f"env:{b.w.preview}",
    ]
    got = run_of(b, run_id)
    assert got["state"] == "completed"
    scale = got["steps"][3]
    assert (scale["state"], scale["attempts"], scale["error"]) == ("done", 2, "RUNTIME_ERROR")
    assert stopped(b, b.w.prod) and stopped(b, b.w.preview)
    assert [a["after"]["state"] for a in audit_of(b, run_id)] == ["done"] * 5


async def test_a_step_fails_after_five_tries_and_the_rest_still_run(b: Bench) -> None:
    await bring_up(b)
    b.driver.stubborn.add(service_name(b.w.prod))
    run_id = pulled(b)
    ran = await drain(b)
    assert [r.lock for r in ran] == [None] + [f"env:{b.w.prod}"] * 5
    assert ran[-1].outcome == "failed"
    got = run_of(b, run_id)
    assert got["state"] == "failed" and got["finished_at"] is not None
    assert [s["state"] for s in got["steps"]] == ["done", "done", "done", "failed", "done"]
    scale = got["steps"][3]
    assert (scale["attempts"], scale["error"]) == (5, "NOT_STOPPED")
    assert b.events[-1] == ("pause_for_kill", b.w.app, "disable", b.w.admin)
    step_audit = audit_of(b, run_id)[3]["after"]
    assert (step_audit["state"], step_audit["error"], step_audit["attempts"]) == (
        "failed",
        "NOT_STOPPED",
        5,
    )
    # The deny holds either way, and the reconciler keeps trying to stop the app.
    assert app_status(b) == "disabled"
    b.driver.stubborn.clear()
    await reconcile(b)
    assert stopped(b, b.w.prod)


async def test_no_runtime_fails_the_scale_at_once(b: Bench) -> None:
    run_id = pulled(b)
    ran = await drain(b, replace(b.ports, runtime_driver=None))
    assert [(r.lock, r.outcome) for r in ran] == [(None, "failed")]
    got = run_of(b, run_id)
    scale = got["steps"][3]
    assert (scale["state"], scale["attempts"], scale["error"]) == (
        "failed",
        1,
        "RUNTIME_UNAVAILABLE",
    )
    assert got["steps"][4]["state"] == "done"


async def test_the_gateway_polls_within_its_job_until_its_version_is_confirmed(b: Bench) -> None:
    b.snapshot.answers = [False, False]
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    run_id = pulled(b)
    timings = replace(FAST, confirm_every=timedelta(milliseconds=250), sleep=sleep)
    ran = await drain(b, timings=timings)
    assert [r.lock for r in ran] == [None, f"env:{b.w.prod}", f"env:{b.w.preview}"]  # no poll job
    assert waits == [0.25, 0.25]
    v = b.snapshot.version
    assert b.events[:4] == [("request", v)] + [("confirmed", v)] * 3
    gateway = run_of(b, run_id)["steps"][0]
    assert (gateway["state"], gateway["snapshot_version"], gateway["attempts"]) == ("done", v, 1)


async def test_the_gateway_waits_for_confirm_by_and_no_transaction_is_open_meanwhile(
    b: Bench,
) -> None:
    b.snapshot.default = False
    open_while_waiting: list[int] = []

    async def sleep(seconds: float) -> None:
        open_while_waiting.append(
            rows_of(
                b.dsn,
                b.w.org,
                "select count(*) as n from pg_stat_activity where datname = current_database() "
                "and pid <> pg_backend_pid() and state like 'idle in transaction%%'",
            )[0]["n"]
        )
        await asyncio.sleep(seconds)

    timings = replace(
        FAST,
        confirm_within=timedelta(milliseconds=400),
        confirm_every=timedelta(milliseconds=50),
        sleep=sleep,
    )
    run_id = pulled(b)
    ran = await drain(b, timings=timings)
    assert [r.lock for r in ran][0] is None and len(ran) == 3  # still no poll job
    assert len(open_while_waiting) >= 3
    assert open_while_waiting == [0] * len(open_while_waiting)
    gateway = run_of(b, run_id)["steps"][0]
    assert (gateway["state"], gateway["attempts"]) == ("unconfirmed", 1)
    finished = datetime.fromisoformat(gateway["finished_at"])
    late = finished - datetime.fromisoformat(gateway["started_at"]) - timings.confirm_within
    assert timedelta(0) <= late < timedelta(seconds=1)  # recorded at confirm_by, not before
    assert len([e for e in b.events if e[0] == "confirmed"]) >= 3 + 2  # the gateway's, then two


async def test_an_unconfirmed_deny_is_recorded_and_the_run_goes_on(b: Bench) -> None:
    b.snapshot.default = False
    run_id = pulled(b)
    await drain(b, timings=replace(FAST, confirm_within=timedelta(0)))
    got = run_of(b, run_id)
    assert got["state"] == "completed"
    assert [s["state"] for s in got["steps"]] == [
        "unconfirmed",
        "unconfirmed",
        "unconfirmed",
        "done",
        "done",
    ]


async def test_the_deny_rides_the_next_snapshot_version(b: Bench, tmp_path: Path) -> None:
    ports = replace(b.ports, snapshot=Snapshots(b.ports.engine))
    patient = replace(POLL, confirm_within=timedelta(seconds=60))
    run_id = pulled(b)
    published: list[int] = []

    async def acknowledge() -> None:
        gateway = run_of(b, run_id)["steps"][0]
        assert gateway["state"] == "running"
        version = gateway["snapshot_version"]
        blob = fs_blob(tmp_path)
        async with bound_org(b.ports.engine, b.w.org) as conn:
            assert await publish(conn, b.w.org, blob, at=datetime.now(UTC)) == version
        published.append(version)
        (doc_row,) = rows_of(
            b.dsn,
            b.w.org,
            "select object_key from ssc.access_snapshot where org_id = %s and version = %s",
            b.w.org,
            version,
        )
        doc = json.loads(b"".join([c async for c in blob.get(doc_row["object_key"])]))
        assert {e["status"] for e in doc["environments"].values()} == {"disabled"}
        (org,) = rows_of(b.dsn, b.w.org, "select cell_label from ssc.org where id = %s", b.w.org)
        async with bound_org(b.ports.engine, b.w.org) as conn:
            await record_ack(conn, b.w.org, cell_label=org["cell_label"], version=version)

    await drain_until_gateway_waits(b, ports, run_id, acknowledge, timings=patient)
    got = run_of(b, run_id)
    assert [s["state"] for s in got["steps"][:3]] == ["done", "done", "done"]
    assert got["steps"][0]["snapshot_version"] == published[0]


def fs_blob(root: Path) -> FsBlobStore:
    clock = SystemClock()
    signer = UrlSigner({"k1": secrets.token_bytes(32)}, active="k1", clock=clock)
    return FsBlobStore(root, signer=signer, base_url="http://blobs.test/v1/blobs/", clock=clock)


async def compile_and_point(b: Bench, blob: FsBlobStore) -> int:
    """What the worker's compile does: publish the next version, then move ``latest.json``."""
    async with bound_org(b.ports.engine, b.w.org) as conn:
        version = await publish(conn, b.w.org, blob, at=datetime.now(UTC))
    await point_latest(b.ports.engine, b.w.org, blob)
    return version


def pointer_ports(b: Bench, blob: FsBlobStore) -> Ports:
    """The bench's ports with the real ``Snapshots``, confirming from ``blob``'s pointer."""
    return replace(b.ports, blob_store=blob, snapshot=Snapshots(b.ports.engine, blob_store=blob))


@dataclass
class Edge:
    """The cell's gateway on ``blob`` and the builder's WebSocket request to prod.
    ``clock`` is the snapshot feed's monotonic clock."""

    view: OnDemandView
    gate: Gate
    facts: Facts
    clock: list[float]


async def gateway_starts(b: Bench, blob: FsBlobStore) -> Edge:
    """A gateway starting from zero: it reads ``latest.json`` before its first check."""
    (org,) = rows_of(b.dsn, b.w.org, "select cell_label from ssc.org where id = %s", b.w.org)
    label = str(org["cell_label"])
    clock = [1000.0]
    holder = ViewHolder(b.w.org)
    view = OnDemandView(
        SnapshotFeed(blob, holder, monotonic=lambda: clock[0]), holder, max_stale=300
    )
    assert await view.first_read()
    keyring = parse_keyring(new_keyring())
    config = GateConfig(
        org_id=b.w.org,
        cell_label=label,
        apps_domain="apps.test",
        auth_url="https://auth.example.test",
        issuer=f"https://keys.example.test/{label}",
        project_number="123456789012",
        region="us-central1",
        max_body_bytes=1024,
    )
    gate = gate_for(config, keyring, view=view.view, refresh=view.refresh)
    host, now = f"ledger.{label}.apps.test", int(time.time())
    who = Session(
        sid=new_sid(), sub=b.w.builder, org=b.w.org, name="Bo", email="bo@example.com",
        iat=now - 60, exp=now + 3600,
    )  # fmt: skip
    sealed = SessionCodec(keyring.session, active=keyring.session_kid).seal(who, host)
    headers = {
        "cookie": f"__Host-ssc-session={sealed}",
        "upgrade": "websocket",
        "connection": "upgrade",
        "origin": f"https://{host}",
    }
    return Edge(view, gate, Facts(method="GET", host=host, path="/ws", headers=headers), clock)


async def echo_app(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.readuntil(b"\r\n\r\n")
    writer.write(b"HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\n\r\n")
    while chunk := await reader.read(1024):
        writer.write(chunk)
    writer.close()


PROD_TIMINGS = replace(POLL, confirm_within=kill_switch.TIMINGS.confirm_within)


async def test_kill_end_to_end_with_an_awake_gateway(b: Bench, tmp_path: Path) -> None:
    await bring_up(b)
    blob = fs_blob(tmp_path)
    ports = pointer_ports(b, blob)
    await compile_and_point(b, blob)
    edge = await gateway_starts(b, blob)
    allowed = await edge.gate.check(edge.facts)
    assert isinstance(allowed, Allow) and allowed.user == b.w.builder
    app_server = await asyncio.start_server(echo_app, "127.0.0.1", 0)
    app_port = app_server.sockets[0].getsockname()[1]

    async def dial(_: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", app_port)

    streams = Streams(lambda: edge.gate, dial=dial, refresh=edge.view.refresh)
    relay = await streams.serve("127.0.0.1", 0)
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", relay.sockets[0].getsockname()[1]
        )
        writer.write(
            f"GET /ws HTTP/1.1\r\nhost: {allowed.upstream}\r\n"
            f"{STREAM_HEADER}: {streams.admit(allowed)}\r\n\r\n".encode()
        )
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 101 ")
        writer.write(b"ping")
        assert await reader.readexactly(4) == b"ping"

        commanded = time.monotonic()
        run_id = pulled(b)
        gateway: dict[str, Any] = {}
        pointed = asyncio.Event()

        async def point() -> None:
            gateway.update(run_of(b, run_id)["steps"][0])
            assert gateway["state"] == "running"
            assert await compile_and_point(b, blob) == gateway["snapshot_version"]
            pointed.set()

        task = asyncio.create_task(
            drain_until_gateway_waits(b, ports, run_id, point, timings=PROD_TIMINGS)
        )
        await asyncio.wait_for(pointed.wait(), 30)
        edge.clock[0] += RECHECK_SECONDS + 0.1
        refused = await edge.gate.check(edge.facts)
        denied = time.monotonic() - commanded
        assert isinstance(refused, Deny) and refused.reason == "not_granted"
        cut = time.monotonic()
        assert await asyncio.wait_for(reader.read(), WATCH_SECONDS + 2) == b""
        assert time.monotonic() - cut <= WATCH_SECONDS + 0.5
        assert not streams.open
        drained = await asyncio.wait_for(task, 30)
        assert drained[0].lock is None  # one job took the gateway step: no poll job followed
        full_stop = time.monotonic() - commanded
    finally:
        await streams.aclose()
        relay.close()
        app_server.close()
        await edge.view.aclose()
    got = run_of(b, run_id)
    assert got["state"] == "completed"
    assert [s["state"] for s in got["steps"]] == ["done"] * 5
    assert got["steps"][0]["snapshot_version"] == gateway["snapshot_version"]
    assert [e for e in b.events if e[0] == "scale_to_zero"] == [
        ("scale_to_zero", service_name(b.w.prod)),
        ("scale_to_zero", service_name(b.w.preview)),
    ]
    assert stopped(b, b.w.prod) and stopped(b, b.w.preview)
    assert denied < 10.0 and full_stop < 60.0
    assert got["total_ms"] < 60_000


async def test_kill_end_to_end_with_the_gateway_and_app_at_zero(b: Bench, tmp_path: Path) -> None:
    await bring_up(b)
    blob = fs_blob(tmp_path)
    ports = pointer_ports(b, blob)
    await compile_and_point(b, blob)
    commanded = time.monotonic()
    run_id = pulled(b)

    async def point() -> None:
        await compile_and_point(b, blob)

    await drain_until_gateway_waits(b, ports, run_id, point, timings=PROD_TIMINGS)
    full_stop = time.monotonic() - commanded
    got = run_of(b, run_id)
    assert [s["state"] for s in got["steps"]] == ["done"] * 5
    assert stopped(b, b.w.prod) and stopped(b, b.w.preview)
    assert full_stop < 60.0
    edge = await gateway_starts(b, blob)
    try:
        refused = await edge.gate.check(edge.facts)
    finally:
        await edge.view.aclose()
    assert isinstance(refused, Deny) and refused.reason == "not_granted"
    assert refused.status == 404


async def test_a_deny_no_pointer_names_is_unconfirmed(b: Bench, tmp_path: Path) -> None:
    blob = fs_blob(tmp_path)
    await compile_and_point(b, blob)
    run_id = pulled(b)
    await drain(b, pointer_ports(b, blob), timings=replace(FAST, confirm_within=timedelta(0)))
    got = run_of(b, run_id)
    assert [s["state"] for s in got["steps"][:3]] == ["unconfirmed"] * 3
    assert got["state"] == "completed" and app_status(b) == "disabled"


# ── what a stopped app refuses ───────────────────────────────────────────────


async def test_the_reconciler_keeps_a_stopped_app_down(b: Bench) -> None:
    await bring_up(b)
    pulled(b)
    await reconcile(b)  # before any step runs: the committed deny is enough
    assert stopped(b, b.w.prod) and stopped(b, b.w.preview)
    await drain(b)
    b.driver.reset_calls()
    for _ in range(3):
        await reconcile(b)
    for env in (b.w.prod, b.w.preview):
        assert changed(b.driver.calls, service_name(env)) == []
        assert stopped(b, env)
    assert enable(b).status_code == 200
    await reconcile(b)
    assert not stopped(b, b.w.prod) and not stopped(b, b.w.preview)


def put_grants(
    b: Bench, env: str, grants: list[dict[str, Any]], version: int, token: str | None = None
) -> Response:
    return b.client.put(
        f"/v1/apps/{b.w.app}/environments/{env}/grants",
        json={"grants": grants},
        headers=auth(token or b.t.admin, **{"If-Match": f'"{version}"'}),
    )


async def test_quarantine_freezes_sharing(b: Bench) -> None:
    grants = [
        {"role": "builder", "subject_kind": "user", "subject_id": b.w.builder},
        {"role": "user", "subject_kind": "user", "subject_id": b.w.member},
    ]
    pulled(b, "disable")
    await drain(b)
    r = put_grants(b, b.w.prod, grants, 1)
    assert r.status_code == 200, r.text  # a disabled app's sharing can still change
    assert r.json()["grants_version"] == 2

    run_id = pulled(b, "quarantine")  # disabled -> quarantined escalates
    escalated = audit_of(b, b.w.app)[-1]
    assert escalated["action"] == "app.quarantined"
    assert (escalated["before"], escalated["after"]) == (
        {"status": "disabled"},
        {"status": "quarantined"},
    )
    await drain(b)
    got = run_of(b, run_id)
    assert (got["mode"], got["state"]) == ("quarantine", "completed")
    assert app_status(b) == "quarantined"
    for token in (b.t.admin, b.t.builder):
        assert_problem(put_grants(b, b.w.prod, grants[:1], 2, token), ErrorCode.APP_NOT_ACTIVE)
    assert_problem(put_grants(b, b.w.preview, [], 1), ErrorCode.APP_NOT_ACTIVE)
    count = "select count(*) as n from ssc.app_grant where environment_id = %s"
    assert rows_of(b.dsn, b.w.org, count, b.w.prod) == [{"n": 2}]
    listed = get(b, f"/v1/apps/{b.w.app}/environments/{b.w.prod}/grants")
    assert listed.status_code == 200 and len(listed.json()["grants"]) == 2
    assert_problem(pull(b, "quarantine"), ErrorCode.APP_NOT_ACTIVE)
    assert_problem(pull(b, "disable"), ErrorCode.APP_NOT_ACTIVE)


def seed_bundle(b: Bench) -> str:
    """A bundle row as ``complete`` leaves it."""
    bid = new_id("bdl")
    m = default_manifest()
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        conn.execute(
            "insert into ssc.bundle (id, org_id, app_id, digest, size_bytes, actor_kind, "
            "actor_id) values (%s, %s, %s, %s, 10, 'user', %s)",
            (
                bid,
                b.w.org,
                b.w.app,
                "sha256:" + hashlib.sha256(bid.encode()).hexdigest(),
                b.w.builder,
            ),
        )
        conn.execute(
            "update ssc.bundle set state = 'stored', manifest = %s::jsonb, manifest_digest = %s, "
            "file_count = 1, stored_at = now() where id = %s",
            (json.dumps(m.model_dump(mode="json", by_alias=True)), manifest_digest(m), bid),
        )
    return bid


def start_build(b: Bench, env: str) -> Response:
    path = f"/v1/apps/{b.w.app}/environments/{env}/builds"
    return post(b, path, {"bundle_id": seed_bundle(b)}, b.t.builder)


def start_deploy(b: Bench, env: str, release: str) -> Response:
    path = f"/v1/apps/{b.w.app}/environments/{env}/deployments"
    return post(b, path, {"release_id": release, "kind": "deploy"}, b.t.builder)


async def test_a_stopped_app_takes_no_deploy_or_build(b: Bench) -> None:
    built = start_build(b, b.w.preview)
    assert built.status_code == 202, built.text
    build = built.json()["build_id"]
    assert await run_build(b.ports, org_id=b.w.org, build_id=build) == "succeeded"
    (row,) = rows_of(b.dsn, b.w.org, "select release_id from ssc.build where id = %s", build)
    release = str(row["release_id"])
    pending = start_deploy(b, b.w.preview, release)
    assert pending.status_code == 202, pending.text
    op = pending.json()["operation_id"]

    pulled(b, "disable")
    await drain(b)
    b.driver.reset_calls()
    # Posted before the switch: the job refuses and never calls the runtime.
    assert await run_deployment(b.ports, org_id=b.w.org, deployment_id=op, health=HEALTH) == (
        "failed"
    )
    (dep,) = rows_of(b.dsn, b.w.org, "select failure_code from ssc.deployment where id = %s", op)
    assert dep["failure_code"] == APP_NOT_ACTIVE
    assert b.driver.calls == []
    for mode in ("disable", "quarantine"):
        if mode == "quarantine":
            pulled(b, "quarantine")
            await drain(b)
        assert app_status(b) == kill_switch.MODE_STATUS[mode]
        assert_problem(start_deploy(b, b.w.preview, release), ErrorCode.APP_NOT_ACTIVE)
        assert_problem(start_build(b, b.w.preview), ErrorCode.APP_NOT_ACTIVE)


# ── refusals ─────────────────────────────────────────────────────────────────


async def test_kill_switch_refusals(b: Bench) -> None:
    for token in (b.t.member, b.t.builder):
        assert_problem(pull(b, token=token), ErrorCode.FORBIDDEN)
        assert_problem(enable(b, token), ErrorCode.FORBIDDEN)
    assert app_status(b) == "active"
    other = new_id("app")
    assert_problem(
        post(b, f"/v1/apps/{other}/kill-switch", {"mode": "disable"}), ErrorCode.NOT_FOUND
    )
    assert_problem(post(b, f"/v1/apps/{other}/enable", {}), ErrorCode.NOT_FOUND)
    assert_problem(pull(b, "stop"), ErrorCode.VALIDATION_FAILED)
    assert_problem(enable(b), ErrorCode.APP_ALREADY_ACTIVE)

    url = f"/v1/apps/{b.w.app}/kill-switch"
    headers = auth(b.t.admin, **{IDEMPOTENCY_HEADER: new_key()})
    first = b.client.post(url, json={"mode": "disable"}, headers=headers)
    again = b.client.post(url, json={"mode": "disable"}, headers=headers)
    assert first.status_code == again.status_code == 202, again.text
    assert again.json() == first.json()
    assert len(kill_jobs(b.dsn, b.w.app)) == 1
    run_id = first.json()["run_id"]

    assert_problem(pull(b, "quarantine"), ErrorCode.KILL_SWITCH_IN_FLIGHT)
    assert_problem(enable(b), ErrorCode.KILL_SWITCH_IN_FLIGHT)
    assert_problem(get(b, f"{url}/{new_id('kil')}"), ErrorCode.NOT_FOUND)
    assert_problem(get(b, f"/v1/apps/{other}/kill-switch/{run_id}"), ErrorCode.NOT_FOUND)
    assert_problem(get(b, f"{url}/{run_id}", b.t.member), ErrorCode.FORBIDDEN)
    # The partial unique index backs the check.
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        with pytest.raises(psycopg.Error) as e:
            conn.execute(
                "insert into ssc.kill_switch_run (id, org_id, app_id, mode, actor_kind, actor_id) "
                "values (%s, %s, %s, 'quarantine', 'user', %s)",
                (new_id("kil"), b.w.org, b.w.app, b.w.admin),
            )
    code, _ = classify(DBAPIError("insert", None, e.value))
    assert code is ErrorCode.KILL_SWITCH_IN_FLIGHT
    await drain(b)
    assert_problem(pull(b, "disable"), ErrorCode.APP_NOT_ACTIVE)


# ── enable and transfer ──────────────────────────────────────────────────────


async def test_enable_resumes_paused_timers_once(b: Bench) -> None:
    await bring_up(b)
    pulled(b)
    await drain(b)
    b.events.clear()
    r = enable(b)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["id"], out["status"]) == (b.w.app, "active")
    assert {e["id"] for e in out["environments"]} == {b.w.prod, b.w.preview}
    assert b.events == [("resume_after_kill", b.w.app, PAUSED, "disable", b.w.admin)]
    enabled = audit_of(b, b.w.app)[-1]
    assert (enabled["action"], enabled["actor_id"]) == ("app.enabled", b.w.admin)
    assert (enabled["before"], enabled["after"]) == ({"status": "disabled"}, {"status": "active"})
    assert_problem(enable(b), ErrorCode.APP_ALREADY_ACTIVE)

    # Each run's schedules are resumed once, with its own reason; none is resumed twice.
    later = ("sch_" + "c" * 20,)
    b.timers.paused = later
    pulled(b, "disable")
    await drain(b)
    b.timers.paused = ()
    pulled(b, "quarantine")
    await drain(b)
    b.events.clear()
    assert enable(b).status_code == 200
    assert b.events == [("resume_after_kill", b.w.app, later, "disable", b.w.admin)]
    runs = rows_of(
        b.dsn,
        b.w.org,
        "select resumed_at is not null as resumed from ssc.kill_switch_run where app_id = %s",
        b.w.app,
    )
    assert runs == [{"resumed": True}] * 3
    assert audit_of(b, b.w.app)[-1]["before"] == {"status": "quarantined"}


def transfer(b: Bench, user_id: str, token: str | None = None, app: str | None = None) -> Response:
    return b.client.put(
        f"/v1/apps/{app or b.w.app}/owner",
        json={"user_id": user_id},
        headers=auth(token or b.t.admin),
    )


async def test_owner_transfer(b: Bench) -> None:
    r = transfer(b, b.w.builder)
    assert r.status_code == 200, r.text
    assert (r.json()["id"], r.json()["owner_user_id"]) == (b.w.app, b.w.builder)

    def transfers() -> list[dict[str, Any]]:
        return [a for a in audit_of(b, b.w.app) if a["action"] == "app.owner_transferred"]

    (audit,) = transfers()
    assert audit["actor_id"] == b.w.admin
    assert (audit["before"], audit["after"]) == (
        {"owner_user_id": b.w.admin},
        {"owner_user_id": b.w.builder},
    )
    assert transfer(b, b.w.builder).status_code == 200  # already the owner: nothing to audit
    assert len(transfers()) == 1
    execute(
        b.dsn,
        b.w.org,
        "update ssc.user_account set status = 'deactivated', deactivated_at = now() where id = %s",
        b.w.member,
    )
    assert_problem(transfer(b, b.w.member), ErrorCode.OWNER_NOT_ACTIVE)
    assert_problem(transfer(b, new_id("usr")), ErrorCode.REFERENCE_NOT_FOUND)
    assert_problem(transfer(b, "someone"), ErrorCode.VALIDATION_FAILED)
    assert_problem(transfer(b, b.w.admin, b.t.builder), ErrorCode.FORBIDDEN)  # owner, not admin
    assert_problem(transfer(b, b.w.admin, app=new_id("app")), ErrorCode.NOT_FOUND)
    (row,) = rows_of(b.dsn, b.w.org, "select owner_user_id from ssc.app where id = %s", b.w.app)
    assert row["owner_user_id"] == b.w.builder
    assert len(transfers()) == 1


# ── jobs that race, stall or go missing ──────────────────────────────────────


async def test_stale_and_concurrent_jobs_record_each_step_once(b: Bench) -> None:
    await bring_up(b)
    first = pulled(b)
    await drain(b)
    assert enable(b).status_code == 200
    second = pulled(b)
    (job,) = kill_jobs(b.dsn, b.w.app, "todo")
    set_job(b.dsn, job["id"], "doing")
    # The worker's job, a duplicate of it and a stale job of the finished run, all at once.
    outcomes = await asyncio.gather(
        kill_switch.run(b.ports, org_id=b.w.org, run_id=second, timings=FAST),
        kill_switch.run(b.ports, org_id=b.w.org, run_id=second, timings=FAST),
        kill_switch.run(b.ports, org_id=b.w.org, run_id=first, timings=FAST),
    )
    set_job(b.dsn, job["id"], "succeeded")
    assert set(outcomes) <= {"running", "completed"}
    await drain(b)
    assert run_of(b, second)["state"] == "completed"
    assert [a["after"]["step"] for a in audit_of(b, second)] == STEP_NAMES
    assert len(audit_of(b, first)) == 5
    assert kill_jobs(b.dsn, b.w.app, "todo") == []
    assert await kill_switch.run(b.ports, org_id=b.w.org, run_id=new_id("kil")) == "missing"


def fresh_db(dsns: Dsns) -> Dsns:
    """A new database in the session's container, migrated to head: the sweep and a worker see
    every org in it."""
    name = f"k{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")

    def at(dsn: str) -> str:
        return make_url(dsn).set(database=name).render_as_string(hide_password=False)

    d = Dsns(at(dsns.superuser), at(dsns.migrate), at(dsns.app))
    upgrade(d.migrate)
    return d


async def test_the_sweep_redefers_a_run_whose_job_is_gone(dsns: Dsns) -> None:
    db = await asyncio.to_thread(fresh_db, dsns)
    w = await make_world(db.app)
    engine = make_engine(db.app)
    try:
        async with bound_org(engine, w.org) as conn:
            started = await kill_switch.start(
                conn,
                org_id=w.org,
                app_id=w.app,
                mode="disable",
                actor=Actor(ActorKind.USER, w.admin),
            )
        assert await kill_switch.sweep(engine) == 0  # its job waits
        (job,) = kill_jobs(db.app, w.app)
        set_job(db.app, job["id"], "doing")
        assert await kill_switch.sweep(engine) == 0  # its job runs
        set_job(db.app, job["id"], "failed")
        assert await kill_switch.sweep(engine) == 1
        (again,) = kill_jobs(db.app, w.app, "todo")
        assert again["lock"] is None
        assert again["args"] == {"org_id": w.org, "run_id": started.run_id, "env_id": None}
        assert await kill_switch.sweep(engine) == 0
    finally:
        await engine.dispose()


def test_the_worker_registers_the_lifecycle_tasks() -> None:
    app = build_app("postgresql://ssc_app@localhost/ssc")
    assert {tasks.RUN_KILL_SWITCH, "lifecycle:sweep"} <= set(app.tasks)
    assert any(key[0] == "lifecycle:sweep" for key in app.periodic_registry.periodic_tasks)


async def until(pred: Callable[[], bool], within: float) -> None:
    deadline = time.monotonic() + within
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.1)


async def test_a_running_worker_completes_the_kill_switch(dsns: Dsns) -> None:
    db = await asyncio.to_thread(fresh_db, dsns)
    w = await make_world(db.app)
    engine = make_engine(db.app)
    events: list[Event] = []
    settings = WorkerSettings(
        stalled_after_seconds=2.0,
        heartbeat_seconds=0.5,
        stalled_worker_timeout=2.0,
        polling_seconds=0.2,
        delete_jobs="never",
    )
    ports = Ports(
        engine=engine,
        runtime_driver=Recording(events),
        snapshot=SpySnapshot(events),
        timers=SpyTimers(events),
    )
    async with bound_org(engine, w.org) as conn:
        started = await kill_switch.start(
            conn,
            org_id=w.org,
            app_id=w.app,
            mode="quarantine",
            actor=Actor(ActorKind.USER, w.admin),
        )

    def state() -> str:
        (row,) = rows_of(
            db.app, w.org, "select state from ssc.kill_switch_run where id = %s", started.run_id
        )
        return str(row["state"])

    task = asyncio.create_task(
        run_worker(
            build_app(db.app, settings=settings),
            ports,
            settings=settings,
            install_signal_handlers=False,
        )
    )
    try:
        await until(lambda: state() != "running", 30)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await engine.dispose()
    assert state() == "completed"
    assert [e[0] for e in events] == [
        "request",
        "confirmed",
        "confirmed",
        "confirmed",
        "scale_to_zero",
        "scale_to_zero",
        "pause_for_kill",
    ]
    locks = [j["lock"] for j in kill_jobs(db.app, w.app)]
    assert locks == [None, f"env:{w.prod}", f"env:{w.preview}"]
