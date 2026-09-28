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
