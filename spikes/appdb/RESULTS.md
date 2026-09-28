# SSC-005 pass/fail sheet

Date: 2026-09-28. Local: Docker `postgres:18`, TLS on, `hostssl`-only. Cloud: not run (no throwaway project yet).

| Check ("Done when") | Result | Evidence |
|---|---|---|
| Create 10 app databases | PASS (local) | `tests/test_provision.py`, 7 passed |
| Creation works from the cell-agent identity only | PENDING CLOUD | needs throwaway project; steps in README |
| `executeSql` limits (30 s, 10 MB, transactions) | PARTIAL | 10 MB and `partialResultMode` confirmed from the API doc; 30 s and transaction behaviour Unknown, to measure |
| No app role can connect to another app's database | PASS (local) | all 90 cross pairs: `permission denied for database` |
| App role cannot create databases or roles | PASS (local) | `InsufficientPrivilege` |
| Full certificate verification enforced | PASS (local) | wrong CA rejected; non-TLS refused by `pg_hba` |
| Driver table says which URL form to freeze | PASS | `drivers/RESULTS.md` |

## Driver table

| Driver | Version | Accepted URL as given | Needed |
|---|---|---|---|
| psycopg | 3.3.6 | yes | nothing |
| asyncpg | 0.31.0 | yes | nothing; wrong CA rejected |
| Django | 6.1.1 | no | no URL parser; give `PG*` parts too |
| node-pg | 8.23.0 | yes | nothing; wrong CA rejected |
| drizzle-orm (node-postgres) | 1.0.0-rc.5 | yes | nothing |
| Prisma (`@prisma/adapter-pg`) | 7.10.0 | yes | Prisma 7 refuses `url=` in schema; URL goes to the adapter in code, unchanged; wrong CA rejected |

## URL form to freeze

`postgresql://app_<id>:<pw>@<host>:<port>/app_<id>?sslmode=verify-full&sslrootcert=<absolute CA path>` plus `PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD/PGSSLMODE/PGSSLROOTCERT` for frameworks without a URL parser.

## Cloud run, 2026-09-28 (project `delimitus-0926`, instance `ssc-appdb-spike`, Cloud SQL Postgres 18, smallest tier; deleted the same day)

Runner: `cloud/run_cloud_checks.py`. Raw output: `cloud/RESULTS.json` (no passwords).

| Check | Result | Evidence |
|---|---|---|
| Creation works from the cell-agent identity only | PASS | cell-agent (roles/cloudsql.admin, IAM database user, member of `cloudsqlsuperuser`): 10 of 10 created. `null-identity` (no roles): executeSql 403, databases.insert 403. |
| Time to create one app database | 1.67 to 2.1 s | four API calls per database, see recipe below |
| No app role can connect to another app's database | PASS | own database: connected and created a table; other database: `FATAL: permission denied for database` |
| executeSql time limit | 30 s, confirmed | `pg_sleep(40)` → HTTP 400 "timed out due to a limit of 30 seconds"; `pg_sleep(20)` succeeds |
| executeSql result size | 10 MB, confirmed | 25 MB result → HTTP 400 unless `partialResultMode=ALLOW_PARTIAL_RESULT`, which returned 10043 rows of the 25000 |
| Semicolon-separated statements | One transaction | `CREATE ROLE ...; select 1/0` aborts everything, the role does not exist afterwards |
| `CREATE DATABASE` through executeSql | Not possible in a batch | "cannot run inside a transaction block"; use the Cloud SQL `databases.insert` API instead |
| SQL failures | HTTP 200 with `status.code` 3 | callers must check `status`, not only the HTTP code |
| Instance setting required | `settings.dataApiAccess = ALLOW_DATA_API` | default refuses executeSql with HTTP 400 |
| IAM database user privileges | Role attributes are not inherited | the IAM user must `SET ROLE cloudsqlsuperuser` in each batch; ownership transfer needs temporary membership in the app role |

### Recipe that works (feeds SSC-040)

1. executeSql, database `postgres`: `SET ROLE cloudsqlsuperuser; CREATE ROLE app_x LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 20 PASSWORD '...'`
2. Cloud SQL Admin API `databases.insert` `{"name": "app_x"}`, wait for the operation (about 0.2 to 1.5 s).
3. executeSql, database `app_x`: `SET ROLE cloudsqlsuperuser; REVOKE ALL ON SCHEMA public FROM PUBLIC; GRANT ALL ON SCHEMA public TO app_x`
4. executeSql, database `postgres`: `SET ROLE cloudsqlsuperuser; GRANT app_x TO cloudsqlsuperuser; ALTER DATABASE app_x OWNER TO app_x; REVOKE CONNECT ON DATABASE app_x FROM PUBLIC; REVOKE app_x FROM cloudsqlsuperuser`

Order matters: step 3 must run before step 4, because after `REVOKE CONNECT ... FROM PUBLIC` the cell agent itself can no longer connect to `app_x`. Later maintenance inside an app database (SSC-043 migration checks) therefore needs an explicit `GRANT CONNECT` to the cell agent, or must run as the app role.

### Decision for SSC-006

executeSql is usable for role and permission statements. Database creation goes through `databases.insert`. The fallback (Python connector over a private endpoint) is not needed for creation, but is still needed for anything that must hold a transaction open longer than 30 s or return more than 10 MB.
