# Driver matrix (SSC-005)

URL under test, unchanged for every driver:

`postgresql://app_<id>:<pw>@localhost:55418/app_<id>?sslmode=verify-full&sslrootcert=<abs path to ca.crt>`

| Driver | Version | Accepted URL as given | What was needed | Error text |
|---|---|---|---|---|
| psycopg | 3.3.6 | yes | nothing; sslrootcert query param honoured (libpq form) | - |
| asyncpg | 0.31.0 | yes | nothing; sslrootcert query param honoured | - |
| django | 6.1.1 | no | Django has no DATABASE_URL parser; URL split by hand into ENGINE/NAME/USER/PASSWORD/HOST/PORT/OPTIONS; sslmode/sslrootcert passed through OPTIONS and honoured | - |
| node-pg | 8.23.0 | yes | nothing; sslrootcert honoured from URL | - |
| drizzle-orm (node-postgres) | 1.0.0-rc.5-ab785fc | yes | nothing; URL passed straight to drizzle() | - |
| prisma (adapter-pg) | 7.10.0 / adapter-pg 7.10.0 | yes | Prisma 7 rejects url= in schema.prisma; URL must be given in code to PrismaPg adapter, unchanged; sslrootcert honoured (node-pg underneath); wrong CA rejected | - |
