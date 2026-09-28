"""Standalone Procrastinate worker for the SSC-004 crash test.

Run: python jobqueue_worker.py DSN RELEASE_FILE. The task records a run row, then blocks until
RELEASE_FILE exists so the test can SIGKILL the process mid-job.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import psycopg
from procrastinate import App, PsycopgConnector

QUEUE = "crash"
TASK_NAME = "slow_job"


def build_app(dsn: str) -> App:
    return App(connector=PsycopgConnector(conninfo=dsn))


def register(app: App, dsn: str, release_file: str) -> None:
    @app.task(name=TASK_NAME, queue=QUEUE)
    def slow_job(run_id: str) -> None:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "insert into job_runs (run_id, pid) values (%s, %s)", (run_id, os.getpid())
            )
        deadline = time.monotonic() + 60
        while not Path(release_file).exists() and time.monotonic() < deadline:
            time.sleep(0.1)


def main() -> None:
    dsn, release_file = sys.argv[1], sys.argv[2]
    app = build_app(dsn)
    register(app, dsn, release_file)
    with app.open():
        app.run_worker(
            queues=[QUEUE],
            wait=True,
            install_signal_handlers=False,
            fetch_job_polling_interval=0.2,
            update_heartbeat_interval=0.5,
            stalled_worker_timeout=2.0,
        )


if __name__ == "__main__":
    main()
