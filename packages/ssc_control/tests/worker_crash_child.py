"""The production worker (``build_app``) plus one test task, for test_worker's stalled-sweep test.

Run: python worker_crash_child.py DSN RUNS_FILE RELEASE_FILE. ``crashtest:slow`` appends
``<run_id> <pid>`` to RUNS_FILE, then blocks until RELEASE_FILE exists, so the test can SIGKILL the
process mid-job. The sweep runs every second and calls a worker dead after 2 s without heartbeat.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from procrastinate import Blueprint

from ssc_control.db import make_engine
from ssc_control.worker import Ports, WorkerSettings, build_app, run_worker

FAST = WorkerSettings(
    sweep_cron="* * * * * *",
    stalled_after_seconds=2.0,
    heartbeat_seconds=0.5,
    stalled_worker_timeout=2.0,
    polling_seconds=0.2,
    delete_jobs="never",
)


def crash_blueprint(runs: Path, release: Path) -> Blueprint:
    bp = Blueprint()

    @bp.task(name="slow")
    def slow(run_id: str) -> None:  # pyright: ignore[reportUnusedFunction]
        with runs.open("a") as f:
            f.write(f"{run_id} {os.getpid()}\n")
        deadline = time.monotonic() + 60
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

    return bp


async def main(dsn: str, runs: str, release: str) -> None:
    app = build_app(dsn, settings=FAST)
    app.add_tasks_from(crash_blueprint(Path(runs), Path(release)), namespace="crashtest")
    engine = make_engine(dsn)
    try:
        await run_worker(app, Ports(engine=engine), settings=FAST)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
