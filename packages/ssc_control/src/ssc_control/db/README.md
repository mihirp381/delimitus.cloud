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
7. **Releases and audit rows are immutable.** No UPDATE or DELETE privilege for the app role,
   plus triggers that refuse the owner too, plus TRUNCATE guards.
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
    `metrics_event`, `idempotency_claim`) are listed in `catalog.UNKEYED_TABLES`; they still
    carry `org_id` and forced RLS, they just have no `(org_id, id)` pair.
14. **`ssc.org_index` is the single unscoped table** (decision 009 amendment), listed alone in
    `catalog.UNSCOPED_TABLES` and never in `catalog.TABLES`. Forced RLS makes a cross-org scan
    impossible, but workers must find every org (reconciler tick, audit anchors, timer re-arm).
    The table holds `org_id` (primary key, foreign key to `ssc.org`) and `created_at`, nothing
    else, and has no RLS. The app role may `SELECT` and `INSERT` it, never `UPDATE`, `DELETE`
    or `TRUNCATE`. `orgs.create_org` inserts the row in the org's creating transaction.
    Workers call `orgs.all_org_ids`, then do each org's work inside `bound_org`, under normal
    RLS. No `BYPASSRLS` role and no `SECURITY DEFINER` function exist for this. A second
    unscoped table needs its own decision.

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
