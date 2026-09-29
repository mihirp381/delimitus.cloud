"""SSC-017 (B2): the job queue in schema ``procrastinate``, ``deferral.defer``, the worker, the
stalled-job sweep, and the reconciler running in a real worker against a real database.

Ticket "done when" checks:
  * vendored schema is Procrastinate 3.10.0's, byte for byte
                                  -> test_vendored_schema_is_procrastinate_3_10_0
  * the app role defers and a worker runs the job -> test_app_role_defers_and_a_worker_runs_it
  * a rolled-back transaction leaves no job       -> test_job_rides_the_callers_transaction
  * a duplicate queueing lock keeps the transaction usable
                                  -> test_duplicate_queueing_lock_keeps_the_transaction_usable
  * a SIGKILLed worker's job runs again           -> test_stalled_sweep_reruns_a_killed_workers_job
  * workers read org_index, then bind each org    -> test_workers_find_every_org_then_bind_each
  * a drifted service is repaired in seconds      -> test_running_worker_repairs_drift
  * a disabled app is not restarted               -> test_running_worker_never_starts_a_disabled_app
  * production period 15 s, 3 passes, under a minute
                                  -> test_default_timings_repair_within_a_minute
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.resources
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from procrastinate import App, PsycopgConnector
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from ssc_testkit import Dsns, make_org

import ssc_control.db
from ssc_contracts.ids import new_id
from ssc_contracts.manifest import default_manifest
from ssc_control.db import MIGRATE_ROLE, SqlState, all_org_ids, bind_org_sync, bound_org, upgrade
from ssc_control.deferral import DeferralError, defer
from ssc_control.runtime import jobs as runtime_jobs
from ssc_control.runtime.driver import service_name
from ssc_control.runtime.fake import FakeRuntimeDriver, changed
from ssc_control.runtime.specs import ReleaseSpec, StaticReleaseSpecs
from ssc_control.worker import (
    CompositionError,
    Ports,
    WorkerSettings,
    build_app,
    compose_ports,
    queue_conninfo,
    refuse_fakes,
    run,
    run_worker,
    runtime_driver_from_env,
)

CHILD = Path(__file__).with_name("worker_crash_child.py")
VENDORED = (
    Path(ssc_control.db.__file__).parent
    / "migrations"
    / "sql"
    / "vendor"
    / "procrastinate-3.10.0-schema.sql"
)
IMAGE = "sha256:" + "a" * 64
DRIFTED = "sha256:" + "d" * 64
FAST = WorkerSettings(
    tick_cron="* * * * * *",
    sweep_cron="* * * * * *",
    stalled_after_seconds=2.0,
    heartbeat_seconds=0.5,
    stalled_worker_timeout=2.0,
    polling_seconds=0.2,
)


def one(dsn: str, sql: str, *params: object) -> object:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def fresh_db(dsns: Dsns) -> Dsns:
    """A new database in the session's container, migrated to head, for tests that run a whole
    worker (its periodic tasks see every org in the database)."""
    name = f"w{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")

    def at(dsn: str) -> str:
        return make_url(dsn).set(database=name).render_as_string(hide_password=False)

    d = Dsns(at(dsns.superuser), at(dsns.migrate), at(dsns.app))
    upgrade(d.migrate)
    return d


# ── the schema and defer ─────────────────────────────────────────────────────


def test_vendored_schema_is_procrastinate_3_10_0() -> None:
    assert importlib.metadata.version("procrastinate") == "3.10.0"
    installed = importlib.resources.files("procrastinate") / "sql" / "schema.sql"
    assert VENDORED.read_bytes() == installed.read_bytes()


def queue_count(dsns: Dsns, where: str, *params: object) -> int:
    n = one(
        dsns.superuser,
        f"select count(*) from procrastinate.procrastinate_jobs where {where}",
        *params,
    )
    assert isinstance(n, int)
    return n


async def test_app_role_defers_and_a_worker_runs_it(dsns: Dsns) -> None:
    queue = f"q{uuid.uuid4().hex[:12]}"
    engine = ssc_control.db.make_engine(dsns.app)
    try:
        async with engine.begin() as conn:
            job_id = await defer(conn, "probe:echo", queueing_lock=None, queue=queue, word="hi")
    finally:
        await engine.dispose()
    assert isinstance(job_id, int)

    ran: list[str] = []
    app = App(connector=PsycopgConnector(conninfo=queue_conninfo(dsns.app)))

    @app.task(name="probe:echo", queue=queue)
    async def echo(word: str) -> None:  # pyright: ignore[reportUnusedFunction]
        ran.append(word)

    async with app.open_async():
        await app.run_worker_async(queues=[queue], wait=False, install_signal_handlers=False)
    assert ran == ["hi"]
    assert queue_count(dsns, "id = %s and status = 'succeeded'", job_id) == 1


async def test_job_rides_the_callers_transaction(dsns: Dsns) -> None:
    org = (await asyncio.to_thread(make_org, dsns.app, "Rides")).org_id
    lock = f"lock-{uuid.uuid4()}"
    group = (
        "insert into ssc.user_group (id, org_id, directory_ref, display_name) "
        "values (:id, :org, :ref, 'G')"
    )
    engine = ssc_control.db.make_engine(dsns.app)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            async with bound_org(engine, org) as conn:
                await conn.execute(text(group), {"id": new_id("grp"), "org": org, "ref": lock})
                assert await defer(conn, "probe:echo", queueing_lock=lock, ref=lock) is not None
                raise RuntimeError("boom")
        assert queue_count(dsns, "queueing_lock = %s", lock) == 0
        async with bound_org(engine, org) as conn:
            await conn.execute(text(group), {"id": new_id("grp"), "org": org, "ref": lock})
            assert await defer(conn, "probe:echo", queueing_lock=lock, ref=lock) is not None
        assert queue_count(dsns, "queueing_lock = %s", lock) == 1
        async with bound_org(engine, org) as conn:
            count = "select count(*) from ssc.user_group where directory_ref = :ref"
            assert (await conn.execute(text(count), {"ref": lock})).scalar_one() == 1
    finally:
        await engine.dispose()


async def test_duplicate_queueing_lock_keeps_the_transaction_usable(dsns: Dsns) -> None:
    lock = f"lock-{uuid.uuid4()}"
    path = text("select current_setting('search_path')")
    engine = ssc_control.db.make_engine(dsns.app)
    try:
        async with engine.begin() as conn:
            before = (await conn.execute(path)).scalar_one()
            first = await defer(conn, "probe:echo", queueing_lock=lock, word="a")
            assert (await conn.execute(path)).scalar_one() == before
            again = await defer(conn, "probe:echo", queueing_lock=lock, word="b")
            assert (await conn.execute(path)).scalar_one() == before
            assert (await conn.execute(text("select 1"))).scalar_one() == 1
        assert isinstance(first, int)
        assert again is None
        assert queue_count(dsns, "queueing_lock = %s", lock) == 1
        async with engine.begin() as conn:  # still refused in a later transaction
            assert await defer(conn, "probe:echo", queueing_lock=lock, word="c") is None
    finally:
        await engine.dispose()


async def test_defer_needs_an_open_transaction(dsns: Dsns) -> None:
    engine = ssc_control.db.make_engine(dsns.app)
    try:
        async with engine.connect() as conn:
            with pytest.raises(DeferralError):
                await defer(conn, "probe:echo", queueing_lock=None)
    finally:
        await engine.dispose()


def test_queue_conninfo_points_at_the_queue_schema_and_keeps_options() -> None:
    plain = psycopg.conninfo.conninfo_to_dict(queue_conninfo("postgresql://u@h/db"))
    assert plain["options"] == "-c search_path=procrastinate"
    kept = queue_conninfo("postgresql://u@h/db?options=-c%20statement_timeout%3D5000")
    assert psycopg.conninfo.conninfo_to_dict(kept)["options"] == (
        "-c statement_timeout=5000 -c search_path=procrastinate"
    )


# ── cross-org discovery ──────────────────────────────────────────────────────


async def test_workers_find_every_org_then_bind_each(dsns: Dsns) -> None:
    a = (await asyncio.to_thread(make_org, dsns.app, "Found A")).org_id
    b = (await asyncio.to_thread(make_org, dsns.app, "Found B")).org_id
    engine = ssc_control.db.make_engine(dsns.app)
    try:
        ids = await all_org_ids(engine)
        assert {a, b} <= set(ids)
        for org in (a, b):
            async with bound_org(engine, org) as conn:
                seen = (await conn.execute(text("select id from ssc.org"))).scalars().all()
            assert seen == [org]
        async with engine.connect() as conn:  # the index opens nothing else
            with pytest.raises(DBAPIError) as e:
                await conn.execute(text("select count(*) from ssc.environment"))
        assert getattr(e.value.orig, "sqlstate", None) == SqlState.NO_ORG_BOUND
    finally:
        await engine.dispose()


# ── the stalled-job sweep ────────────────────────────────────────────────────


def wait_for(pred: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


async def test_stalled_sweep_reruns_a_killed_workers_job(dsns: Dsns, tmp_path: Path) -> None:
    db = fresh_db(dsns)
    runs, release = tmp_path / "runs", tmp_path / "release"
    runs.touch()
    run_id = uuid.uuid4().hex
    engine = ssc_control.db.make_engine(db.app)
    try:
        async with engine.begin() as conn:
            job_id = await defer(conn, "crashtest:slow", queueing_lock=None, run_id=run_id)
    finally:
        await engine.dispose()

    def start() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, str(CHILD), db.app, str(runs), str(release)], cwd=CHILD.parent
        )

    def status() -> object:
        return one(
            db.superuser,
            "select status from procrastinate.procrastinate_jobs where id = %s",
            job_id,
        )

    first = start()
    try:
        await asyncio.to_thread(wait_for, lambda: len(runs.read_text().splitlines()) == 1)
        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=10)
    finally:
        if first.poll() is None:
            first.kill()
    assert status() == "doing"

    release.write_text("go")
    second = start()
    try:
        await asyncio.to_thread(wait_for, lambda: status() == "succeeded")
    finally:
        second.terminate()
        second.wait(timeout=15)
    lines = runs.read_text().splitlines()
    assert len(lines) == 2 and len({line.split()[1] for line in lines}) == 2
    assert (
        one(
            db.superuser,
            "select attempts from procrastinate.procrastinate_jobs where id = %s",
            job_id,
        )
        == 2
    )
    events = (
        "select count(*) from procrastinate.procrastinate_events "
        "where job_id = %s and type = 'deferred_for_retry'"
    )
    assert one(db.superuser, events, job_id) == 1
    sweeps = (
        "select count(*) from procrastinate.procrastinate_jobs "
        "where task_name = 'core:stalled_sweep' and status = 'succeeded'"
    )
    assert isinstance(n := one(db.superuser, sweeps), int) and n >= 1


# ── the reconciler in a running worker ───────────────────────────────────────


@dataclass(frozen=True)
class LiveEnv:
    org: str
    app: str
    env: str
    release: str

    @property
    def service(self) -> str:
        return service_name(self.env)


def live_env(dsn: str, name: str, *, status: str = "active") -> LiveEnv:
    """An org with one app whose prod environment points at a healthy deployment of IMAGE."""
    created = make_org(dsn, name)
    org, app, env, rel, dep = (
        created.org_id,
        new_id("app"),
        new_id("env"),
        new_id("rel"),
        new_id("dep"),
    )
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        conn.execute(
            "insert into ssc.app (id, org_id, slug, owner_user_id, status) "
            "values (%s, %s, %s, %s, %s)",
            (app, org, name.lower().replace(" ", "-"), created.admin_user_id, status),
        )
        conn.execute(
            "insert into ssc.environment (id, org_id, app_id, name) values (%s, %s, %s, 'prod')",
            (env, org, app),
        )
        conn.execute(
            "insert into ssc.release (id, org_id, app_id, number, image_digest, manifest_digest, "
            "source_digest, actor_kind, actor_id) values (%s, %s, %s, 1, %s, %s, %s, 'user', %s)",
            (
                rel,
                org,
                app,
                IMAGE,
                "sha256:" + "1" * 64,
                "sha256:" + "2" * 64,
                created.admin_user_id,
            ),
        )
        conn.execute(
            "insert into ssc.deployment (id, org_id, app_id, environment_id, release_id, kind, "
            "state, config_version, grants_version, actor_kind, actor_id, finished_at) "
            "values (%s, %s, %s, %s, %s, 'deploy', 'healthy', 1, 1, 'user', %s, now())",
            (dep, org, app, env, rel, created.admin_user_id),
        )
        conn.execute(
            "update ssc.environment set current_deployment_id = %s where id = %s", (dep, env)
        )
    return LiveEnv(org, app, env, rel)


def serving(driver: FakeRuntimeDriver, service: str) -> str | None:
    """The image digest with all of the service's traffic, if it serves and is not stopped."""
    svc = driver.services.get(service)
    if svc is None or svc.stopped:
        return None
    full = [r.image_digest for r in svc.revisions if svc.traffic.get(r.name) == 100]
    return full[0] if full else None


async def until(pred: Callable[[], bool], within: float) -> float:
    """Seconds until ``pred`` holds; fails after ``within``."""
    started = time.monotonic()
    while time.monotonic() - started < within:
        if pred():
            return time.monotonic() - started
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {within} s")


@asynccontextmanager
async def running_worker(
    db: Dsns, driver: FakeRuntimeDriver, envs: list[LiveEnv]
) -> AsyncIterator[None]:
    specs = StaticReleaseSpecs({e.release: ReleaseSpec(manifest=default_manifest()) for e in envs})
    engine = ssc_control.db.make_engine(db.app)
    ports = Ports(engine=engine, runtime_driver=driver, release_specs=specs)
    task = asyncio.create_task(
        run_worker(
            build_app(db.app, settings=FAST), ports, settings=FAST, install_signal_handlers=False
        )
    )
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await engine.dispose()


async def test_running_worker_repairs_drift(dsns: Dsns) -> None:
    db = fresh_db(dsns)
    a = await asyncio.to_thread(live_env, db.app, "Drift A")
    b = await asyncio.to_thread(live_env, db.app, "Drift B")
    driver = FakeRuntimeDriver()
    async with running_worker(db, driver, [a, b]):
        await until(lambda: serving(driver, a.service) == serving(driver, b.service) == IMAGE, 15)
        assert changed(driver.calls, a.service) == changed(driver.calls, b.service) == ["apply"]

        driver.reset_calls()
        driver.drift(a.service, image_digest=DRIFTED)  # a manual deploy behind our back
        assert serving(driver, a.service) == DRIFTED
        elapsed = await until(lambda: serving(driver, a.service) == IMAGE, 5)
        assert elapsed < 5
        assert changed(driver.calls, a.service) == ["set_traffic"]  # the old revision is kept

        driver.reset_calls()
        driver.delete_service(b.service)
        await until(lambda: serving(driver, b.service) == IMAGE, 5)
        assert changed(driver.calls, b.service) == ["apply"]

        driver.reset_calls()
        driver.drift(a.service, max_instances=8, stopped=True)
        await until(lambda: serving(driver, a.service) == IMAGE, 5)
        assert driver.services[a.service].max_instances == 2
        assert changed(driver.calls, a.service) == ["apply"]


async def test_running_worker_never_starts_a_disabled_app(dsns: Dsns) -> None:
    db = fresh_db(dsns)
    on = await asyncio.to_thread(live_env, db.app, "Up")
    off = await asyncio.to_thread(live_env, db.app, "Down", status="disabled")
    driver = FakeRuntimeDriver()
    async with running_worker(db, driver, [on, off]):
        await until(lambda: serving(driver, on.service) == IMAGE, 15)
        await until(
            lambda: sum(1 for m, s in driver.calls if s == off.service and m == "observe") >= 3, 10
        )
        assert off.service not in driver.services
        assert changed(driver.calls, off.service) == []

        with psycopg.connect(db.app) as conn:
            bind_org_sync(conn, on.org)
            conn.execute("update ssc.app set status = 'disabled' where id = %s", (on.app,))
        await until(lambda: driver.services[on.service].stopped, 5)
        driver.reset_calls()
        await until(
            lambda: sum(1 for m, s in driver.calls if s == on.service and m == "observe") >= 3, 10
        )
        assert changed(driver.calls, on.service) == []  # stays down, never re-applied


async def test_reconciler_without_a_driver_defers_nothing(dsns: Dsns) -> None:
    db = fresh_db(dsns)
    await asyncio.to_thread(live_env, db.app, "Idle")
    engine = ssc_control.db.make_engine(db.app)
    task = asyncio.create_task(
        run_worker(
            build_app(db.app, settings=FAST),
            Ports(engine=engine),
            settings=FAST,
            install_signal_handlers=False,
        )
    )
    try:
        done = (
            "select count(*) from procrastinate.procrastinate_jobs "
            "where task_name = 'runtime:reconcile_tick'"
        )
        await until(lambda: one(db.superuser, done) != 0, 10)
        await asyncio.sleep(1.5)
        assert (
            one(
                db.superuser,
                "select count(*) from procrastinate.procrastinate_jobs where task_name = %s",
                runtime_jobs.RECONCILE_ENV,
            )
            == 0
        )
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await engine.dispose()


# ── composition and timings ──────────────────────────────────────────────────


def test_build_app_registers_every_lane_task_and_can_be_built_twice() -> None:
    for _ in range(2):
        app = build_app("postgresql://ssc_app@localhost/ssc")
        assert {"core:stalled_sweep", "runtime:reconcile_tick", runtime_jobs.RECONCILE_ENV} <= set(
            app.tasks
        )


def periods(app: App, task_name: str) -> set[float]:
    ((periodic,),) = [
        [p for key, p in app.periodic_registry.periodic_tasks.items() if key[0] == task_name]
    ]
    ticks = [periodic.croniter.get_next(float, start_time=1_790_000_000.0)]
    for _ in range(12):
        ticks.append(periodic.croniter.get_next(float, start_time=ticks[-1]))
    return {round(b - a, 3) for a, b in zip(ticks, ticks[1:], strict=False)}


def test_default_timings_repair_within_a_minute() -> None:
    settings = WorkerSettings()
    app = build_app("postgresql://ssc_app@localhost/ssc", settings=settings)
    tick = periods(app, "runtime:reconcile_tick")
    assert max(tick) <= 15
    assert periods(app, "core:stalled_sweep") <= {30.0}
    # At most three passes converge any state (test_runtime), so a repair takes at most 45 s.
    assert 3 * max(tick) < 60
    assert settings.stalled_after_seconds > 2 * settings.heartbeat_seconds
    assert settings.stalled_after_seconds == 30 and settings.heartbeat_seconds == 5
    with pytest.raises(ValueError, match="heartbeats"):
        WorkerSettings(stalled_after_seconds=5, heartbeat_seconds=5)


def test_fakes_run_only_in_dev_and_test() -> None:
    ports = Ports(
        engine=ssc_control.db.make_engine("postgresql://ssc_app@localhost/ssc"),
        runtime_driver=FakeRuntimeDriver(),
    )
    for env in ({}, {"SSC_ENV": "prod"}, {"SSC_ENV": "staging"}):
        with pytest.raises(CompositionError, match="fake runtime_driver"):
            refuse_fakes(ports, env)
    for ok in ("dev", "test"):
        refuse_fakes(ports, {"SSC_ENV": ok})
    base = {"SSC_DATABASE_DSN": "postgresql://ssc_app@localhost/ssc"}
    with pytest.raises(CompositionError):
        compose_ports({**base, "SSC_RUNTIME_DRIVER": "fake"})
    assert isinstance(
        compose_ports({**base, "SSC_RUNTIME_DRIVER": "fake", "SSC_ENV": "test"}).runtime_driver,
        FakeRuntimeDriver,
    )
    assert compose_ports(base).runtime_driver is None
    with pytest.raises(CompositionError, match="unknown"):
        runtime_driver_from_env({"SSC_RUNTIME_DRIVER": "cloudrun"})


def test_worker_refuses_to_start_without_a_database() -> None:
    with pytest.raises(CompositionError, match="SSC_DATABASE_DSN"):
        asyncio.run(run({}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("SSC_")}
    done = subprocess.run(
        [sys.executable, "-m", "ssc_control.worker"],
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 2
    assert b"SSC_DATABASE_DSN" in done.stderr
