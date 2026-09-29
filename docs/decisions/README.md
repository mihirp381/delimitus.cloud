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
| 009 | Control database rules: org-scoped tables, forced RLS, `SC001`, two roles, bounded PL/pgSQL | SSC-010 (`packages/ssc_control/src/ssc_control/db/`) | decided 2026-09-28 |

## 008 Job queue

Choice: Procrastinate 3.10.0, core package only (psycopg connector, no SQLAlchemy extra), on the control-plane Postgres 18.

Reason: the four SSC-004 crash tests pass in CI against a `postgres:18` container. Deferring with `task.configure(connection=conn)` rides the caller's transaction, so a job and its data row commit or roll back together. `queueing_lock` refuses a duplicate with `AlreadyEnqueued`. A worker killed with SIGKILL mid-job leaves the job in `doing` with a stale worker heartbeat; `job_manager.get_stalled_jobs(seconds_since_heartbeat=...)` finds it and `retry_job` returns it to `todo`, after which a fresh worker runs it again (attempts becomes 2). A duplicate defer inside `conn.transaction()` (a savepoint) leaves the outer transaction usable; without the savepoint the connection is in `InFailedSqlTransaction`.

Rules this imposes on SSC-016 and every reconciler: defer through the caller's connection, never a second connection; give every idempotent job a `queueing_lock`; wrap each defer in a savepoint; run a periodic task that sweeps `get_stalled_jobs` and retries, because 3.10 has no built-in `retry_stalled_jobs`; set `update_heartbeat_interval` and `stalled_worker_timeout` on workers and keep `seconds_since_heartbeat` above the heartbeat interval.

Reverse if: a later Procrastinate release drops the psycopg connector, or a job needs exactly-once side effects outside Postgres (then DBOS or an outbox pattern is re-evaluated).

## 009 Control database

Choice: one Postgres 18 database for the control plane, schema `ssc`, with customer separation enforced by the database itself. Full rules in `packages/ssc_control/src/ssc_control/db/README.md`; the list of PL/pgSQL functions in `PLPGSQL.md` there; the personal-data inventory in `PII.md` there.

- Every table carries `org_id`; foreign keys are `(org_id, id)` pairs. Row-level security is enabled and forced on all 18 tables. `ssc.current_org()` raises SQLSTATE `SC001` when no org is bound; it never returns NULL.
- The bind is `set_config('ssc.org', <org id>, true)`, once per unit of work, in the transaction. Never per statement (Delimitus paid 5 to 7 round trips for that), never per session (a pooled-connection leak). A test refuses any non-transaction-scoped `set_config` in the package.
- Two roles. `ssc_migrate` runs Alembic and owns every object. `ssc_app` is the application: `NOBYPASSRLS`, explicit per-table privileges written in `catalog.APP_ROLE_PRIVILEGES`, nothing on `ssc.alembic_version`. No `GRANT ... ON ALL TABLES` (Delimitus `003_rls.sql:113` gave the app role DELETE on the migration ledger that way).
- Our SQLSTATE class is `SC`: `SC001` no org bound, `SC002` last admin, `SC003` owner not active, `SC004` release immutable, `SC005` audit row immutable, `SC006` truncate refused, `SC007` schedule deleted.
- PL/pgSQL is a bounded exemption from the Python rule: six functions today, limit ten, listed in `PLPGSQL.md` and enforced by a catalog test. Everything else is Python.
- Releases and audit rows are immutable: no UPDATE or DELETE privilege, plus triggers that refuse the owner too, plus TRUNCATE guards. One deployment in flight per environment is a partial unique index. A deployment's release and environment must belong to the same app (composite FKs through `app_id`).
- Approvals: `decided_via_agent` is `CHECK (= false)` and an approved request's decider must differ from its requester. Only a person approves, never the requester.
- Table names `user_account` and `user_group`, because `user` and `group` are reserved words in Postgres.
- Org creation is admin-first and atomic (`db.orgs.create_org`), seeding the audit head with a 32-byte zero genesis hash. The `org.created` audit row joins that transaction when SSC-012 lands the writer.
- Audit actions and actor kinds are closed lists: a CHECK in the database and a `StrEnum` in `ssc_contracts.audit`, with a test that they match. Adding an action is an expand migration.
- Alembic runs from Python (`ssc_control.db.upgrade`), no `alembic.ini`, version table in schema `ssc`, expand-then-contract. Revision SQL lives in `migrations/sql/` and runs through the raw psycopg cursor because PL/pgSQL bodies contain `%s`.
- Procrastinate's tables go in their own schema (`procrastinate`) when SSC-011 wires the worker. They are not org-scoped and stay outside the RLS catalog check.

Reason: the product's central claim is per-customer isolation enforced at the data layer. Retrofitting RLS onto an existing schema is much harder than starting with it, and the Delimitus live test showed every footgun this closes (owner exempt under plain ENABLE, silent zero rows on a missing scope, over-broad grants, session-scoped binds). Tests in `packages/ssc_control/tests/test_control_db.py` attack the schema through raw SQL as `ssc_app` against a `postgres:18` container.

Reverse if: the control plane must shard across databases (then org routing replaces RLS as the first guard and RLS stays as the second), or the chosen cloud's managed Postgres cannot create non-superuser owner roles the way Cloud SQL does (then the two-role split is re-planned in SSC-013).
