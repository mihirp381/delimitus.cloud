"""SSC-004: prove Procrastinate 3.10 on Postgres 18 does not silently lose work.

1. A job deferred in the same transaction as a data row is saved or dropped together with it.
2. Deferring the same job twice (same queueing lock) is refused.
3. A SIGKILLed worker leaves a stalled job; get_stalled_jobs + retry_job runs it again.
4. A duplicate defer inside a savepoint does not poison the surrounding transaction.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from procrastinate import App, PsycopgConnector, SyncPsycopgConnector
from procrastinate.exceptions import AlreadyEnqueued
from testcontainers.postgres import PostgresContainer

WORKER = Path(__file__).with_name("jobqueue_worker.py")
QUEUE = "crash"


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    with PostgresContainer("postgres:18", driver=None) as pg:
        url = pg.get_connection_url()
        app = App(connector=SyncPsycopgConnector(conninfo=url))
        with app.open():
            app.schema_manager.apply_schema()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("create table orders (id serial primary key, ref text not null)")
            conn.execute(
                "create table job_runs (run_id text, pid int, at timestamptz default now())"
            )
        yield url


@pytest.fixture
def app(dsn: str) -> Iterator[App]:
    app = App(connector=SyncPsycopgConnector(conninfo=dsn))

    @app.task(name="slow_job", queue=QUEUE)
    def slow_job(run_id: str) -> None:
        pass

    @app.task(name="order_job", queue="orders")
    def order_job(ref: str) -> None:
        pass

    with app.open():
        yield app


def status_is(dsn: str, job_id: int, status: str) -> bool:
    sql = "select count(*) from procrastinate_jobs where id = %s and status = %s"
    return count(dsn, sql, job_id, status) == 1


def count(dsn: str, sql: str, *params: object) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


def wait_for(pred: object, timeout: float = 20.0) -> None:
    assert callable(pred)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


def test_enqueue_is_atomic_with_data_row(app: App, dsn: str) -> None:
    ref = f"ref-{uuid.uuid4()}"
    task = app.tasks["order_job"]
    with psycopg.connect(dsn) as conn:
        conn.execute("insert into orders (ref) values (%s)", (ref,))
        task.configure(connection=conn).defer(ref=ref)
        conn.rollback()
    assert count(dsn, "select count(*) from orders where ref = %s", ref) == 0
    assert count(dsn, "select count(*) from procrastinate_jobs where args->>'ref' = %s", ref) == 0

    with psycopg.connect(dsn) as conn:
        conn.execute("insert into orders (ref) values (%s)", (ref,))
        task.configure(connection=conn).defer(ref=ref)
        conn.commit()
    assert count(dsn, "select count(*) from orders where ref = %s", ref) == 1
    assert count(dsn, "select count(*) from procrastinate_jobs where args->>'ref' = %s", ref) == 1


def test_duplicate_enqueue_is_refused(app: App, dsn: str) -> None:
    lock = f"lock-{uuid.uuid4()}"
    task = app.tasks["order_job"]
    task.configure(queueing_lock=lock).defer(ref="a")
    with pytest.raises(AlreadyEnqueued):
        task.configure(queueing_lock=lock).defer(ref="a")
    assert count(dsn, "select count(*) from procrastinate_jobs where queueing_lock = %s", lock) == 1


async def test_sigkilled_worker_job_runs_again(app: App, dsn: str, tmp_path: Path) -> None:
    run_id = f"run-{uuid.uuid4()}"
    release = tmp_path / "release"
    job_id = app.tasks["slow_job"].configure().defer(run_id=run_id)

    def start_worker() -> subprocess.Popen[bytes]:
        return subprocess.Popen([sys.executable, str(WORKER), dsn, str(release)], cwd=WORKER.parent)

    first = start_worker()
    try:
        wait_for(lambda: count(dsn, "select count(*) from job_runs where run_id = %s", run_id) == 1)
        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=10)
    finally:
        if first.poll() is None:
            first.kill()
    assert status_is(dsn, job_id, "doing")

    async_app = App(connector=PsycopgConnector(conninfo=dsn))
    async with async_app.open_async():
        await asyncio.sleep(2.5)
        stalled = list(
            await async_app.job_manager.get_stalled_jobs(queue=QUEUE, seconds_since_heartbeat=2)
        )
        assert [j.id for j in stalled] == [job_id]
        await async_app.job_manager.retry_job(stalled[0])
    assert status_is(dsn, job_id, "todo")

    release.write_text("go")
    second = start_worker()
    try:
        wait_for(lambda: status_is(dsn, job_id, "succeeded"))
    finally:
        second.kill()
        second.wait(timeout=10)
    assert count(dsn, "select count(distinct pid) from job_runs where run_id = %s", run_id) == 2
    assert count(dsn, "select attempts from procrastinate_jobs where id = %s", job_id) == 2


def test_duplicate_enqueue_in_savepoint_does_not_poison_transaction(app: App, dsn: str) -> None:
    lock = f"lock-{uuid.uuid4()}"
    task = app.tasks["order_job"]
    task.configure(queueing_lock=lock).defer(ref="first")

    with psycopg.connect(dsn) as conn:
        conn.execute("insert into orders (ref) values (%s)", (lock + "-before",))
        with pytest.raises(AlreadyEnqueued):
            with conn.transaction():
                task.configure(connection=conn, queueing_lock=lock).defer(ref="dup")
        conn.execute("insert into orders (ref) values (%s)", (lock + "-after",))
        conn.commit()
    assert count(dsn, "select count(*) from orders where ref like %s", lock + "-%") == 2

    with psycopg.connect(dsn) as conn:
        with pytest.raises(AlreadyEnqueued):
            task.configure(connection=conn, queueing_lock=lock).defer(ref="dup")
        with pytest.raises(psycopg.errors.InFailedSqlTransaction):
            conn.execute("select 1")
