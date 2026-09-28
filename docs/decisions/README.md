# Decision records (SSC-006)

One short file per decision: the choice, the reason, and what would make us reverse it. Filled in as SSC-001, SSC-002 and SSC-005 finish.

| # | Decision | Source | Status |
|---|---|---|---|
| 001 | Cloud and runtime | SSC-001 scorecard (`spikes/bakeoff/SCORECARD.md`) | pending bake-off |
| 002 | Login vendor and directory join key | SSC-002 | pending |
| 003 | App database connection string form | SSC-005 (`spikes/appdb/RESULTS.md`) | draft |
| 004 | Domains: `delimitus.com` platform hosts; apps on a separate Delimitus-branded domain with opaque labels | tickets §2 SSC-006 | decided 2026-09-28 |
| 005 | The seven decide-once rules | build path | decided 2026-09-28 |
| 006 | Assumptions A1 to A7 | tickets §1 | decided 2026-09-28 |
| 007 | Toolchain pins and the 7-day rule | SSC-007 | decided 2026-09-28 |
| 008 | Job queue: Procrastinate 3.10.0 core on Postgres 18, no DBOS fallback | SSC-004 (`packages/ssc_control/tests/test_jobqueue_crash.py`) | decided 2026-09-28 |

## 008 Job queue

Choice: Procrastinate 3.10.0, core package only (psycopg connector, no SQLAlchemy extra), on the control-plane Postgres 18.

Reason: the four SSC-004 crash tests pass in CI against a `postgres:18` container. Deferring with `task.configure(connection=conn)` rides the caller's transaction, so a job and its data row commit or roll back together. `queueing_lock` refuses a duplicate with `AlreadyEnqueued`. A worker killed with SIGKILL mid-job leaves the job in `doing` with a stale worker heartbeat; `job_manager.get_stalled_jobs(seconds_since_heartbeat=...)` finds it and `retry_job` returns it to `todo`, after which a fresh worker runs it again (attempts becomes 2). A duplicate defer inside `conn.transaction()` (a savepoint) leaves the outer transaction usable; without the savepoint the connection is in `InFailedSqlTransaction`.

Rules this imposes on SSC-016 and every reconciler: defer through the caller's connection, never a second connection; give every idempotent job a `queueing_lock`; wrap each defer in a savepoint; run a periodic task that sweeps `get_stalled_jobs` and retries, because 3.10 has no built-in `retry_stalled_jobs`; set `update_heartbeat_interval` and `stalled_worker_timeout` on workers and keep `seconds_since_heartbeat` above the heartbeat interval.

Reverse if: a later Procrastinate release drops the psycopg connector, or a job needs exactly-once side effects outside Postgres (then DBOS or an outbox pattern is re-evaluated).
