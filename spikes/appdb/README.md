# SSC-005 spike: per-app database creation and driver check

Standalone uv project. Nothing here is product code.

## Run

```bash
./docker/start_pg.sh          # postgres:18, TLS on, hostssl-only pg_hba, 127.0.0.1:55418
uv sync
uv run pytest -q              # 7 tests: 10 app dbs, own-db connect, cross-connect refused, wrong CA, plain connection refused
./drivers/run_matrix.sh       # writes drivers/RESULTS.md (needs `npm install` in drivers/node once)
./docker/stop_pg.sh
```

`drivers/.env` holds the DATABASE_URL of the `app_drv` database; recreate it with the snippet in `drivers/run_matrix.sh` if the container was rebuilt.

## What was proven locally (2026-09-28)

- `appdb/provision.py` creates role `app_<id>` (LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOINHERIT, CONNECTION LIMIT 20), database `app_<id>` owned by it, `REVOKE CONNECT ... FROM PUBLIC`, `REVOKE ALL ON SCHEMA public FROM PUBLIC`. Ten databases created and dropped in the test.
- Every app role connects to its own database over `sslmode=verify-full` and can create tables.
- Every app role is refused on every other app database: `permission denied for database` (all 90 pairs).
- App roles cannot `CREATE DATABASE` or `CREATE ROLE`.
- A wrong CA fails with `certificate verify failed`. A non-TLS connection is refused by `pg_hba.conf` (only `hostssl` lines).
- Driver matrix in `drivers/RESULTS.md`: psycopg, asyncpg, node-pg, drizzle-orm and Prisma 7 (through `@prisma/adapter-pg`) all accept the URL unchanged and honour `sslrootcert` from the query string; node-pg, asyncpg and Prisma were also checked to reject a wrong CA. Django accepts the same values but has no URL parser, so the platform must also expose the parts (`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSSLMODE`, `PGSSLROOTCERT`).

## URL form to freeze

```
postgresql://app_<id>:<password>@<host>:<port>/app_<id>?sslmode=verify-full&sslrootcert=<absolute path to CA file>
```

Ship the CA file into the app container at a fixed path (for example `/etc/ssc/db-ca.crt`) and reference that path in the URL. Also set the `PG*` environment variables above for frameworks that cannot parse a URL. Password is `secrets.token_urlsafe(32)`, URL-encoded.

## Not testable locally, pending a throwaway GCP project

1. Cloud SQL `instances.executeSql`: the discovery doc states the 10 MB result limit and `partialResultMode`; the ticket's 30 s limit, transaction behaviour across `;`-separated statements, and the exact IAM permission are **Unknown** from the doc and must be measured. Client: `appdb/cloudsql_execute_sql.py` (`--dry-run` prints the request).
2. Creation from a cell-only service account, and that the control plane's identity is refused.
3. Cross-connect on a real Cloud SQL instance (same SQL, expected same result).

### Cloud run completed 2026-09-28

See `RESULTS.md`, section "Cloud run". The steps below were used (instance deleted afterwards). Prerequisites that the permission filter made the founder run by hand: service accounts and their role bindings, the `postgres` password, the IAM database user, and `GRANT cloudsqlsuperuser` to it. The instance also needs `--data-api-access ALLOW_DATA_API` and `cloudsql.iam_authentication=on`.

### Steps used (throwaway project, never `ristretto-506621`)

```bash
gcloud projects create ssc-bakeoff-appdb --set-as-default
gcloud billing projects link ssc-bakeoff-appdb --billing-account=<BILLING_ACCOUNT_ID>
gcloud services enable sqladmin.googleapis.com secretmanager.googleapis.com
gcloud sql instances create cell-pg --database-version=POSTGRES_18 --edition=ENTERPRISE --tier=db-f1-micro --region=us-central1 --no-assign-ip --network=default   # smallest tier; delete when done
gcloud iam service-accounts create cell-agent
gcloud projects add-iam-policy-binding ssc-bakeoff-appdb --member=serviceAccount:cell-agent@ssc-bakeoff-appdb.iam.gserviceaccount.com --role=roles/cloudsql.admin
gcloud sql users create cell-agent@ssc-bakeoff-appdb.iam --instance=cell-pg --type=cloud_iam_service_account
gcloud auth print-access-token --impersonate-service-account=cell-agent@ssc-bakeoff-appdb.iam.gserviceaccount.com
```

Then, with that token, run `uv run python appdb/cloudsql_execute_sql.py --project ssc-bakeoff-appdb --instance cell-pg --auto-iam-authn --statement "<the CREATE ROLE / CREATE DATABASE / REVOKE statements from provision.py>"` for 10 app ids, once with the cell-agent token (expect success) and once with a token that has no `cloudsql.*` role (expect 403). Record: time to create one database, the error returned for a statement that runs past 30 s (`select pg_sleep(40)`), whether `CREATE DATABASE; REVOKE` in one call works or fails as a transaction, and the response for a 20 MB result. Delete the project afterwards: `gcloud projects delete ssc-bakeoff-appdb`.

If `executeSql` is unusable: fallback is the Python provisioner over a cell-local private endpoint (the code in `provision.py` already runs over a normal connection).
