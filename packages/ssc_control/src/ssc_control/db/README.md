# Control database (SSC-010)

The bookkeeping tables of the control plane, with customer separation enforced by Postgres.
Schema `ssc`, Postgres 18. Decision record: `docs/decisions/README.md` 009.

## Rules

1. **Every table carries `org_id`** (one exception, `org_index`: rule 14). Foreign keys are `(org_id, id)` pairs, so a row can never
   point at another customer's row. Every table has `UNIQUE (org_id, id)` for that purpose.
2. **Row-level security is enabled and forced on every table in `catalog.TABLES`.** One
   policy per table:
   `org_id = ssc.current_org()`, for both reading (`USING`) and writing (`WITH CHECK`).
3. **`ssc.current_org()` raises `SC001` when no org is bound.** It never returns NULL.
4. **Bind once per unit of work.** `bound_org(engine, org_id)` opens a transaction and runs
   `set_config('ssc.org', org_id, true)`. The `true` makes the bind transaction-scoped; a
   session-scoped bind leaks into the next borrower of a pooled connection. A static test
   refuses any `set_config` in this package that is not transaction-scoped.
5. **Two roles.** `ssc_migrate` runs migrations and owns every object. `ssc_app` is the
   application: no ownership, `NOBYPASSRLS`, explicit per-table privileges listed in
   `catalog.APP_ROLE_PRIVILEGES`, and nothing at all on the migration ledger
   `ssc.alembic_version`. No `GRANT ... ON ALL TABLES`, ever.
6. **Ids are type-prefixed** (`org_`, `usr_`, `app_`, ...) and checked by a regex per table.
   See `ssc_contracts.ids`.
7. **Releases, audit rows and audit anchors are immutable.** No UPDATE or DELETE privilege for
   the app role, plus triggers that refuse the owner too, plus TRUNCATE guards.
8. **One deployment in flight per environment**, enforced by a partial unique index.
9. **PL/pgSQL is bounded.** The allowed functions are listed in `PLPGSQL.md` (limit 10).
10. **Personal data is inventoried** in `PII.md`.
11. **Org creation is admin-first and atomic**: `orgs.create_org` is the only supported path.
12. **Procrastinate lives in its own schema** (`procrastinate`), not in `ssc`: revision
    `0006_procrastinate_orgindex`, wired by SSC-017 (B2) in `ssc_control/worker.py`. Its tables
    are not org-scoped and stay out of the RLS catalog check; the app role's privileges on them
    are explicit per table in `catalog.QUEUE_APP_PRIVILEGES`, and a test compares them with the
    database. Jobs are deferred with `ssc_control.deferral.defer` on the caller's connection, in
    the caller's transaction.
13. **Tables without a type-prefixed id** (`group_member`, `audit_event`, `audit_head`,
    `metrics_event`, `idempotency_claim`, `access_snapshot`, `snapshot_ack`, `audit_anchor`) are
    listed in
    `catalog.UNKEYED_TABLES`; they still carry `org_id` and forced RLS, they just have no
    `(org_id, id)` pair.
14. **`ssc.org_index` is the single unscoped table** (decision 009 amendment), listed alone in
    `catalog.UNSCOPED_TABLES` and never in `catalog.TABLES`. Forced RLS makes a cross-org scan
    impossible, but workers must find every org (reconciler tick, audit anchors, timer re-arm).
    The table holds `org_id` (primary key, foreign key to `ssc.org`) and `created_at`, nothing
    else, and has no RLS. The app role may `SELECT` and `INSERT` it, never `UPDATE`, `DELETE`
    or `TRUNCATE`. `orgs.create_org` inserts the row in the org's creating transaction.
    Workers call `orgs.all_org_ids`, then do each org's work inside `bound_org`, under normal
    RLS. No `BYPASSRLS` role and no `SECURITY DEFINER` function exist for this. A second
    unscoped table needs its own decision.
15. **Advisory locks are named by class.** Class `21` is the org's access snapshot lock, taken
    as `(21, hashtext(org_id))`: shared by every transaction that changes what the gateway decides (`snapshot.service.mark_dirty`), exclusive by the compile
    (`snapshot.compiler.publish`), both transaction-scoped (decision 019). Class `12` is the
    org's audit anchor lock, `(12, hashtext(org_id))`, held exclusively by
    `audit.anchor.write_anchor` for its transaction (decision 012). A new class takes an unused
    number and is listed here.
16. **Every org has a cell label** (`org.cell_label`, founder default D1: app hosts are
    `<slug>[--preview].<cell_label>.<apps domain>`). Since revision 0011 the column's default
    generates it at insert, twelve letters from `bcdfghjkmnpqrstv` (48 random bits, no vowels,
    so never a word), and it is NOT NULL; 0001's "NULL until SSC-013" comment no longer holds.
    `orgs.create_org` returns it. It is unique and never derived from the customer's name.

## Migrations

Alembic, expand-then-contract. Every revision must be safe to run while the previous release
of the control plane is still serving: add columns and tables nullable or with defaults, back-fill,
deploy code that writes both shapes, then drop the old shape in a separate contract revision
after that code is out. Revisions so far, all pure expand:

| Revision | Ticket | Adds |
|---|---|---|
| `0001_control_schema` | SSC-010 | the 18 tables, roles' privileges, PL/pgSQL guards |
| `0002_idempotency` | SSC-011 | `ssc.idempotency_claim` (19th table): the `Idempotency-Key` ledger, keyed by org, credential and key, RLS and `SELECT, INSERT, UPDATE` for the app role |
| `0003_lane_vocab` | W0 | nine audit actions the lanes emit (`audit.exported`, `audit.reanchored`, `user.updated`, `schedule.updated`, `schedule.run_requested`, `bundle.stored`, `build.started`, `build.failed`, `release.created`) in the `audit_event_action_check` CHECK; downgrade restores the 0002 list |
| `0005_approvals` | SSC-045 | `environment.profile` (default `internal`, CHECK-limited); `approval_request` gains `environment_id` (FK, cascade), `subject_key`, the decision fields (`decision_reason`, `decision_channel`, `recorded_by_operator`, `policy_decision_id` FK), the four-kind CHECK, the named `approval_request_not_self` (replacing 0001's unnamed CHECK, found by its definition, and now covering denials), `approval_request_one_pending` and two lookup indexes. The table had no writer, so the NOT NULL columns take no default. Adding a foreign key checks existing rows with a query row-level security filters, which raises with no org bound, so the revision lifts `FORCE ROW LEVEL SECURITY` on the three tables inside its transaction and restores it; any revision adding a foreign key to a forced table does the same. Downgrade drops the new columns (development databases only) |
| `0006_procrastinate_orgindex` | SSC-017 | schema `procrastinate` with Procrastinate 3.10.0's own `schema.sql`, vendored byte for byte in `sql/vendor/` (a test compares it with the installed package) and run with `search_path` set to the new schema; explicit per-table grants to the app role (`catalog.QUEUE_APP_PRIVILEGES`). `ssc.org_index` (rule 14), back-filled from `ssc.org`: `FORCE ROW LEVEL SECURITY` binds the owner too, so the revision lifts it on `ssc.org` for the back-fill inside its transaction and restores it. Upgrading Procrastinate is a new revision applying its `sql/migrations/*.sql` the same way. Downgrade drops both (development databases only) |
| `0007_metrics_checks` | SSC-028 | CHECKs on `metrics_event`: `pseudonym` is 32 hex characters, `source_tool` the normalised tool name, `app_id` an app id, and `properties` one JSON object whose text holds no `usr_` id and no email address. The table had no writer, so every row passes; CHECK validation scans without row-level security, so FORCE stays on. Downgrade drops the four constraints |
| `0008_bundle` | SSC-014 | `ssc.bundle`: one row per uploaded source bundle, unique per `(org_id, app_id, digest)`, `pending` until complete has checked the stored object and then `stored` with the manifest read from the bundle (`bundle_stored_check`); forced RLS, `SELECT, INSERT, UPDATE` for the app role. Downgrade drops it (development databases only) |
| `0009_access_snapshot` | SSC-021 | `ssc.access_snapshot`: one row per published access snapshot, keyed `(org_id, version)`, with the object's `sha256:` digest and content-addressed key; `SELECT, INSERT` for the app role (a published version never changes). `ssc.snapshot_ack`: each org's cell and the version its latest heartbeat reported, keyed by org, with a foreign key to the published version; `SELECT, INSERT, UPDATE`. Both new and empty, forced RLS, in `UNKEYED_TABLES`. Downgrade drops both (development databases only) |
| `0010_build` | SSC-016 | `ssc.build`: one row per build of a stored bundle for one environment, `queued`, `running`, then `succeeded` with the one release it created (`build_release_check`, unique per release) or `failed` with a reason code (`build_failure_check`); `build_one_in_flight` allows one queued or running build per (environment, bundle); forced RLS, `SELECT, INSERT, UPDATE` for the app role. `deployment.failure_code`, a reason code allowed only on `failed` rows (`deployment_failure_check`); CHECK validation scans without row-level security, so FORCE stays on. Releases stay immutable. Downgrade drops both (development databases only) |
| `0011_audit_anchor` | SSC-012 | `ssc.audit_anchor`: one row per audit anchor written to the blob store (decision 012), keyed `(org_id, anchored_at)`, unique `(org_id, object_key)`, `reason` `daily` or `restore` with `restored_to` exactly on restore anchors; append-only (triggers `SC005`/`SC006`), forced RLS, `SELECT, INSERT` for the app role, in `UNKEYED_TABLES`. `org.cell_label` gains a generating default and becomes NOT NULL (rule 16); existing orgs are back-filled with `FORCE ROW LEVEL SECURITY` lifted on `ssc.org` inside the transaction, and the default serves the previous release's inserts too. `access_snapshot.content_digest` (nullable, `sha256:` CHECK) lets the stale sweep compare content. Downgrade drops the table and the digest and makes the label nullable without a default, keeping the labels (development databases only) |
| `0012_kill_switch` | SSC-025 | `ssc.kill_switch_run`: one row per pull of an app's kill switch, `disable` or `quarantine`, `running` then `completed` or `failed` (`finished_at` exactly when finished, `kill_switch_run_finished_check`), each step's state and timings in `steps` and the schedules it paused in `paused_schedule_ids`; `resumed_at` once enabling the app has resumed them, only on a finished run (`kill_switch_run_resumed_check`); `kill_switch_one_running` allows one running run per app (`KILL_SWITCH_IN_FLIGHT`); forced RLS, `SELECT, INSERT, UPDATE` for the app role. `metrics_event_last_used`, a partial index on `app_opened` events for the inventory's last use. Downgrade drops both (development databases only) |
| `0013_timers` | SSC-041 | `ssc.schedule` gains `path` (CHECK: `/` then printable ASCII, at most 512), `method` (`GET` or `POST`, default `POST`), `timeout_seconds` (1 to 900, default 60), `pause_reason` (CHECK-limited, set exactly when paused: `schedule_paused_has_reason`), `next_run_at` (set exactly when active: `schedule_active_is_armed`), `last_scheduled_for` and `declared_by_user_id` (FK to `user_account`). The table had no writer, so `path` and `declared_by_user_id` are NOT NULL without a default; the foreign key needs `FORCE ROW LEVEL SECURITY` lifted on `schedule` and `user_account` inside the transaction, as in 0005. `schedule_live_name` (unique name among schedules not deleted) replaces the plain unique name, so a deleted name may be declared again; `schedule_armed` indexes active schedules by instant. `ssc.timer_run`: one row per scheduled instant claimed or manual run asked for, with CHECKs tying `trigger`, `state`, `error`, `http_status` and the times together; `timer_run_one_running`, `timer_run_one_queued` (`TIMER_RUN_IN_FLIGHT`), `timer_run_once_per_instant`, `timer_run_history` and `timer_run_unfinished`; forced RLS, `SELECT, INSERT, UPDATE` for the app role. Downgrade deletes every schedule, drops the table and the columns and restores the plain unique name, so upgrading again finds `schedule` empty (development databases only) |

There is no `alembic.ini`. Run migrations from Python:

```python
from ssc_control.db import upgrade

upgrade("postgresql://ssc_migrate:...@host/ssc")  # as the migrator role
```

To add a revision, copy `versions/0001_control_schema.py`, give it the next number and a new
`revision` id, and put its SQL in `migrations/sql/`. There is no autogenerate: the SQL is the
source of truth, and every guard is a constraint or trigger that autogenerate would not see.

Revision SQL lives in `migrations/sql/` and is executed through the raw psycopg cursor, because
the PL/pgSQL bodies contain `%s` and `%I` that the DB-API layer would read as placeholders.

## Local setup

```python
import psycopg
from ssc_control.db import ensure_roles, upgrade

with psycopg.connect(superuser_dsn, autocommit=True) as conn:
    ensure_roles(conn)
    conn.execute("alter role ssc_migrate login password '...'")
    conn.execute("alter role ssc_app login password '...'")
    conn.execute("grant create on database ssc to ssc_migrate")
upgrade(migrate_dsn)
```

`tests/test_control_db.py` does exactly this against a `postgres:18` container and then attacks
the schema through raw SQL as the app role.
