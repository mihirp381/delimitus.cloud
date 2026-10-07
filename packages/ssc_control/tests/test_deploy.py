"""SSC-016: builds, numbered releases, deployments and rollback, against the fake build and
runtime drivers and postgres:18, and through the cell agent on the Cloud Build and Cloud Run
emulators.

Ticket "done when" checks:
  * rollback in under 30 seconds                 -> test_rollback_through_the_cell_agent_is_...,
                                                    test_a_rollback_job_goes_ahead_of_waiting_jobs
                                                    test_the_kill_switch_outranks_a_rollback
  * a failed health check leaves the old pointer -> test_a_failed_health_check_keeps_the_old_pointer
                                                    (on the emulators: test_rollback_through_...)
  * a release row cannot be edited               -> test_a_release_row_cannot_be_edited,
                                                    test_no_route_mutates_a_release
  * a cold revision has time to start            -> test_the_health_window_outlasts_cloud_runs_...
  * R numbering                                  -> test_concurrent_builds_number_releases_r1_to_r5
  * one in flight                                -> test_one_deployment_in_flight_per_environment
                                                    (and test_api's test_deployment_is_accepted_...)
  * rollback pre-empts                           -> test_a_rollback_pre_empts_a_running_deploy
  * rollback does not restore sharing            -> test_a_rollback_keeps_sharing_and_timers
  * gates before any prod boot                   -> test_a_blocked_gate_never_boots_prod,
                                                    test_a_release_without_a_manifest_never_boots,
                                                    test_the_production_gate_end_to_end
  * other refusals                               -> test_a_release_built_for_preview_is_refused_...,
                                                    test_a_disabled_app_is_refused
SSC-025 (a stopped app, decision 014):
  * a deploy stops within one poll               -> test_a_stopped_app_ends_a_deploy_within_one_poll
  * and when going live                          -> test_a_stop_while_observing_is_caught_when_...
  * a build fails when claimed                   -> test_a_build_of_a_stopped_app_fails_when_claimed
SSC-015 (the build reads the stored bundle first):
  * a Dash app is a session app without sessions -> test_a_dash_bundle_deploys_as_a_session_app
  * SQLite on disk is refused                    -> test_sqlite_on_disk_fails_the_build_before_...
  * the framework migration                      -> test_0015_downgrades_and_upgrades,
                                                    test_a_framework_must_be_a_short_lowercase_name
SSC-090 (the request deadline the gateway tells the app):
  * the snapshot carries a longer timeout only   -> test_the_snapshot_carries_only_a_longer_timeout
  * never later than the revision serving        -> test_a_billing_change_never_makes_the_....
  * an unconfirmed lower timeout keeps traffic   -> test_an_unconfirmed_lower_timeout_keeps_the_...
  * a failed deploy puts the timeout back        -> test_a_failed_session_to_request_deploy_puts_...
Plus: the build API and job, build failures and timeouts, the health timeout, going live locking
schedule rows before ``audit_head``, the deploy and first_url metrics, history and release
listings, and the worker's wiring.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlsplit

import httpx2
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from ssc_testkit import (
    ISSUER,
    Dsns,
    SigningKey,
    assert_problem,
    audit_head_is_free,
    auth,
    backend_pid,
    cells_with,
    mint,
    new_key,
    wait_for_a_lock_wait,
    with_cell,
)

import ssc_control.api
from ssc_agent.app import create_app as create_agent
from ssc_agent.cloud_build import CellBuildConfig, CloudBuildDriver
from ssc_agent.cloud_run import (
    STARTUP_FAILURES,
    STARTUP_PERIOD_SECONDS,
    CellRuntime,
    CloudRunDriver,
)
from ssc_bundle.client import prepare
from ssc_conformance.cloud_build_emulator import CloudBuildEmulator
from ssc_conformance.cloud_run_emulator import PROJECT, REGION, CloudRunEmulator
from ssc_contracts.audit import ActorKind
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_contracts.manifest import Manifest
from ssc_control import worker
from ssc_control.api import Settings, create_app
from ssc_control.api.dberrors import classify
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.approvals import service
from ssc_control.approvals.gate import ApprovalsProdGate
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
from ssc_control.deferral import KILL_SWITCH_PRIORITY, MANUAL_TIMER_PRIORITY, ROLLBACK_PRIORITY
from ssc_control.deploy import tasks
from ssc_control.deploy.build_driver import (
    BUILD_TIMED_OUT,
    BuildStatus,
    FakeBuildDriver,
    fake_image_digest,
)
from ssc_control.deploy.builds import BUILD_DRIVER_UNAVAILABLE, run_build
from ssc_control.deploy.bundles import bundle_key
from ssc_control.deploy.cell_build import CellAgentBuildDriver
from ssc_control.deploy.deployments import (
    APP_NOT_ACTIVE,
    APPROVAL_REQUIRED,
    HEALTH_CHECK_FAILED,
    HEALTH_POLL_SECONDS,
    HEALTH_TIMEOUT_SECONDS,
    RELEASE_SPEC_UNAVAILABLE,
    RUNTIME_ERROR,
    SNAPSHOT_UNCONFIRMED,
    HealthWait,
    run_deployment,
)
from ssc_control.deploy.gates import approvals_prod_gate
from ssc_control.lifecycle import kill_switch
from ssc_control.metrics import metrics_port
from ssc_control.ports import (
    DeclaredSchedule,
    GateResult,
    NullMetricsPort,
    NullSnapshotPort,
    NullTimersPort,
)
from ssc_control.runtime.cell_agent import CellAgentDriver
from ssc_control.runtime.cells import STATIC_LABEL, CellRouter, OrgCell, StaticCells
from ssc_control.runtime.driver import service_name
from ssc_control.runtime.fake import FakeRuntimeDriver
from ssc_control.runtime.specs import BundleReleaseSpecs
from ssc_control.snapshot.compiler import compile_document
from ssc_control.timers.service import Timers
from ssc_control.worker import CompositionError, Ports, build_app, compose_ports
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.canonical import manifest_digest
from ssc_shared.clock import SystemClock
from ssc_shared.runtime import REQUEST_TIMEOUT_SECONDS, SESSION_TIMEOUT_SECONDS

MASTER = bytes(range(32))
FAST = HealthWait(within=2.0, every=0.01)
FINANCE = {"connections": {"names": ["finance"]}}
FIXTURES = Path(__file__).resolve().parents[3] / "conformance" / "build_fixtures"
NIGHTLY = {"schedules": [{"name": "nightly", "cron": "0 3 * * *", "path": "/tasks/nightly"}]}
AGENT = "https://ssc-cell-agent.test"
REPO = f"{REGION}-docker.pkg.dev/{PROJECT}/ssc-apps/apps"
ROLLBACK_LIMIT_SECONDS = 30.0
CELL_BUILD = CellBuildConfig(
    project=PROJECT,
    region=REGION,
    image_repository=REPO,
    service_account=f"ssc-build@{PROJECT}.iam.gserviceaccount.com",
    tools_image=f"{REGION}-docker.pkg.dev/ssc-platform/tools/ssc-build-tools@sha256:" + "a" * 64,
    frontend_image="ghcr.io/railwayapp/railpack-frontend@sha256:" + "f" * 64,
)
CELL_RUNTIME = CellRuntime(
    project=PROJECT,
    region=REGION,
    network=f"projects/{PROJECT}/global/networks/ssc-cell",
    subnetwork=f"projects/{PROJECT}/regions/{REGION}/subnetworks/apps",
    image_repository=REPO,
    invoker=f"ssc-gateway@{PROJECT}.iam.gserviceaccount.com",
)

# ── world ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class World:
    org: str
    admin: str  # the org's first admin and the app's owner
    approver: str  # a second admin
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


class Hold:
    """A fake-driver ``sleep`` that parks the caller until ``released`` is set."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.released = asyncio.Event()

    async def __call__(self, _seconds: float) -> None:
        self.entered.set()
        await self.released.wait()


class SpyTimers(NullTimersPort):
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], str]] = []

    async def sync_schedules(
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        environment_id: str,
        declared: Sequence[DeclaredSchedule],
        declared_by_user_id: str,
        actor: Actor,
    ) -> None:
        self.calls.append((environment_id, tuple(s.name for s in declared), declared_by_user_id))


class SpyGate:
    def __init__(self, outcome: str = "waiting") -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    async def check(
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        app_id: str,
        environment_id: str,
        release_id: str,
    ) -> GateResult:
        self.calls.append(environment_id)
        return GateResult(outcome=self.outcome)  # type: ignore[arg-type]


@dataclass
class Bench:
    client: TestClient
    w: World
    t: Tokens
    dsn: str
    runtime: FakeRuntimeDriver
    hold: Hold
    builds: FakeBuildDriver
    timers: SpyTimers
    ports: Ports

    def cells_with(self, **changes: Any) -> StaticCells:
        """The bench's one cell with ``changes`` (``identity=``, ``build=`` and so on)."""
        return cells_with(self.ports, **changes)


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
        spec = NewOrg("Deploys", "Ada Admin", "ada@example.com", ISSUER, new_id("usr"))
        created = await create_org(engine, spec)
    finally:
        await engine.dispose()
    org, admin = created.org_id, created.admin_user_id
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        approver = add_account(conn, org, "admin")
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
    return World(org, admin, approver, member, builder, app, prod, preview)


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
    hold, timers = Hold(), SpyTimers()
    runtime, builds = FakeRuntimeDriver(sleep=hold), FakeBuildDriver()
    ports = Ports(
        engine=engine,
        cells=StaticCells(OrgCell(label=STATIC_LABEL, runtime=runtime, build=builds)),
        release_specs=BundleReleaseSpecs(),
        timers=timers,
        prod_gate=approvals_prod_gate(),
        metrics=metrics_port(MASTER),
    )
    with TestClient(create_app(settings)) as client:
        yield Bench(client, world, tokens, dsns.app, runtime, hold, builds, timers, ports)
    await engine.dispose()


# ── helpers ──────────────────────────────────────────────────────────────────


def manifest_of(**tables: Any) -> Manifest:
    return Manifest.model_validate({"schema": "ssc/v1", **tables})


def rows_of(dsn: str, org: str, sql: str, *args: object) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        bind_org_sync(conn, org)
        return conn.execute(sql, args).fetchall()


def execute(dsn: str, org: str, sql: str, *args: object) -> None:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        conn.execute(sql, args)


def take_job(dsn: str, queueing_lock: str) -> None:
    """What a worker does when it fetches the job: it is no longer waiting."""
    with psycopg.connect(dsn) as conn:
        # Procrastinate's triggers name its types unqualified.
        conn.execute("set search_path to procrastinate")
        conn.execute(
            "update procrastinate_jobs set status = 'doing' "
            "where queueing_lock = %s and status = 'todo'",
            (queueing_lock,),
        )


def seed_bundle(
    b: Bench, manifest: Manifest | None = None, *, stored: bool = True, commit: str | None = None
) -> tuple[str, str]:
    """A bundle row as ``complete`` leaves it; its id and digest."""
    bid = new_id("bdl")
    digest = "sha256:" + hashlib.sha256(bid.encode()).hexdigest()
    m = manifest or manifest_of()
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        conn.execute(
            "insert into ssc.bundle (id, org_id, app_id, digest, size_bytes, source_commit, "
            "actor_kind, actor_id) values (%s, %s, %s, %s, 10, %s, 'user', %s)",
            (bid, b.w.org, b.w.app, digest, commit, b.w.builder),
        )
        if stored:
            conn.execute(
                "update ssc.bundle set state = 'stored', manifest = %s::jsonb, "
                "manifest_digest = %s, file_count = 1, stored_at = now() where id = %s",
                (json.dumps(m.model_dump(mode="json", by_alias=True)), manifest_digest(m), bid),
            )
    return bid, digest


def post(b: Bench, path: str, body: dict[str, Any], token: str | None, **headers: str) -> Response:
    return b.client.post(
        path,
        json=body,
        headers=auth(token or b.t.builder, **{IDEMPOTENCY_HEADER: new_key(), **headers}),
    )


def get(b: Bench, path: str, token: str | None = None) -> Response:
    return b.client.get(path, headers=auth(token or b.t.builder))


def start_build(b: Bench, env: str, bundle_id: str, token: str | None = None) -> Response:
    path = f"/v1/apps/{b.w.app}/environments/{env}/builds"
    return post(b, path, {"bundle_id": bundle_id}, token)


def start_deploy(
    b: Bench,
    env: str,
    release: str,
    kind: str = "deploy",
    token: str | None = None,
    **headers: str,
) -> Response:
    path = f"/v1/apps/{b.w.app}/environments/{env}/deployments"
    return post(b, path, {"release_id": release, "kind": kind}, token, **headers)


def seed_prod_build(b: Bench, bundle_id: str, actor: str | None = None) -> str:
    """A queued prod build as promote leaves it; the builds route refuses prod (SSC-042)."""
    build = new_id("bld")
    execute(
        b.dsn,
        b.w.org,
        "insert into ssc.build (id, org_id, app_id, environment_id, bundle_id, actor_kind, "
        "actor_id) values (%s, %s, %s, %s, %s, 'user', %s)",
        build,
        b.w.org,
        b.w.app,
        b.w.prod,
        bundle_id,
        actor or b.w.builder,
    )
    return build


async def build_release(
    b: Bench, env: str, manifest: Manifest | None = None, token: str | None = None
) -> str:
    bundle, _ = seed_bundle(b, manifest)
    if env == b.w.prod:
        build = seed_prod_build(b, bundle, b.w.admin if token == b.t.admin else None)
    else:
        r = start_build(b, env, bundle, token)
        assert r.status_code == 202, r.text
        build = r.json()["build_id"]
    assert await run_build(b.ports, org_id=b.w.org, build_id=build) == "succeeded"
    release = get(b, f"/v1/builds/{build}").json()["release_id"]
    assert release is not None
    return str(release)


async def run(b: Bench, op: str, ports: Ports | None = None) -> str:
    return await run_deployment(ports or b.ports, org_id=b.w.org, deployment_id=op, health=FAST)


async def deploy(
    b: Bench, env: str, release: str, kind: str = "deploy", **headers: str
) -> tuple[str, str]:
    r = start_deploy(b, env, release, kind, **headers)
    assert r.status_code == 202, r.text
    op = str(r.json()["operation_id"])
    return op, await run(b, op)


def pointer(b: Bench, env: str) -> str | None:
    (row,) = rows_of(
        b.dsn, b.w.org, "select current_deployment_id from ssc.environment where id = %s", env
    )
    return row["current_deployment_id"]


def operation(b: Bench, op: str) -> dict[str, Any]:
    r = get(b, f"/v1/operations/{op}")
    assert r.status_code == 200, r.text
    return cast("dict[str, Any]", r.json())


def audit_of(b: Bench, target: str) -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select action, actor_kind, actor_id, before, after, policy_decision_id "
        "from ssc.audit_event where target_id = %s order by seq",
        target,
    )


def live_image(b: Bench, env: str) -> str:
    svc = b.runtime.services[service_name(env)]
    (serving,) = [r for r in svc.revisions if svc.traffic.get(r.name) == 100]
    return serving.image_digest


def image_of(b: Bench, release: str) -> str:
    (row,) = rows_of(b.dsn, b.w.org, "select image_digest from ssc.release where id = %s", release)
    return str(row["image_digest"])


def set_prod_gate(b: Bench, gate: object) -> None:
    app = cast("FastAPI", b.client.app)
    app.state.runtime = replace(app.state.runtime, prod_gate=gate)


# ── builds and releases ──────────────────────────────────────────────────────


async def test_a_build_makes_the_next_numbered_release(b: Bench) -> None:
    m = manifest_of(
        build={"public_env": {"preview": {"VITE_API": "https://preview.example"}}}, **FINANCE
    )
    commit = "c" * 40
    bundle, digest = seed_bundle(b, m, commit=commit)
    r = start_build(b, b.w.preview, bundle)
    assert r.status_code == 202, r.text
    build = r.json()["build_id"]
    assert r.headers["Location"] == f"/v1/builds/{build}"
    assert r.json()["state"] == "queued"
    diff = r.json()["capability_diff"]
    assert [(c["kind"], c["subject"]) for c in diff["changes"]] == [
        ("connection_missing", "finance")
    ]
    assert diff["blocks"] is False
    jobs = rows_of(
        b.dsn,
        b.w.org,
        "select task_name, queueing_lock, lock from procrastinate.procrastinate_jobs "
        "where queueing_lock = %s",
        f"bld:{build}",
    )
    assert jobs == [{"task_name": tasks.RUN_BUILD, "queueing_lock": f"bld:{build}", "lock": None}]

    assert await run_build(b.ports, org_id=b.w.org, build_id=build) == "succeeded"
    out = get(b, f"/v1/builds/{build}").json()
    assert (out["state"], out["release_number"], out["failure_code"]) == ("succeeded", 1, None)
    assert out["started_at"] is not None and out["finished_at"] is not None
    (release,) = rows_of(
        b.dsn, b.w.org, "select * from ssc.release where id = %s", out["release_id"]
    )
    assert release["image_digest"] == fake_image_digest(
        digest, "preview", {"VITE_API": "https://preview.example"}
    )
    assert release["source_digest"] == digest
    assert release["manifest_digest"] == manifest_digest(m)
    assert release["source_commit"] == commit
    assert release["scan_refs"] == [f"fake-scan:{build}"]
    assert (release["actor_kind"], release["actor_id"]) == ("user", b.w.builder)
    (request,) = b.builds.requests
    assert (request.env_name, request.source_digest, request.manifest) == ("preview", digest, m)
    assert [(a["action"], a["actor_id"]) for a in audit_of(b, build)] == [
        ("build.started", b.w.builder)
    ]
    assert [
        (a["action"], a["actor_id"], a["after"]["number"]) for a in audit_of(b, release["id"])
    ] == [("release.created", b.w.builder, 1)]
    listed = get(b, f"/v1/apps/{b.w.app}/releases/{release['id']}").json()
    assert listed["label"] == "R1"
    assert listed["built_for_environment_id"] == b.w.preview
    assert listed["actor"] == {"kind": "user", "id": b.w.builder, "via_agent": False}
    # A rerun of a finished build changes nothing.
    assert await run_build(b.ports, org_id=b.w.org, build_id=build) == "succeeded"
    assert len(b.builds.requests) == 1


async def test_concurrent_builds_number_releases_r1_to_r5(b: Bench) -> None:
    builds = []
    for _ in range(5):
        bundle, _ = seed_bundle(b)
        r = start_build(b, b.w.preview, bundle)
        assert r.status_code == 202, r.text
        builds.append(r.json()["build_id"])
    results = await asyncio.gather(
        *(run_build(b.ports, org_id=b.w.org, build_id=i) for i in builds)
    )
    assert results == ["succeeded"] * 5
    numbers = rows_of(b.dsn, b.w.org, "select number from ssc.release order by number")
    assert [r["number"] for r in numbers] == [1, 2, 3, 4, 5]
    page = get(b, f"/v1/apps/{b.w.app}/releases?limit=3").json()
    assert [i["label"] for i in page["items"]] == ["R5", "R4", "R3"]
    assert page["next_before"] == 3
    rest = get(b, f"/v1/apps/{b.w.app}/releases?limit=3&before=3").json()
    assert [i["label"] for i in rest["items"]] == ["R2", "R1"]
    assert rest["next_before"] is None


async def test_build_refusals(b: Bench) -> None:
    bundle, _ = seed_bundle(b)
    assert start_build(b, b.w.preview, bundle).status_code == 202
    assert_problem(start_build(b, b.w.preview, bundle), ErrorCode.BUILD_IN_FLIGHT)
    # Prod builds only through promote.
    assert_problem(start_build(b, b.w.prod, bundle), ErrorCode.PROD_REQUIRES_PROMOTE)
    pending, _ = seed_bundle(b, stored=False)
    assert_problem(start_build(b, b.w.preview, pending), ErrorCode.BUNDLE_NOT_UPLOADED)
    assert_problem(start_build(b, b.w.preview, new_id("bdl")), ErrorCode.REFERENCE_NOT_FOUND)
    assert_problem(start_build(b, b.w.preview, bundle, b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(start_build(b, new_id("env"), bundle), ErrorCode.NOT_FOUND)
    r = get(b, f"/v1/builds/{new_id('bld')}")
    assert_problem(r, ErrorCode.NOT_FOUND)
    assert_problem(get(b, f"/v1/apps/{b.w.app}/releases", b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(get(b, f"/v1/apps/{new_id('app')}/releases"), ErrorCode.NOT_FOUND)


async def test_a_failed_build_records_its_reason(b: Bench) -> None:
    bundle, digest = seed_bundle(b)
    b.builds.fail(digest, "BUILD_EXITED_NONZERO", "npm run build exited 1")
    build = start_build(b, b.w.preview, bundle).json()["build_id"]
    assert await run_build(b.ports, org_id=b.w.org, build_id=build) == "failed"
    out = get(b, f"/v1/builds/{build}").json()
    assert (out["state"], out["failure_code"], out["release_id"]) == (
        "failed",
        "BUILD_EXITED_NONZERO",
        None,
    )
    assert [(a["action"], a["after"]) for a in audit_of(b, build)] == [
        ("build.started", {"environment_id": b.w.preview, "bundle_id": bundle, "state": "queued"}),
        ("build.failed", {"state": "failed", "failure_code": "BUILD_EXITED_NONZERO"}),
    ]
    assert rows_of(b.dsn, b.w.org, "select count(*) as n from ssc.release") == [{"n": 0}]
    # The bundle may build again once nothing is in flight.
    assert start_build(b, b.w.preview, bundle).status_code == 202


async def stored_fixture(b: Bench, name: str | Path, root: Path) -> tuple[Ports, str]:
    """``conformance/build_fixtures/<name>``, or the source at a path, packed and stored as
    ``complete`` leaves it."""
    prepared = prepare(name if isinstance(name, Path) else FIXTURES / name, root / "bundle.tar.gz")
    digest = prepared.bundle.digest
    signer = UrlSigner({"k1": MASTER}, active="k1", clock=SystemClock())
    store = FsBlobStore(root / "blobs", signer=signer, base_url="https://blobs.test/v1/blobs")
    await store.put(bundle_key(b.w.org, b.w.app, digest), prepared.bundle.path.read_bytes())
    bid = new_id("bdl")
    m = prepared.manifest
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        conn.execute(
            "insert into ssc.bundle (id, org_id, app_id, digest, size_bytes, actor_kind, "
            "actor_id, state, manifest, manifest_digest, file_count, stored_at) values "
            "(%s, %s, %s, %s, %s, 'user', %s, 'stored', %s::jsonb, %s, %s, now())",
            (
                bid,
                b.w.org,
                b.w.app,
                digest,
                prepared.bundle.size,
                b.w.builder,
                json.dumps(m.model_dump(mode="json", by_alias=True)),
                manifest_digest(m),
                prepared.bundle.file_count,
            ),
        )
    return replace(b.ports, blob_store=store), bid


async def test_a_dash_bundle_deploys_as_a_session_app(b: Bench, tmp_path: Path) -> None:
    ports, bundle = await stored_fixture(b, "dash-app", tmp_path)
    build = start_build(b, b.w.preview, bundle).json()["build_id"]
    assert await run_build(ports, org_id=b.w.org, build_id=build) == "succeeded"
    release = get(b, f"/v1/builds/{build}").json()["release_id"]
    rows = rows_of(b.dsn, b.w.org, "select framework from ssc.release where id = %s", release)
    assert rows == [{"framework": "dash"}]
    assert (await deploy(b, b.w.preview, release))[1] == "healthy"
    svc = b.runtime.services[service_name(b.w.preview)]
    (revision,) = svc.revisions
    assert (revision.billing, revision.timeout_seconds, revision.concurrency) == (
        "instance",
        3600,
        1000,
    )
    assert svc.max_instances == 1


async def test_sqlite_on_disk_fails_the_build_before_the_builder(b: Bench, tmp_path: Path) -> None:
    ports, bundle = await stored_fixture(b, "sqlite-on-disk", tmp_path)
    build = start_build(b, b.w.preview, bundle).json()["build_id"]
    assert await run_build(ports, org_id=b.w.org, build_id=build) == "failed"
    out = get(b, f"/v1/builds/{build}").json()
    assert (out["state"], out["failure_code"]) == ("failed", "STATE_SQLITE_EPHEMERAL")
    assert b.builds.requests == []


async def test_an_unlisted_native_library_fails_the_build_before_the_builder(
    b: Bench, tmp_path: Path
) -> None:
    ports, bundle = await stored_fixture(b, "unlisted-native-library", tmp_path)
    build = start_build(b, b.w.preview, bundle).json()["build_id"]
    assert await run_build(ports, org_id=b.w.org, build_id=build) == "failed"
    out = get(b, f"/v1/builds/{build}").json()
    assert (out["state"], out["failure_code"]) == ("failed", "ADD_APPROVED_PACKAGE")
    assert b.builds.requests == []


async def test_listed_native_libraries_reach_the_builder(b: Bench, tmp_path: Path) -> None:
    ports, bundle = await stored_fixture(b, "listed-native-library", tmp_path)
    build = start_build(b, b.w.preview, bundle).json()["build_id"]
    assert await run_build(ports, org_id=b.w.org, build_id=build) == "succeeded"
    (request,) = b.builds.requests
    assert request.system_packages == ("poppler-utils",)


def test_0015_downgrades_and_upgrades(dsns: Dsns) -> None:
    rev = importlib.import_module("ssc_control.db.migrations.versions.0015_build_framework")
    name = f"m{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database=name).render_as_string(hide_password=False)
    columns = (
        "select table_name from information_schema.columns where table_schema = 'ssc' "
        "and table_name in ('build', 'release') and column_name = 'framework' order by 1"
    )

    def tables() -> list[str]:
        with psycopg.connect(dsn) as conn:
            return [r[0] for r in conn.execute(columns).fetchall()]

    upgrade(dsn)
    assert tables() == ["build", "release"]
    downgrade(dsn, rev.down_revision)
    assert tables() == []
    upgrade(dsn)
    assert tables() == ["build", "release"]


async def test_a_framework_must_be_a_short_lowercase_name(b: Bench) -> None:
    bundle, _ = seed_bundle(b)
    build = start_build(b, b.w.preview, bundle).json()["build_id"]
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("update ssc.build set framework = 'Dash App' where id = %s", (build,))


async def test_a_build_polls_later_then_times_out(b: Bench) -> None:
    slow = with_cell(b.ports, build=FakeBuildDriver(polls=5))
    bundle, _ = seed_bundle(b)
    build = start_build(b, b.w.preview, bundle).json()["build_id"]
    take_job(b.dsn, f"bld:{build}")
    assert await run_build(slow, org_id=b.w.org, build_id=build) == "running"
    later = rows_of(
        b.dsn,
        b.w.org,
        "select scheduled_at from procrastinate.procrastinate_jobs "
        "where queueing_lock = %s and status = 'todo' and scheduled_at is not null",
        f"bld:{build}",
    )
    assert len(later) == 1
    # A minute ahead, so the database's clock running a little fast cannot hide the timeout.
    ahead = replace(slow, clock=lambda: datetime.now(UTC) + timedelta(minutes=1))
    out = await run_build(ahead, org_id=b.w.org, build_id=build, build_timeout=timedelta(0))
    assert out == "failed"
    assert get(b, f"/v1/builds/{build}").json()["failure_code"] == BUILD_TIMED_OUT
    # With no builder configured a build fails at once.
    other, _ = seed_bundle(b)
    build = start_build(b, b.w.preview, other).json()["build_id"]
    none = with_cell(b.ports, build=None)
    assert await run_build(none, org_id=b.w.org, build_id=build) == "failed"
    assert get(b, f"/v1/builds/{build}").json()["failure_code"] == BUILD_DRIVER_UNAVAILABLE


class CountingBuilds(FakeBuildDriver):
    def __init__(self, *, polls: int = 0) -> None:
        super().__init__(polls=polls)
        self.polls = 0

    async def poll(self, ref: str) -> BuildStatus:
        self.polls += 1
        return await super().poll(ref)


async def test_a_build_of_a_stopped_app_fails_when_claimed(b: Bench) -> None:
    builds = CountingBuilds(polls=5)
    ports = with_cell(b.ports, build=builds)
    bundle, _ = seed_bundle(b)
    queued = start_build(b, b.w.preview, bundle).json()["build_id"]
    other, _ = seed_bundle(b)
    running = seed_prod_build(b, other)
    take_job(b.dsn, f"bld:{running}")
    assert await run_build(ports, org_id=b.w.org, build_id=running) == "running"
    assert (len(builds.requests), builds.polls) == (1, 1)
    execute(b.dsn, b.w.org, "update ssc.app set status = 'quarantined' where id = %s", b.w.app)
    # Queued or already running, the next step fails it before any builder call.
    for build in (queued, running):
        assert await run_build(ports, org_id=b.w.org, build_id=build) == "failed"
        out = get(b, f"/v1/builds/{build}").json()
        assert (out["state"], out["failure_code"]) == ("failed", APP_NOT_ACTIVE)
        assert audit_of(b, build)[-1]["action"] == "build.failed"
    assert (len(builds.requests), builds.polls) == (1, 1)
    assert rows_of(b.dsn, b.w.org, "select count(*) as n from ssc.release") == [{"n": 0}]


async def test_a_release_row_cannot_be_edited(b: Bench, dsns: Dsns) -> None:
    release = await build_release(b, b.w.preview)
    update = "update ssc.release set number = 99 where id = %s"
    delete = "delete from ssc.release where id = %s"
    for dsn, expected in ((dsns.app, "42501"), (dsns.migrate, "SC004")):
        for sql in (update, delete):
            with psycopg.connect(dsn) as conn:
                bind_org_sync(conn, b.w.org)
                with pytest.raises(psycopg.Error) as e:
                    conn.execute(sql, (release,))
            assert e.value.sqlstate == expected, (dsn, sql)
            if expected == "SC004":
                code, _ = classify(DBAPIError(sql, None, e.value))
                assert code is ErrorCode.RECORD_IMMUTABLE
    (row,) = rows_of(b.dsn, b.w.org, "select number from ssc.release where id = %s", release)
    assert row["number"] == 1


def test_no_route_mutates_a_release() -> None:
    api = Path(ssc_control.api.__file__).parent
    writes = re.compile(r"\b(update|delete\s+from|truncate)\s+(table\s+)?ssc\.release\b", re.I)
    offenders = [str(p) for p in api.rglob("*.py") if writes.search(p.read_text())]
    assert offenders == []
    spec = json.loads((Path(__file__).parents[3] / "docs" / "api" / "openapi.json").read_text())
    for path, item in spec["paths"].items():
        if "/releases" in path:
            assert set(item) == {"get"}, path


# ── deployments ──────────────────────────────────────────────────────────────


async def test_a_healthy_deploy_moves_the_pointer_and_traffic(b: Bench) -> None:
    release = await build_release(b, b.w.preview)
    r = start_deploy(b, b.w.preview, release)
    op = r.json()["operation_id"]
    jobs = rows_of(
        b.dsn,
        b.w.org,
        "select task_name, lock from procrastinate.procrastinate_jobs where queueing_lock = %s",
        f"dep:{op}",
    )
    assert jobs == [{"task_name": tasks.RUN_DEPLOYMENT, "lock": f"env:{b.w.preview}"}]
    assert await run(b, op) == "healthy"
    assert pointer(b, b.w.preview) == op
    assert live_image(b, b.w.preview) == image_of(b, release)
    out = operation(b, op)
    assert (out["state"], out["failure_code"]) == ("healthy", None)
    assert [a["action"] for a in audit_of(b, op)] == ["deploy.started", "deploy.finished"]
    history = get(b, f"/v1/apps/{b.w.app}/environments/{b.w.preview}/deployments").json()
    assert [(i["operation_id"], i["current"], i["release_number"]) for i in history["items"]] == [
        (op, True, 1)
    ]
    # A rerun of a finished deployment changes nothing.
    b.runtime.reset_calls()
    assert await run(b, op) == "healthy"
    assert b.runtime.calls == []


async def test_a_failed_health_check_keeps_the_old_pointer(b: Bench) -> None:
    r1 = await build_release(b, b.w.preview)
    first, state = await deploy(b, b.w.preview, r1)
    assert state == "healthy"
    r2 = await build_release(b, b.w.preview)
    b.runtime.unhealthy(image_of(b, r2))
    second, state = await deploy(b, b.w.preview, r2)
    assert state == "failed"
    assert pointer(b, b.w.preview) == first
    assert live_image(b, b.w.preview) == image_of(b, r1)
    assert operation(b, second)["failure_code"] == HEALTH_CHECK_FAILED
    assert [(a["action"], a["after"]) for a in audit_of(b, second)][-1] == (
        "deploy.failed",
        {
            "state": "failed",
            "failure_code": HEALTH_CHECK_FAILED,
            "release_id": r2,
            "environment_id": b.w.preview,
        },
    )
    history = get(b, f"/v1/apps/{b.w.app}/environments/{b.w.preview}/deployments").json()
    assert [(i["state"], i["current"]) for i in history["items"]] == [
        ("failed", False),
        ("healthy", True),
    ]


async def test_a_revision_that_never_starts_times_out(b: Bench) -> None:
    release = await build_release(b, b.w.preview)
    b.runtime.starting(image_of(b, release))
    r = start_deploy(b, b.w.preview, release)
    op = r.json()["operation_id"]
    short = HealthWait(within=0.05, every=0.01)
    assert await run_deployment(b.ports, org_id=b.w.org, deployment_id=op, health=short) == "failed"
    assert operation(b, op)["failure_code"] == HEALTH_CHECK_FAILED
    assert pointer(b, b.w.preview) is None
    # Nothing was live before, so the service is left scaled to zero.
    assert b.runtime.services[service_name(b.w.preview)].stopped is True


def elsewhere(b: Bench) -> Ports:
    """The bench's ports with its one cell serving another org alone: the bench's org has no
    cell this worker can reach (decision 030)."""
    cell = OrgCell(label=STATIC_LABEL, runtime=b.runtime, build=b.builds)
    return replace(b.ports, cells=StaticCells(orgs={new_id("org"): cell}))


async def test_a_deployment_for_an_org_whose_cell_is_not_configured_fails(b: Bench) -> None:
    release = await build_release(b, b.w.preview)
    b.runtime.reset_calls()
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert await run(b, op, elsewhere(b)) == "failed"
    assert operation(b, op)["failure_code"] == "CELL_UNAVAILABLE"
    assert pointer(b, b.w.preview) is None
    assert b.runtime.calls == []


async def test_a_build_for_an_org_whose_cell_is_not_configured_fails(b: Bench) -> None:
    bundle, _ = seed_bundle(b)
    r = start_build(b, b.w.preview, bundle)
    assert r.status_code == 202, r.text
    build = r.json()["build_id"]
    assert await run_build(elsewhere(b), org_id=b.w.org, build_id=build) == "failed"
    out = get(b, f"/v1/builds/{build}").json()
    assert (out["state"], out["failure_code"]) == ("failed", "CELL_UNAVAILABLE")


async def test_one_deployment_in_flight_per_environment(b: Bench) -> None:
    r1 = await build_release(b, b.w.preview)
    assert start_deploy(b, b.w.preview, r1).status_code == 202
    assert_problem(start_deploy(b, b.w.preview, r1), ErrorCode.DEPLOYMENT_IN_FLIGHT)
    # A rollback pre-empts a forward deploy, but not another rollback.
    assert start_deploy(b, b.w.preview, r1, "rollback").status_code == 202
    assert_problem(start_deploy(b, b.w.preview, r1, "rollback"), ErrorCode.DEPLOYMENT_IN_FLIGHT)
    assert_problem(start_deploy(b, b.w.preview, r1), ErrorCode.DEPLOYMENT_IN_FLIGHT)


async def test_a_rollback_pre_empts_a_running_deploy(b: Bench) -> None:
    r1 = await build_release(b, b.w.preview)
    first, _ = await deploy(b, b.w.preview, r1)
    r2 = await build_release(b, b.w.preview)
    second = start_deploy(b, b.w.preview, r2).json()["operation_id"]
    b.runtime.slow("observe", 1.0)
    job = asyncio.create_task(run(b, second))
    # R2 is applied and ready in the fake; its job is parked inside ``observe``.
    await asyncio.wait_for(b.hold.entered.wait(), 10)
    r = await asyncio.to_thread(start_deploy, b, b.w.preview, r1, "rollback")
    assert r.status_code == 202, r.text
    rollback = r.json()["operation_id"]
    assert operation(b, second)["state"] == "superseded"
    b.runtime.slow("observe", 0)
    b.hold.released.set()
    # The R2 job resumes, its compare-and-set fails, and the pointer never moves to R2.
    assert await asyncio.wait_for(job, 10) == "superseded"
    assert pointer(b, b.w.preview) == first
    assert [a["action"] for a in audit_of(b, second)] == ["deploy.started"]
    assert audit_of(b, rollback)[0]["after"]["superseded"] == [second]
    assert await run(b, rollback) == "healthy"
    assert pointer(b, b.w.preview) == rollback
    assert live_image(b, b.w.preview) == image_of(b, r1)
    assert operation(b, first)["state"] == "superseded"
    assert [a["action"] for a in audit_of(b, rollback)] == ["rollback.started", "rollback.finished"]


async def test_a_rollback_keeps_sharing_and_timers(b: Bench) -> None:
    r1 = await build_release(b, b.w.preview, manifest_of(**NIGHTLY))
    await deploy(b, b.w.preview, r1)
    assert b.timers.calls == [(b.w.preview, ("nightly",), b.w.builder)]
    grants = f"/v1/apps/{b.w.app}/environments/{b.w.preview}/grants"
    body = {
        "grants": [
            {"role": "builder", "subject_kind": "user", "subject_id": b.w.builder},
            {"role": "builder", "subject_kind": "user", "subject_id": b.w.member},
        ]
    }
    put = b.client.put(grants, json=body, headers=auth(b.t.admin, **{"If-Match": '"1"'}))
    assert put.status_code == 200, put.text
    r2 = await build_release(b, b.w.preview)
    await deploy(b, b.w.preview, r2)
    assert len(b.timers.calls) == 2
    before = get(b, grants, b.t.admin)
    op, state = await deploy(b, b.w.preview, r1, "rollback")
    assert state == "healthy"
    assert len(b.timers.calls) == 2  # a rollback never touches timers
    after = get(b, grants, b.t.admin)
    assert (after.json(), after.headers["ETag"]) == (before.json(), before.headers["ETag"])
    (row,) = rows_of(
        b.dsn,
        b.w.org,
        "select d.grants_version, d.config_version, e.grants_version as env_grants, "
        "e.config_version as env_config from ssc.deployment d join ssc.environment e "
        "on e.id = d.environment_id where d.id = %s",
        op,
    )
    assert row["grants_version"] == row["env_grants"] == 2
    assert row["config_version"] == row["env_config"]


async def test_a_blocked_gate_never_boots_prod(b: Bench) -> None:
    spy = SpyGate("waiting")
    set_prod_gate(b, spy)
    ports = replace(b.ports, prod_gate=spy)
    release = await build_release(b, b.w.prod)
    op = start_deploy(b, b.w.prod, release).json()["operation_id"]
    assert await run(b, op, ports) == "failed"
    assert operation(b, op)["failure_code"] == APPROVAL_REQUIRED
    assert b.runtime.calls == []
    assert spy.calls == [b.w.prod, b.w.prod]  # at POST time and in the job
    preview = await build_release(b, b.w.preview)
    _, state = await deploy(b, b.w.preview, preview)
    assert state == "healthy"
    assert spy.calls == [b.w.prod, b.w.prod]  # preview never asks


async def test_a_release_without_a_manifest_never_boots(b: Bench) -> None:
    release = new_id("rel")
    execute(
        b.dsn,
        b.w.org,
        "insert into ssc.release (id, org_id, app_id, number, image_digest, manifest_digest, "
        "source_digest, actor_kind, actor_id) values (%s, %s, %s, 1, %s, %s, %s, 'user', %s)",
        release,
        b.w.org,
        b.w.app,
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        "sha256:" + "3" * 64,
        b.w.builder,
    )
    # The POST-time gate refuses (capabilities unknown) but only advises.
    op = start_deploy(b, b.w.prod, release).json()["operation_id"]
    assert await run(b, op) == "failed"
    assert operation(b, op)["failure_code"] == RELEASE_SPEC_UNAVAILABLE
    assert operation(b, op)["billing"] is None
    assert b.runtime.calls == []


@pytest.mark.parametrize(
    ("runtime", "billing"),
    [
        ({}, "request"),
        ({"sessions": True}, "instance"),
        ({"start": "streamlit run app.py --server.port $PORT"}, "instance"),
    ],
)
async def test_an_operation_says_how_its_release_is_billed(
    b: Bench, runtime: dict[str, Any], billing: str
) -> None:
    release = await build_release(b, b.w.preview, manifest_of(runtime=runtime))
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert operation(b, op)["billing"] == billing


async def test_the_production_gate_end_to_end(b: Bench) -> None:
    assert isinstance(cast("FastAPI", b.client.app).state.runtime.prod_gate, ApprovalsProdGate)
    release = await build_release(b, b.w.prod, manifest_of(**FINANCE), b.t.admin)
    r = start_deploy(b, b.w.prod, release, token=b.t.admin)
    assert r.status_code == 202, r.text
    op = r.json()["operation_id"]
    # The gate ran at POST time: the approval it opened committed with the deployment.
    (asked,) = rows_of(
        b.dsn,
        b.w.org,
        "select id, kind, subject_key, state, requested_by_user_id from ssc.approval_request",
    )
    assert (asked["kind"], asked["subject_key"], asked["state"]) == (
        "connect_data_source",
        "finance",
        "pending",
    )
    assert asked["requested_by_user_id"] == b.w.admin
    assert audit_of(b, op)[0]["policy_decision_id"] is not None
    # The job waits for the approval: it fails, never boots, and keeps the request.
    assert await run(b, op) == "failed"
    assert operation(b, op)["failure_code"] == APPROVAL_REQUIRED
    assert b.runtime.calls == []
    failed = audit_of(b, op)[-1]
    assert (failed["action"], failed["policy_decision_id"] is not None) == ("deploy.failed", True)
    assert rows_of(b.dsn, b.w.org, "select state from ssc.approval_request") == [
        {"state": "pending"}
    ]
    async with bound_org(b.ports.engine, b.w.org) as conn:
        await service.decide(
            conn,
            org_id=b.w.org,
            approval_id=asked["id"],
            decider=service.Decider(
                user_id=b.w.approver,
                via_agent=False,
                recorded_by_operator=None,
                channel="console",
                reason="Finance agreed.",
                outcome="approved",
            ),
            actor=Actor(ActorKind.USER, b.w.approver),
        )
    again, state = await deploy(b, b.w.prod, release)
    assert state == "healthy"
    assert pointer(b, b.w.prod) == again
    assert [m for m, _ in b.runtime.calls][:1] == ["apply"]


async def test_a_release_built_for_preview_is_refused_in_prod(b: Bench) -> None:
    release = await build_release(b, b.w.preview)
    r = start_deploy(b, b.w.prod, release)
    assert_problem(r, ErrorCode.RELEASE_ENVIRONMENT_MISMATCH)
    assert_problem(start_deploy(b, b.w.preview, release, token=b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(start_deploy(b, b.w.preview, new_id("rel")), ErrorCode.REFERENCE_NOT_FOUND)
    assert_problem(start_deploy(b, new_id("env"), release), ErrorCode.NOT_FOUND)


async def test_a_disabled_app_is_refused(b: Bench) -> None:
    release = await build_release(b, b.w.preview)
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    execute(b.dsn, b.w.org, "update ssc.app set status = 'disabled' where id = %s", b.w.app)
    # Disabled after the POST: the job refuses and never calls the runtime.
    assert await run(b, op) == "failed"
    assert operation(b, op)["failure_code"] == APP_NOT_ACTIVE
    assert b.runtime.calls == []
    assert_problem(start_deploy(b, b.w.preview, release), ErrorCode.APP_NOT_ACTIVE)
    bundle, _ = seed_bundle(b)
    assert_problem(start_build(b, b.w.preview, bundle), ErrorCode.APP_NOT_ACTIVE)


async def test_a_stopped_app_ends_a_deploy_within_one_poll(b: Bench) -> None:
    r1 = await build_release(b, b.w.preview, manifest_of(**NIGHTLY))
    first, _ = await deploy(b, b.w.preview, r1)
    r2 = await build_release(b, b.w.preview, manifest_of(**NIGHTLY))
    b.runtime.starting(image_of(b, r2))
    second = start_deploy(b, b.w.preview, r2).json()["operation_id"]
    synced = len(b.timers.calls)
    b.runtime.reset_calls()

    async def stop_between_polls(_seconds: float) -> None:
        sql = "update ssc.app set status = 'disabled' where id = %s"
        await asyncio.to_thread(execute, b.dsn, b.w.org, sql, b.w.app)

    # A minute to become healthy: only the stop can end the job at once.
    wait = HealthWait(within=60.0, every=0.01, sleep=stop_between_polls)
    job = run_deployment(b.ports, org_id=b.w.org, deployment_id=second, health=wait)
    assert await asyncio.wait_for(job, 10) == "failed"
    assert_stopped_without_going_live(b, second, first, r1, synced)
    service = service_name(b.w.preview)
    assert b.runtime.calls == [("apply", service), ("observe", service)]


async def test_a_stop_while_observing_is_caught_when_going_live(b: Bench) -> None:
    r1 = await build_release(b, b.w.preview, manifest_of(**NIGHTLY))
    first, _ = await deploy(b, b.w.preview, r1)
    r2 = await build_release(b, b.w.preview, manifest_of(**NIGHTLY))
    second = start_deploy(b, b.w.preview, r2).json()["operation_id"]
    synced = len(b.timers.calls)
    b.runtime.reset_calls()
    b.runtime.slow("observe", 1.0)
    job = asyncio.create_task(run(b, second))
    # The poll saw an active app; R2 is ready and its job is parked inside ``observe``.
    await asyncio.wait_for(b.hold.entered.wait(), 10)
    pulled, commit = asyncio.Event(), asyncio.Event()

    async def pull_the_switch() -> None:
        async with bound_org(b.ports.engine, b.w.org) as conn:
            await kill_switch.start(
                conn,
                org_id=b.w.org,
                app_id=b.w.app,
                mode="disable",
                actor=Actor(ActorKind.USER, b.w.admin),
            )
            pulled.set()
            await commit.wait()

    switch = asyncio.create_task(pull_the_switch())
    await asyncio.wait_for(pulled.wait(), 10)
    b.runtime.slow("observe", 0)
    b.hold.released.set()
    # Going live waits on the uncommitted switch, then sees the app stopped.
    await asyncio.wait_for(asyncio.to_thread(wait_for_a_lock_wait, b.dsn), 10)
    commit.set()
    await asyncio.wait_for(switch, 10)
    assert await asyncio.wait_for(job, 10) == "failed"
    assert_stopped_without_going_live(b, second, first, r1, synced)
    assert "set_traffic" not in {method for method, _ in b.runtime.calls}


def assert_stopped_without_going_live(
    b: Bench, op: str, live: str, live_release: str, synced: int
) -> None:
    assert operation(b, op)["failure_code"] == APP_NOT_ACTIVE
    assert pointer(b, b.w.preview) == live
    assert live_image(b, b.w.preview) == image_of(b, live_release)
    assert len(b.timers.calls) == synced
    last = audit_of(b, op)[-1]
    assert (last["action"], last["after"]["failure_code"]) == ("deploy.failed", APP_NOT_ACTIVE)


async def test_going_live_locks_schedules_before_audit_head(b: Bench) -> None:
    # A schedule's own writers (pause, resume, a run's claim) lock its row, then audit.
    ports = replace(b.ports, timers=Timers())
    r1 = await build_release(b, b.w.preview, manifest_of(**NIGHTLY))
    assert await run(b, start_deploy(b, b.w.preview, r1).json()["operation_id"], ports) == "healthy"
    r2 = await build_release(b, b.w.preview, manifest_of(**NIGHTLY))
    second = start_deploy(b, b.w.preview, r2).json()["operation_id"]
    lock_schedules = text(
        "select 1 from ssc.schedule where org_id = :org and environment_id = :env for update"
    )
    async with bound_org(b.ports.engine, b.w.org) as holder:
        pid = await backend_pid(holder)
        await holder.execute(lock_schedules, {"org": b.w.org, "env": b.w.preview})
        job = asyncio.create_task(run(b, second, ports))
        await asyncio.wait_for(asyncio.to_thread(wait_for_a_lock_wait, b.dsn, blocker=pid), 10)
        free = await audit_head_is_free(holder, b.w.org)
    assert await asyncio.wait_for(job, 10) == "healthy"
    assert free
    assert pointer(b, b.w.preview) == second


# ── metrics ──────────────────────────────────────────────────────────────────


async def test_deploys_record_the_deploy_and_first_url_metrics(b: Bench) -> None:
    release = await build_release(b, b.w.preview)
    await deploy(b, b.w.preview, release, **{"X-SSC-Source-Tool": "Cursor"})
    await deploy(b, b.w.preview, release, "rollback")
    await deploy(b, b.w.preview, release)
    events = rows_of(
        b.dsn,
        b.w.org,
        "select kind, app_id, source_tool, properties, pseudonym is not null as person "
        "from ssc.metrics_event order by id",
    )
    assert events == [
        {
            "kind": "deploy",
            "app_id": b.w.app,
            "source_tool": "cursor",
            "properties": {"environment": "preview", "via_agent": False},
            "person": True,
        },
        {
            "kind": "first_url",
            "app_id": b.w.app,
            "source_tool": None,
            "properties": {"environment": "preview"},
            "person": True,
        },
        {
            "kind": "deploy",
            "app_id": b.w.app,
            "source_tool": None,
            "properties": {"environment": "preview", "via_agent": False},
            "person": True,
        },
    ]


# ── the request deadline (SSC-090) ───────────────────────────────────────────

SESSION_APP = {"runtime": {"sessions": True}}


async def told(b: Bench, env: str) -> int:
    """The timeout a snapshot compiled now tells the gateway for ``env``."""
    async with bound_org(b.ports.engine, b.w.org) as conn:
        doc = await compile_document(conn, b.w.org, version=0, compiled_at=datetime.now(UTC))
    return doc.environments[env].timeout_seconds or REQUEST_TIMEOUT_SECONDS


class Cell(NullSnapshotPort):
    """The org's cell as the deploy job sees it. ``holds`` is the timeout of the snapshot it
    last confirmed; it confirms a version only while ``answering``."""

    def __init__(self, b: Bench, env: str, *, answering: bool = True) -> None:
        self.b, self.env, self.answering = b, env, answering
        self.version = 0
        self.holds = REQUEST_TIMEOUT_SECONDS
        self.events: list[str] = []

    async def request(self, conn: AsyncConnection, org_id: str) -> int:
        self.version += 1
        self.events.append("request")
        return self.version

    async def confirmed(self, org_id: str, version: int) -> bool:
        self.events.append("confirmed")
        if self.answering:
            self.holds = await told(self.b, self.env)
        return self.answering

    async def catch_up(self) -> None:
        self.holds = await told(self.b, self.env)


def watch_traffic(
    b: Bench, cell: Cell, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[int | None, int, int, int]]:
    """Each ``set_traffic``, just before it: the serving revision's timeout, the new one's,
    what a compile now would tell, and what the cell holds."""
    seen: list[tuple[int | None, int, int, int]] = []
    move = b.runtime.set_traffic

    async def watched(service: str, revision: str) -> None:
        svc = b.runtime.services[service]
        serving = [r.timeout_seconds for r in svc.revisions if svc.traffic.get(r.name) == 100]
        (new,) = [r.timeout_seconds for r in svc.revisions if r.name == revision]
        seen.append((serving[0] if serving else None, new, await told(b, cell.env), cell.holds))
        cell.events.append("set_traffic")
        await move(service, revision)

    monkeypatch.setattr(b.runtime, "set_traffic", watched)
    return seen


async def test_the_snapshot_carries_only_a_longer_timeout(b: Bench) -> None:
    async with bound_org(b.ports.engine, b.w.org) as conn:
        doc = await compile_document(conn, b.w.org, version=0, compiled_at=datetime.now(UTC))
    assert "timeout_seconds" not in doc.model_dump(mode="json")["environments"][b.w.preview]
    for stored, member in ((REQUEST_TIMEOUT_SECONDS, None), (3600, 3600), (None, None)):
        execute(
            b.dsn,
            b.w.org,
            "update ssc.environment set request_timeout_seconds = %s where id = %s",
            stored,
            b.w.preview,
        )
        async with bound_org(b.ports.engine, b.w.org) as conn:
            doc = await compile_document(conn, b.w.org, version=0, compiled_at=datetime.now(UTC))
        envs = doc.model_dump(mode="json")["environments"]
        assert envs[b.w.preview].get("timeout_seconds") == member
        assert "timeout_seconds" not in envs[b.w.prod]
    with pytest.raises(psycopg.errors.CheckViolation):
        execute(
            b.dsn,
            b.w.org,
            "update ssc.environment set request_timeout_seconds = 0 where id = %s",
            b.w.preview,
        )


@pytest.mark.parametrize(
    ("first", "then"),
    [({}, SESSION_APP), (SESSION_APP, {})],
    ids=["request-to-session", "session-to-request"],
)
async def test_a_billing_change_never_makes_the_deadline_late(
    b: Bench, monkeypatch: pytest.MonkeyPatch, first: dict[str, Any], then: dict[str, Any]
) -> None:
    cell = Cell(b, b.w.preview)
    ports = replace(b.ports, snapshot=cell)
    seen = watch_traffic(b, cell, monkeypatch)
    before, after = (
        SESSION_TIMEOUT_SECONDS if m else REQUEST_TIMEOUT_SECONDS for m in (first, then)
    )
    r1 = await build_release(b, b.w.preview, manifest_of(**first))
    assert await run(b, start_deploy(b, b.w.preview, r1).json()["operation_id"], ports) == "healthy"
    assert await told(b, b.w.preview) == before
    await cell.catch_up()
    cell.events.clear()
    r2 = await build_release(b, b.w.preview, manifest_of(**then))
    op = start_deploy(b, b.w.preview, r2).json()["operation_id"]
    assert await run(b, op, ports) == "healthy"
    serving, new, compiled, held = seen[-1]
    assert (serving, new) == (before, after)
    assert compiled <= min(serving, new)
    assert held <= new
    assert await told(b, b.w.preview) == after
    if after < before:
        assert cell.events == ["request", "confirmed", "set_traffic"]
    else:
        assert cell.events == ["set_traffic", "request"]


def stored_timeout(b: Bench, env: str) -> int | None:
    (row,) = rows_of(
        b.dsn, b.w.org, "select request_timeout_seconds from ssc.environment where id = %s", env
    )
    return row["request_timeout_seconds"]


async def test_an_unconfirmed_lower_timeout_keeps_the_old_revision(b: Bench) -> None:
    cell = Cell(b, b.w.preview, answering=False)
    ports = replace(b.ports, snapshot=cell)
    r1 = await build_release(b, b.w.preview, manifest_of(**SESSION_APP))
    first = start_deploy(b, b.w.preview, r1).json()["operation_id"]
    assert await run(b, first, ports) == "healthy"
    r2 = await build_release(b, b.w.preview)
    second = start_deploy(b, b.w.preview, r2).json()["operation_id"]
    wait = HealthWait(within=2.0, every=0.01, confirm_within=0.05)
    assert (
        await run_deployment(ports, org_id=b.w.org, deployment_id=second, health=wait) == "failed"
    )
    assert (
        operation(b, second)["failure_code"]
        == SNAPSHOT_UNCONFIRMED
        == ErrorCode.SNAPSHOT_UNCONFIRMED
    )
    assert pointer(b, b.w.preview) == first
    assert live_image(b, b.w.preview) == image_of(b, r1)


@pytest.mark.parametrize(
    ("how", "code", "stored"),
    [
        ("unconfirmed", SNAPSHOT_UNCONFIRMED, SESSION_TIMEOUT_SECONDS),
        ("unhealthy", HEALTH_CHECK_FAILED, SESSION_TIMEOUT_SECONDS),
        ("runtime-error", RUNTIME_ERROR, SESSION_TIMEOUT_SECONDS),
        ("unhealthy", HEALTH_CHECK_FAILED, None),
    ],
    ids=["unconfirmed", "unhealthy", "runtime-error", "unhealthy-before-0024"],
)
async def test_a_failed_session_to_request_deploy_puts_the_timeout_back(
    b: Bench, how: str, code: str, stored: int | None
) -> None:
    cell = Cell(b, b.w.preview, answering=how != "unconfirmed")
    ports = replace(b.ports, snapshot=cell)
    r1 = await build_release(b, b.w.preview, manifest_of(**SESSION_APP))
    first = start_deploy(b, b.w.preview, r1).json()["operation_id"]
    assert await run(b, first, ports) == "healthy"
    execute(
        b.dsn,
        b.w.org,
        "update ssc.environment set request_timeout_seconds = %s where id = %s",
        stored,
        b.w.preview,
    )
    r2 = await build_release(b, b.w.preview)
    if how == "unhealthy":
        b.runtime.unhealthy(image_of(b, r2))
    if how == "runtime-error":
        b.runtime.fail_next("apply", RuntimeError("boom"))
    second = start_deploy(b, b.w.preview, r2).json()["operation_id"]
    cell.events.clear()
    wait = HealthWait(within=2.0, every=0.01, confirm_within=0.05)
    assert (
        await run_deployment(ports, org_id=b.w.org, deployment_id=second, health=wait) == "failed"
    )
    assert operation(b, second)["failure_code"] == code
    assert pointer(b, b.w.preview) == first
    assert live_image(b, b.w.preview) == image_of(b, r1)
    assert stored_timeout(b, b.w.preview) == SESSION_TIMEOUT_SECONDS
    assert await told(b, b.w.preview) == SESSION_TIMEOUT_SECONDS
    assert cell.events[0] == "request"
    assert cell.events[-1] == "request"


async def test_a_failed_first_deploy_has_nothing_to_put_back(b: Bench) -> None:
    cell = Cell(b, b.w.preview)
    ports = replace(b.ports, snapshot=cell)
    execute(
        b.dsn,
        b.w.org,
        "update ssc.environment set request_timeout_seconds = %s where id = %s",
        SESSION_TIMEOUT_SECONDS,
        b.w.preview,
    )
    release = await build_release(b, b.w.preview)
    b.runtime.unhealthy(image_of(b, release))
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert await run(b, op, ports) == "failed"
    assert pointer(b, b.w.preview) is None
    assert stored_timeout(b, b.w.preview) == REQUEST_TIMEOUT_SECONDS
    assert cell.events == ["request"]


# ── through the cell agent, on the emulators ─────────────────────────────────


class Clock:
    """The seconds each driver would have slept; sleeping settles the Cloud Run emulator."""

    def __init__(self, emulator: CloudRunEmulator) -> None:
        self.emulator = emulator
        self.driver: list[float] = []
        self.health: list[float] = []

    async def driver_sleep(self, seconds: float) -> None:
        self.driver.append(seconds)
        self.emulator.settle()

    async def health_sleep(self, seconds: float) -> None:
        self.health.append(seconds)
        self.emulator.settle()


async def agent_token(_: str) -> str:
    return "id-token"


async def access_token() -> str:
    return "access-token"


async def cell_build(b: Bench, ports: Ports) -> str:
    bundle = rows_of(b.dsn, b.w.org, "select id from ssc.bundle")[0]["id"]
    build = start_build(b, b.w.preview, bundle).json()["build_id"]
    for _ in range(5):
        take_job(b.dsn, f"bld:{build}")
        if await run_build(ports, org_id=b.w.org, build_id=build) != "running":
            break
    out = get(b, f"/v1/builds/{build}").json()
    assert out["state"] == "succeeded", out
    return str(out["release_id"])


async def test_rollback_through_the_cell_agent_is_quick_and_never_rebuilds(
    b: Bench, tmp_path: Path
) -> None:
    ports, _ = await stored_fixture(b, "cs-fastapi-hello", tmp_path)
    data = (tmp_path / "bundle.tar.gz").read_bytes()
    signer = UrlSigner({"k1": MASTER}, active="k1", clock=SystemClock())

    def fetch(url: str) -> bytes:
        parts = urlsplit(url)
        key = parts.path.removeprefix("/v1/blobs/")
        signer.verify("GET", key, dict(parse_qsl(parts.query)))
        return data

    run_emulator, build_emulator = CloudRunEmulator(), CloudBuildEmulator(fetch, polls=0)
    clock = Clock(run_emulator)
    cloud_run = CloudRunDriver(
        CELL_RUNTIME,
        access_token,
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(run_emulator.handler)),
        sleep=clock.driver_sleep,
    )
    cloud_build = CloudBuildDriver(
        CELL_BUILD,
        access_token,
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(build_emulator.handler)),
    )
    agent = httpx2.ASGITransport(app=create_agent(cloud_run, cloud_build, org_id=b.w.org))
    runtime = CellAgentDriver(
        AGENT, agent_token, org_id=b.w.org, client=httpx2.AsyncClient(transport=agent)
    )
    builder = CellAgentBuildDriver(
        AGENT,
        agent_token,
        ports.blob_store,
        org_id=b.w.org,
        client=httpx2.AsyncClient(transport=agent),
    )
    ports = with_cell(ports, runtime=runtime, build=builder)
    health = HealthWait(within=5.0, every=HEALTH_POLL_SECONDS, sleep=clock.health_sleep)

    async def deploy_through_the_cell(release: str, kind: str = "deploy") -> tuple[str, str]:
        op = start_deploy(b, b.w.preview, release, kind).json()["operation_id"]
        out = await run_deployment(ports, org_id=b.w.org, deployment_id=op, health=health)
        return op, out

    r1, r2, r3 = [await cell_build(b, ports) for _ in range(3)]
    _, state = await deploy_through_the_cell(r1)
    assert state == "healthy"
    svc = run_emulator.services[service_name(b.w.preview)]
    (r1_revision,) = [t["revision"] for t in svc.traffic_statuses]
    second, state = await deploy_through_the_cell(r2)
    assert state == "healthy"
    (r2_revision,) = [t["revision"] for t in svc.traffic_statuses]

    run_emulator.unhealthy(image_of(b, r3))
    third, state = await deploy_through_the_cell(r3)
    assert (state, operation(b, third)["failure_code"]) == ("failed", HEALTH_CHECK_FAILED)
    assert pointer(b, b.w.preview) == second
    assert [(t["revision"], t["percent"]) for t in svc.traffic_statuses] == [(r2_revision, 100)]

    revisions = [r["name"] for r in svc.revisions]
    template = json.dumps(svc.body["template"], sort_keys=True)
    builds = rows_of(b.dsn, b.w.org, "select count(*) as n from ssc.build")
    build_calls = len(build_emulator.calls)
    run_emulator.calls.clear()
    clock.driver.clear()
    clock.health.clear()
    started = time.monotonic()
    rollback, state = await deploy_through_the_cell(r1, "rollback")
    elapsed = time.monotonic() - started

    assert state == "healthy"
    assert pointer(b, b.w.preview) == rollback
    assert [(t["revision"], t["percent"]) for t in svc.traffic_statuses] == [(r1_revision, 100)]
    assert len(build_emulator.calls) == build_calls
    assert rows_of(b.dsn, b.w.org, "select count(*) as n from ssc.build") == builds
    assert [r["name"] for r in svc.revisions] == revisions
    assert json.dumps(svc.body["template"], sort_keys=True) == template
    assert [m for m, _ in run_emulator.calls].count("PATCH") == 1
    assert clock.health == []
    assert len(run_emulator.calls) * 1.0 + sum(clock.driver) < ROLLBACK_LIMIT_SECONDS
    assert elapsed < ROLLBACK_LIMIT_SECONDS
    await runtime.aclose()
    await builder.aclose()
    await cloud_run.aclose()
    await cloud_build.aclose()


def test_the_health_window_outlasts_cloud_runs_startup_probe() -> None:
    assert HEALTH_TIMEOUT_SECONDS > STARTUP_PERIOD_SECONDS * STARTUP_FAILURES


def job_priority(b: Bench, queueing_lock: str) -> int:
    (row,) = rows_of(
        b.dsn,
        b.w.org,
        "select priority from procrastinate.procrastinate_jobs where queueing_lock = %s",
        queueing_lock,
    )
    return int(row["priority"])


async def test_a_rollback_job_goes_ahead_of_waiting_jobs(b: Bench) -> None:
    r1 = await build_release(b, b.w.preview)
    forward = start_deploy(b, b.w.preview, r1).json()["operation_id"]
    rollback = start_deploy(b, b.w.preview, r1, "rollback").json()["operation_id"]
    assert job_priority(b, f"dep:{forward}") == 0
    assert job_priority(b, f"dep:{rollback}") == ROLLBACK_PRIORITY
    assert worker.WorkerSettings().concurrency > 1


async def test_the_kill_switch_outranks_a_rollback(b: Bench) -> None:
    assert KILL_SWITCH_PRIORITY > ROLLBACK_PRIORITY > MANUAL_TIMER_PRIORITY > 0
    r1 = await build_release(b, b.w.preview)
    rollback = start_deploy(b, b.w.preview, r1, "rollback").json()["operation_id"]
    r = post(b, f"/v1/apps/{b.w.app}/kill-switch", {"mode": "disable"}, b.t.admin)
    assert r.status_code == 202, r.text
    assert job_priority(b, f"kil:{b.w.app}") == KILL_SWITCH_PRIORITY
    assert job_priority(b, f"dep:{rollback}") == ROLLBACK_PRIORITY


# ── the worker ───────────────────────────────────────────────────────────────


def test_the_worker_registers_the_deploy_tasks_and_ports() -> None:
    app = build_app("postgresql://ssc_app@localhost/ssc")
    assert {tasks.RUN_BUILD, tasks.RUN_DEPLOYMENT} <= set(app.tasks)
    base = {"SSC_DATABASE_DSN": "postgresql://ssc_app@localhost/ssc"}
    ports = compose_ports(base)
    assert isinstance(ports.prod_gate, ApprovalsProdGate)
    assert ports.cells is None
    assert isinstance(ports.metrics, NullMetricsPort)
    with pytest.raises(CompositionError, match="fake cells.FakeBuildDriver"):
        compose_ports({**base, "SSC_BUILD_DRIVER": "fake"})
    fake = compose_ports({**base, "SSC_BUILD_DRIVER": "fake", "SSC_ENV": "test"})
    assert isinstance(fake.cells, StaticCells)
    assert fake.cells.cell is not None
    assert isinstance(fake.cells.cell.build, FakeBuildDriver)
    assert fake.cells.cell.runtime is None
    engine = fake.engine
    with pytest.raises(CompositionError, match="unknown"):
        worker.cells_of({"SSC_BUILD_DRIVER": "cloudbuild"}, engine)
    jwks = {"keys": [{"kid": "k1", "kty": "EC", "crv": "P-256", "x": "AA", "y": "AA"}]}
    agent = {
        "SSC_RUNTIME_DRIVER": "cell_agent",
        "SSC_BUILD_DRIVER": "cell_agent",
        "SSC_CELLS": json.dumps({"cellabcd01": {"identity_jwks": jwks}}),
    }
    with pytest.raises(CompositionError, match="SSC_BLOB_"):
        worker.cells_of(agent, engine)
    signer = UrlSigner({"k1": MASTER}, active="k1", clock=SystemClock())
    store = FsBlobStore(Path("/nonexistent"), signer=signer, base_url="https://blobs.test")
    with pytest.raises(CompositionError, match="SSC_RUNTIME_DRIVER=cell_agent"):
        worker.cells_of({**agent, "SSC_RUNTIME_DRIVER": ""}, engine, store)
    with pytest.raises(CompositionError, match="SSC_CELLS"):
        worker.cells_of({**agent, "SSC_CELLS": ""}, engine, store)
    with pytest.raises(CompositionError, match="SSC_CELLS"):
        worker.cells_of({**agent, "SSC_CELLS": '{"Bad": {}}'}, engine, store)
    router = worker.cells_of(agent, engine, store)
    assert isinstance(router, CellRouter)
    assert router.labels == {"cellabcd01"}
    key = base64.b64encode(MASTER).decode()
    assert not isinstance(compose_ports({**base, "SSC_METRICS_KEY": key}).metrics, NullMetricsPort)
    with pytest.raises(CompositionError, match="base64"):
        compose_ports({**base, "SSC_METRICS_KEY": "not base64!"})
