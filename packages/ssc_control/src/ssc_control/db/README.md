# Control database (SSC-010)

The bookkeeping tables of the control plane, with customer separation enforced by Postgres.
Schema `ssc`, Postgres 18. Decision record: `docs/decisions/README.md` 009.

## Rules

1. **Every table carries `org_id`.** Foreign keys are `(org_id, id)` pairs, so a row can never
   point at another customer's row. Every table has `UNIQUE (org_id, id)` for that purpose.
2. **Row-level security is enabled and forced on every table.** One policy per table:
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
12. **Procrastinate lives in its own schema** (`procrastinate`), not in `ssc`, when SSC-016
    wires the worker. Its tables are not org-scoped and stay out of the RLS catalog check.
13. **Tables without a type-prefixed id** (`group_member`, `audit_event`, `audit_head`,
    `metrics_event`, `idempotency_claim`) are listed in `catalog.UNKEYED_TABLES`; they still
    carry `org_id` and forced RLS, they just have no `(org_id, id)` pair.

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
