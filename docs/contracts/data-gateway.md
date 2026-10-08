# Data gateway v1 (`POST /v1/connections/{name}/query`)

How an app reads a database its org connected, through the cell's data gateway `ssc-datagw` (SSC-050, C19), and keeps files through its file broker ([Files](#files), SSC-046). One service per customer, in the cell, on Cloud Run at minimum 0 and request-billed; it leaves through Direct VPC egress and the cell NAT, so the customer's database sees the cell's one fixed address. Implementations: `ssc_datagw.server` (the pipeline), `ssc_datagw.workload` (the caller), `ssc_datagw.note` (the identity note), `ssc_datagw.admission` (the snapshot), `ssc_datagw.limits` (limits, budget, slots), `ssc_datagw.connectors` (the connector seam), `ssc_datagw.kinds` (which connector serves each kind, GA-5), `ssc_datagw.postgres` and `ssc_datagw.classify` (the Postgres connector, SSC-051), `ssc_datagw.mysql` (the MySQL connector, GA-5), `ssc_datagw.sqlserver` (the SQL Server connector, GA-5), `ssc_datagw.airtable` (the Airtable connector, GA-5), `ssc_datagw.snowflake` (the Snowflake connector, GA-5) and `ssc_datagw.rest` (the REST connector, GA-5) and `ssc_datagw.gsheets` (the Google Sheets connector, GA-5) and `ssc_datagw.s3` (the S3 connector, GA-5), `ssc_datagw.gcs` (the GCS connector, GA-5) and `ssc_datagw.bigquery` with `ssc_datagw.google` (the BigQuery connector and the service-account signer, GA-5). Cell wiring: `infra/README.md`, "Data gateway".

## Request

`POST /v1/connections/{name}/query`, where `name` is the connection's name in the org (`^[a-z][a-z0-9-]{0,62}$`, `connections` in `docs/contracts/access-snapshot.md`).

| Header | Rule |
|---|---|
| `Authorization` | `Bearer <Google ID token>`, minted by the app's own service account from the metadata server for the audience `SSC_DATAGW_AUDIENCE` (the data gateway's `run.app` URL), asked for with `format=full`. Required. Cloud Run's invoker check is off on this service; the data gateway checks the token itself. |
| `X-SSC-Identity` | Optional. The identity note the app received on the request it is answering, forwarded unchanged. Absent means the app acts for itself (`app_only`). |
| `X-Request-Id` | Optional. `[A-Za-z0-9._:-]{1,128}`; anything else is replaced by a fresh id. Echoed in the response header and body. |

The body is JSON, at most 1 MB, with no other member:

| Member | Type | Meaning |
|---|---|---|
| `sql` | string | One read-only statement, 1 to 100,000 characters. Parameters are `$1`, `$2`, ... |
| `params` | list | Up to 1,000 strings, integers, floats, booleans or nulls. Default empty. |
| `max_rows` | int ≥ 0 | Optional. Default 5,000. |
| `max_bytes` | int ≥ 0 | Optional. Default 10 MB. |
| `timeout_ms` | int ≥ 0 | Optional. No narrowing when absent. |

The three asks only narrow what the platform, the connection and the grant allow (Limits, below).

## Calling it from an app

`ssc_app.data.query(name, sql, params=(), *, max_rows=None, max_bytes=None, timeout_ms=None, identity=None)` (Python) and `query(name, sql, params, { maxRows, maxBytes, timeoutMs, identity })` of `@delimitus/ssc-data` (Node, SSC-052) make this request: they find the gateway from the metadata server (`SSC_DATAGW_URL` replaces it), send the app's workload token, forward `identity` as `X-SSC-Identity` when given, and return `columns`, `rows`, `row_count`, `truncated`, `truncated_reason` and `request_id`. A refusal is a `DataError` carrying the code below and, for `QUERY_FAILED`, the `sqlstate`. A call is tried once more on a lost connection or a 502, 503 or 504, not on a timeout. Which environments may call which connection is the grants of `ssc.connection_grant` (`docs/runbooks/ssc-052-first-connection.md`): the snapshot admits only `ready` connections.

## Response

`200`:

| Member | Meaning |
|---|---|
| `columns` | List of `{name, type, db_type}`. `type` is portable (`integer`, `decimal`, `string`, `timestamp`, ...); `db_type` is the database's own. |
| `rows` | List of rows, each a list in column order. |
| `row_count` | `len(rows)`. |
| `truncated` | `true` when the gateway stopped reading before the result ended. |
| `truncated_reason` | `max_rows`, `max_bytes`, `daily_rows`, `daily_bytes`, or `null`. The cap that cut the result. |
| `snapshot_version` | The access snapshot version that admitted the query. |
| `request_id` | As in `X-Request-Id`. |
| `elapsed_ms` | Time from receipt to answer. |

Values are exact JSON: a decimal is a string (never a float), a date, time or timestamp is ISO 8601, an interval is `PT<seconds>S`, bytes are base64, a UUID is its string, a non-finite float is its name (`nan`, `inf`), arrays and JSON values are nested JSON. A result is cut at a whole row: the row that would pass a cap is left out.

## Errors

Every refusal has one body, and the first check that refuses answers:

```json
{"error": {"code": "CONNECTION_SUSPENDED", "stage": "admission", "message": "...", "fix_owner": "admin"}, "request_id": "..."}
```

`sqlstate` is added for `QUERY_FAILED` when the database gave one. `message` is fixed per code; nothing from the statement, its parameters or the database's own message is returned.

| Code | Status | Stage | Fix owner | When |
|---|---|---|---|---|
| `BODY_TOO_LARGE` | 413 | request | app | The body is over 1 MB. |
| `UNAUTHENTICATED` | 401 | workload | app | No token, or not a Google-signed token for this audience from an app account of this cell. Checked before the body is read. |
| `UNAVAILABLE` | 503 | workload | platform | Google's keys could not be fetched and none are cached; also any unexpected failure (stage `execute`). |
| `DATA_SNAPSHOT_STALE` | 503 | admission | platform | No snapshot read confirmed in the last 120 s. |
| `UNKNOWN_ENVIRONMENT` | 403 | admission | platform | The caller's environment is not in the snapshot. |
| `APP_NOT_ACTIVE` | 403 | admission | admin | The app is `disabled` or `quarantined` (the kill switch). |
| `IDENTITY_REFUSED` | 401 | user | app | The forwarded note does not verify for the caller's environment, or its user is not active. |
| `CONNECTION_NOT_GRANTED` | 403 | admission | admin | No such connection, or not granted to the caller's environment: the two look the same. |
| `CONNECTION_SUSPENDED` | 403 | admission | admin | The connection is suspended. |
| `VALIDATION_FAILED` | 422 | request | app | The body is not a query as above, or not a file request ([Files](#files)). |
| `DAILY_BUDGET_SPENT` | 429 | limits | admin | The grant has used today's rows or bytes; resets at 00:00 UTC. |
| `CONCURRENCY_LIMIT` | 429 | limits | app | Every slot of the grant stayed taken for 2 s. |
| `QUERY_REFUSED` | 422 | classify | app | The connector's classifier refused the statement (not one plain read). |
| `QUERY_FAILED` | 422 | execute | app | The database refused the statement. |
| `QUERY_TIMEOUT` | 408 | execute | app | The read ran past `timeout_ms` plus a 2 s grace. |
| `CONNECTION_UNAVAILABLE` | 503 | execute | platform | The source could not be reached, the cell has no `SSC_CONNECTION_*` for it, or the session is not what it must be (see the Postgres connector). |

When a running query is ended because a newer snapshot no longer admits it, the answer is that snapshot's refusal (`APP_NOT_ACTIVE`, `CONNECTION_SUSPENDED`, `CONNECTION_NOT_GRANTED`, `UNKNOWN_ENVIRONMENT` or `DATA_SNAPSHOT_STALE`) with stage `execute`.

## The workload token

A Google ID token (RS256, keys from `https://www.googleapis.com/oauth2/v3/certs`, cached for an hour and fetched again at most every 30 s for an unknown `kid`) with `iss` `https://accounts.google.com` or `accounts.google.com`, `aud` exactly `SSC_DATAGW_AUDIENCE`, `exp`, `iat`, `sub`, `email_verified` true, and `email` `ssc-a-<20>@<cell project>.iam.gserviceaccount.com`, which names the environment `env_<20>`. The metadata server leaves `email` and `email_verified` out unless the app asks with `format=full` (`/computeMetadata/v1/instance/service-accounts/default/identity?audience=...&format=full`), and such a token is refused; `ssc_app.workload.WorkloadToken` asks correctly and caches the token until 5 minutes before it expires. Any service account anywhere can mint a token for the audience, so a token from another project, another account of the cell or a person is refused.

## The identity note

When present, `X-SSC-Identity` is verified as in `docs/contracts/identity-note.md`, with the cell JWKS (`SSC_IDENTITY_JWKS`), the cell issuer, and the caller environment's own origin as the audience (its host label from the snapshot's `hosts`). Its `org`, `app` and `env` must be the caller's. A `usr_` subject must be active in the snapshot; a schedule (`sch_`) note is logged as `schedule`. The note is optional, so the app can query for itself (`app_only`); a note that is present and does not verify is refused.

## Connectors by kind

Each `SSC_CONNECTION_CON_<20>` variable on the service (the connection id in upper case) is JSON `{kind?, ...}`: `kind` is one of `ssc_contracts.connections.KINDS` and, left out, `postgres`, the only kind before GA-5, so every value pinned before still reads. The kind picks the target model that validates the rest (`ssc_datagw.kinds.REGISTRY`; unknown members are refused, and a bad variable, or a kind this build has no connector for, stops the service at start, naming the variable and the fields, never the value) and the connector that serves the connection. The kinds a build serves are exactly the kinds the control plane of the same commit lets a customer create (`ssc_contracts.connections.AVAILABLE`); a kind the control plane names and a gateway does not is a build error, not a runtime one. Each kind's connector takes the same `Query`, yields rows the same way, and keeps the same promises: it refuses anything but a read before it reaches the source, redacts its credential from every log and error, maps its source's types to the portable types of [Response](#response), honours `timeout_ms` and `max_rows` plus one, and ends its read when the gateway cancels (`packages/ssc_datagw/tests/connector_suite.py` holds those promises as one suite every connector's tests run against its real source or a recorded fake of it).

## The Postgres connector

`{kind: "postgres", host, port, database, user, password, ca}` (`ssc_datagw.postgres.PostgresTarget`). The cell holds the value as the secret `ssc-conn-<20>`, written through the cell's secret intake and tagged `ssc-secret-kind=connection` when the agent creates it; the variable is a Cloud Run secret variable pinned to one version (the cell setting `datagw_connections`). The cell's deny rule refuses `ssc-data` every secret without that tag, so the service can read connection credentials and no app secret (`infra/README.md`, "Data gateway"). Each query gets its own connection and runs in this order; the first step that refuses answers:

1. **Classify.** sqlglot parses the text as Postgres; anything but exactly one `SELECT` (or `UNION`, `INTERSECT`, `EXCEPT` of them) is `QUERY_REFUSED`: a second statement, a data-changing `WITH`, `SELECT INTO`, `FOR UPDATE`/`SHARE`, `SET`, `NOTIFY`, `LISTEN`, `COPY`, `DO`, and any call to a function that writes, signals or ends other sessions, reads server files, or runs a query given as text (`query_to_xml` and the other `*_to_xml`, `ts_stat`, `ts_rewrite`, `pg_terminate_backend`, `pg_cancel_backend`, `pg_notify`, `set_config`, `nextval`, `dblink*`, `lo_*`, `pg_advisory*`, `pg_read_*`, `pg_ls_*`, and others: `ssc_datagw.classify.DENIED`). `E'...'` strings, dollar-quoted strings and `U&"..."` function names are refused, since sqlglot and Postgres can read them apart; text sqlglot cannot parse is refused.
2. **Connect over TLS.** With a pasted `ca`, the server's chain must lead to it and the name is not checked (`verify-ca`, as for a Cloud SQL certificate); without one, the system trust store and the host name decide (`verify-full`). There is no plaintext and no unverified mode. The startup packet sets `application_name=ssc-datagw`, `default_transaction_read_only=on`, `standard_conforming_strings=on` and `idle_in_transaction_session_timeout=60s`. 10 s to connect. For the first 60 s after an instance starts, a connect that times out is tried again (5 s per attempt, 1 s apart) until it connects or the query's deadline ends it: a new instance's calls through Cloud NAT may not connect for its first 20 to 37 s (`spikes/proofrun` T6). A refused connection, a TLS failure or a time-out after those 60 s is `UPSTREAM_UNAVAILABLE` at once.
3. **`BEGIN READ ONLY`, then read the session back** in one statement that also sets the transaction's `statement_timeout` (`timeout_ms`) and `application_name` (the query's tag, at most 63 bytes). `CONNECTION_UNAVAILABLE` when the backend pid differs from the one the server announced at startup or `default_transaction_read_only` did not land (a pooler is in between: PgBouncer in transaction mode and the like break the session guarantees and are refused; point the connection at the database or a replica), when the transaction is not read-only, or when the role is a superuser or may create objects or temporary tables.
4. **Prepare and read.** The statement is prepared, so Postgres itself refuses a second statement (42601), and read through a cursor 500 rows at a time, at most `max_rows` plus one, which is how the gateway knows to say `truncated`. Parameters bind by their placeholder types: a JSON string bound to `date`, `timestamp`, `timestamptz`, `time`, `timetz` or `uuid` is parsed as ISO 8601 or a UUID, a JSON number or string bound to `numeric` is an exact decimal; a wrong count is `QUERY_FAILED` 08P01, a value that does not parse 22P02. `json` and `jsonb` columns arrive as nested JSON.
5. **`ROLLBACK`**, always, and the connection is closed; nothing a read did is ever committed (a `NOTIFY` is delivered only at commit).

SQLSTATE 57014 (`statement_timeout`) is `QUERY_TIMEOUT`; classes 08, 53, 57P and 58 are `CONNECTION_UNAVAILABLE`; any other database error is `QUERY_FAILED` with its SQLSTATE. When the gateway cancels a read (the kill watch or its deadline) the connector ends the backend with `pg_terminate_backend` from a second connection as the same role (5 s at most), which may end only its own sessions.

**The role.** `packages/ssc_datagw/src/ssc_datagw/postgres_setup.sql` is the script the customer runs, as the database owner, on the primary: `psql "<dsn>" -v schemas=reporting,finance [-v relations=s.view] [-v role=ssc_datagw] -f postgres_setup.sql`. It makes a login role that is no superuser, creates nothing, inherits nothing, starts read-only, and may `SELECT` from the named schemas' tables and views (or the named relations) only. Postgres gives every role `TEMPORARY` (and, in a database first made before Postgres 15, `CREATE` on `public`) through `PUBLIC`; the script grants those to every role that exists, then revokes them from `PUBLIC`. It is safe to run again, which is how tables added later become readable. A read replica is preferred. The password is prompted for (`\password`, so only a SCRAM verifier reaches the server) unless `-v password=` is given.

What stops each attack, tested on Postgres 17 and 18 (`packages/ssc_datagw/tests/test_postgres.py`):

| Attack | Classifier | Database alone |
|---|---|---|
| `SET TRANSACTION READ WRITE` | refused | 25001: the transaction has already read |
| `CREATE TEMP TABLE` | refused | 25006 read-only transaction; 42501 for the role outside one |
| `SELECT 1; DELETE ...` | refused | 42601: a prepared statement holds one command |
| `query_to_xml('DELETE ...')`, `ts_stat('DELETE ...')` | refused | 0A000: the text runs read-only |
| `NOTIFY` | refused | runs, never delivered: the transaction rolls back |
| `pg_terminate_backend(pid)` | refused | 42501 for another role's session; the role's own sessions only by the classifier |

## The MySQL connector

`{kind: "mysql", host, port, database, user, password, ca}` (`ssc_datagw.mysql.MySqlTarget`; `port` defaults to 3306). `database` is the schema the session starts in, and the user must be able to read it: MySQL refuses the connection (1044) otherwise. The value is held, tagged and pinned as for Postgres. Each query gets its own connection and runs in this order; the first step that refuses answers:

1. **Classify.** sqlglot parses the text as MySQL (`ssc_datagw.classify.MYSQL`); anything but exactly one `SELECT` (or `UNION`, `INTERSECT`, `EXCEPT` of them) is `QUERY_REFUSED`, as for Postgres: a second statement, `SELECT ... INTO` (a variable, `OUTFILE`, `DUMPFILE`), `FOR UPDATE`/`FOR SHARE`, `SET`, `SHOW`, `DO`, `CALL`, and any call to a function on `ssc_datagw.classify.MYSQL_DENIED` or with a denied prefix: functions that read server files (`LOAD_FILE`), hold locks other sessions wait on (`GET_LOCK` and kin), wait on replication, burn time on purpose (`BENCHMARK`, `SLEEP`: `SLEEP` returns 1 when interrupted, so a read that sleeps would end quietly instead of as `QUERY_TIMEOUT`), or reach outside the server (`sys_*` and the other plugin prefixes). sqlglot and MySQL read backslash escapes alike, so no string form is refused; text sqlglot cannot parse is refused.
2. **Connect over TLS**, with the same CA rules as Postgres (a pasted `ca`: the chain must lead to it and the name is not checked; none: the system trust store and the host name decide; no plaintext, no unverified mode). The handshake sets `program_name=ssc-datagw` and `utf8mb4`. 10 s to connect, with the same 60 s warm-up retry. A refused connection, a TLS failure, a bad password or a time-out after the warm-up is `UPSTREAM_UNAVAILABLE` at once.
3. **Settle the session and read it back.** One `SET SESSION` sets `max_execution_time` (`timeout_ms`), `transaction_read_only = 1`, `time_zone = '+00:00'` and a fixed `sql_mode` (`ssc_datagw.mysql.SQL_MODE`, MySQL 8.0's default: never `ANSI_QUOTES`, never `NO_BACKSLASH_ESCAPES`); then `START TRANSACTION READ ONLY`; then one `SELECT` reads the session back. `CONNECTION_UNAVAILABLE` when `CONNECTION_ID()` differs from the thread id the server announced at the handshake (a pooler or proxy is in between, such as ProxySQL multiplexing; point the connection at the database or a replica), when the transaction is not read-only, when the timeout, the `sql_mode` or UTC did not land, when a role is active (`CURRENT_ROLE()` is not `NONE`), or when the user holds a global privilege or any schema, table or column privilege other than `SELECT`.
4. **Bind and read.** MySQL has no prepared statement that streams, so the connector binds: each `?` placeholder that sqlglot's MySQL tokenizer finds (a `?` inside a string or a comment is not one) is replaced by its parameter as a literal the driver escapes (`TRUE`/`FALSE` for a boolean, `NULL` for null). This is safe because the classifier approved the text with the placeholder where a value stands, and the session's `sql_mode` was set and read back without `NO_BACKSLASH_ESCAPES`, in `utf8mb4`, so the driver's backslash escaping is what the server reads. A wrong count is `QUERY_FAILED` 07001; `%` is never a format directive. The statement carries the query's tag in a leading comment, `/* <tag> */` (at most 64 characters, anything that could end the comment dropped), which the server's process list and slow log show the customer's DBA. The rows stream over the text protocol 500 at a time, at most `max_rows` plus one. `TINYINT(1)` is `boolean`, `TIMESTAMP` arrives in UTC, `DATETIME` stays without a zone, `TIME` is an `interval`, `JSON` arrives as nested JSON, `BIT` as an integer, binary strings and `GEOMETRY` as bytes.
5. **Close.** The connection is closed without a commit, which rolls the read-only transaction back.

Error 3024 (`max_execution_time` ran out) is `QUERY_TIMEOUT`; the driver's connection numbers (2002, 2003, 2006, 2013) and the server's (1040, 1053, 1077, 1152, 1159, 1160, 1161) are `CONNECTION_UNAVAILABLE`; any other database error is `QUERY_FAILED` with the SQLSTATE its number stands for (the driver surfaces the number only, `ssc_datagw.mysql.SQLSTATE`): 42000 for a privilege the user lacks (1044, 1142, 1143, 1227, 1370), a syntax error (1064) or an unknown function (1305); 42S02 no such table; 42S22 no such column; 25006 a write in a read-only transaction; 25001 a change of the transaction; 22003 out of range; 22032 invalid JSON; 70100 a read interrupted by a `KILL QUERY` from outside the gateway (a DBA). The message names the error class and number only, never the server's text, which quotes names and values. When the gateway cancels a read (the kill watch or its deadline) the connector runs `KILL QUERY <id>` from a second connection as the same user (5 s at most), which may stop only its own sessions' statements.

**The user.** `packages/ssc_datagw/src/ssc_datagw/mysql_setup.sql` is the script the customer runs with the `mysql` client, on the primary of MySQL 8.0 or later, as an admin, naming a database for the session: `mysql -h <host> -u admin -p --ssl-mode=VERIFY_IDENTITY reporting`, then `SET @schemas = 'reporting,finance'; SET @password = '...'; source mysql_setup.sql` (or the same variables through `--init-command` and `< mysql_setup.sql`, which puts the password in the process list and the shell's history). `@user` defaults to `ssc_datagw`. It makes `@user@'%'` with that password, `REQUIRE SSL` and at most 20 sessions, revokes every privilege and every role it held, grants `SELECT` on each named schema, and sets no default role; it fails without `@password` or `@schemas`. The work runs in a temporary procedure, `ssc_setup`, made `SQL SECURITY INVOKER` in the session's database and dropped at the end, since MySQL runs `IF` and `SIGNAL` only in a stored program. It is safe to run again, which is how a new password or another schema lands; other users keep their grants. A read replica is preferred.

What stops each attack: local-proven on MySQL 8.4 and 9 (`tests/test_mysql.py`, in `packages/ssc_datagw`). The classifier column is tested for every row; the database-alone column is tested for the rows with an error number and the multi-statement row, and the rows that say "runs" were observed on MySQL 8.4 with the same user:

| Attack | Classifier | Database alone |
|---|---|---|
| `SET TRANSACTION READ WRITE` | refused | 25001 (1568): the transaction has started |
| `CREATE TEMPORARY TABLE` | refused | 25006 (1792): the transaction is read-only |
| `DELETE`, `INSERT` | refused | 42000 (1142): the user has `SELECT` only |
| `SELECT 1; DELETE ...` | refused | the second statement runs: the driver always sends `MULTI_STATEMENTS`. The connector answers the first result only, and the `DELETE` meets the same `SELECT`-only user and read-only transaction and changes nothing. Only the classifier holds a text to one statement |
| `SELECT ... FOR UPDATE` | refused | 42000 (1142) |
| `SELECT ... INTO OUTFILE` | refused | 42000 (1227): no `FILE` privilege |
| `SELECT LOAD_FILE(...)` | refused | runs and returns `NULL`: no `FILE` privilege |
| `GET_LOCK`, `SLEEP`, `DO`, `SHOW` | refused | run; the lock is the session's and ends with it, a sleep ends at `max_execution_time`, `SHOW` lists only what the user may read |
| a schema not granted (`secret.payroll`) | passes (a read) | 42000 (1142) |
| a role granted to the user | `SET ROLE` refused | the read-back refuses an active role; `mysql_setup.sql` revokes every role |

<!-- rest -->
## The REST connector

`{kind: "rest", base_url, token?, header?, scheme?, items?, ca?}` (`ssc_datagw.rest.RestTarget`; unknown members are refused), the value of a `rest` connection's `SSC_CONNECTION_*` variable ([Connectors by kind](#connectors-by-kind)).

| Member | Rule |
|---|---|
| `base_url` | `https://<host>[:<port>][/<path>]`, at most 2,000 characters, no query or fragment. The gateway target may carry a port; the control plane address (`ssc_contracts.connections.RestAddress`) does not yet. A trailing `/` is dropped. |
| `token` | Optional. 1 to 4,096 visible ASCII characters (no space, no control character). Never shown in a repr, an error or a log line, not even the error that refuses it. |
| `header` | The header the token travels in, default `Authorization`; `^[A-Za-z0-9-]{1,64}$`, and never `Host`, `Accept`, `User-Agent`, `X-SSC-Query`, `Content-Length`, `Transfer-Encoding`, `Connection` or `Cookie` (any case). |
| `scheme` | Default `Bearer`; `^[A-Za-z0-9-]{0,32}$`. The header's value is `<scheme> <token>`, or the bare token when `scheme` is empty. |
| `items` | Optional dotted path of object keys (`^[A-Za-z0-9_.-]{1,200}$`, e.g. `data.orders`) to the records in the answer; left out, the body itself. |
| `ca` | As for Postgres: with it the chain must lead to it and the host name is not checked; without it the system trust store and the host name decide. There is no plain `http` and no unverified mode. |

**The request.** For this kind `sql` is not SQL: it is one GET path, with an optional query string, appended to `base_url`. It must start with one `/` (not `//`), be at most 2,000 characters, all visible ASCII (no whitespace, no control or non-ASCII character), with no `\` and no `#`, and its path part (before `?`) must have no `..` segment, also after percent-decoding (`%2e%2e`, `%2f..%2f`). Anything else is `QUERY_REFUSED` ("the request is not a GET path"), and so are `params`, which a REST read does not take; both are refused before anything is sent. So a read stays on the base's host and under its path. The request is `GET` only, sends `Accept: application/json`, `User-Agent: ssc-datagw`, `X-SSC-Query: <tag>` and the credential header when a token is set, follows no redirect, and ignores the process environment's proxy and CA settings.

**Time.** 10 s to connect, with the warm-up retry of the Postgres connector (a connect that times out in an instance's first 60 s is tried again). Each read of the answer has `timeout_ms`, and the whole read, connect and retries included, ends at `timeout_ms`: past it is `QUERY_TIMEOUT`. When the gateway cancels a read (the kill watch or its deadline) the request is dropped and its connection closed.

**The answer.**

| Answer | Result |
|---|---|
| 2xx | read on |
| 3xx | `QUERY_FAILED`, no `sqlstate` (no redirect is followed) |
| 401, 403 | `QUERY_FAILED` 28000 (the source refused the credential) |
| 404 | `QUERY_FAILED` 42P01 (no such path) |
| 429 | `CONNECTION_UNAVAILABLE` (the source is rate limiting) |
| other 4xx | `QUERY_FAILED`, no `sqlstate` |
| 5xx | `CONNECTION_UNAVAILABLE` |
| no connection, a TLS failure | `CONNECTION_UNAVAILABLE` |
| a body over 32 MiB (decoded) | `QUERY_FAILED`, no `sqlstate`; reading stops at the cap |
| a body that is not JSON | `QUERY_FAILED` 22P02 |
| a body its `Content-Encoding` cannot decode | `QUERY_FAILED` 22P02 |

**Records and columns.** The value at `items` (each key into an object; a missing key or a value that is not an object on the way is `QUERY_FAILED` 42P01) is the records: an array is one record per element, an object one record, anything else `QUERY_FAILED` 22P02. When the first record is an object its keys, in order, are the columns; a later record's missing key is `null`, a key that is not a column is dropped, and a later record that is not an object is a row of `null`. When the first record is not an object there is one column, `value`, holding each record as it is. No records is no columns and no rows. A column's type is that of its first non-null value in the first 100 records: boolean `boolean`, integer `integer`, other number `float`, string `string`, array or object `json`, none `string`; `db_type` is the JSON type (`boolean`, `number`, `string`, `array`, `object`, `null`). A value of another type than its column's is kept as it is. At most `max_rows` plus one rows are read, as for every connector.

The connector logs one line per read, `rest read: status=<status> bytes=<body bytes>`, with no URL. Local-proven against a TLS server in-process (`packages/ssc_datagw/tests/test_rest.py`), which also runs the connector suite.

<!-- gsheets -->
## The Google Sheets connector

`{kind: "gsheets", spreadsheet_id, sheet?, service_account}` (`ssc_datagw.gsheets.GsheetsTarget`; unknown members are refused), the value of a `gsheets` connection's `SSC_CONNECTION_*` variable ([Connectors by kind](#connectors-by-kind)). The address is the control plane's (`ssc_contracts.connections.SheetsAddress`), with the same patterns.

| Member | Rule |
|---|---|
| `spreadsheet_id` | `^[A-Za-z0-9_-]{20,128}$`, the id in the spreadsheet's URL. |
| `sheet` | Optional. The one tab apps read, `^[^\x00-\x1f'!:]{1,100}$`; left out, every tab. |
| `service_account` | The service account's JSON key file, as Google gives it. It must be JSON with `client_email`, `private_key` (a PEM RSA key) and `private_key_id`, or the variable is refused with a message that quotes none of them. Never shown in a repr, an error or a log line, nor are the email, the key or a JWT made from it. |

**Who reads.** Each read signs its own JWT with the service account's key, Google's self-signed JWT for a service account (no token endpoint, no cache): RS256, header `kid` the `private_key_id`, `iss` and `sub` the `client_email`, `aud` `https://sheets.googleapis.com/`, `iat` now and `exp` an hour later, sent as `Authorization: Bearer <jwt>`. The service account needs no role in any project; the customer shares the spreadsheet with its email, and viewer is enough. Turning the Sheets API on in the service account's project is the customer's step.

**The range.** For this kind `sql` is not SQL: it is one A1 range, at most 300 characters, in one of these forms:

| Form | Example |
|---|---|
| a cell, or cell to cell | `A1`, `A1:D100` |
| a cell to the end of a column | `A2:D` |
| whole columns | `A:D` |
| whole rows | `1:100` |
| any of these after a tab | `Orders!A1:D`, `'Q1 sales'!B2` |
| a tab alone (all of it) | `Orders`, `'Q1 sales'` |

A column is 1 to 3 letters (any case), a row 1 to 9,999,999. A tab name is bare when it is letters, digits and `_`, else quoted in `'...'` with `''` for a quote, 1 to 100 characters either way. A bare text that reads as a range is a range: quote a tab named like one (`'A1'`). With `sheet` set, a range without a tab reads that tab, and a named tab must be it (compared without case; sent as `sheet` spells it); another is `QUERY_REFUSED` ("the range names another sheet than the connection's"). Without `sheet`, a range without a tab reads the spreadsheet's first tab. Anything else is `QUERY_REFUSED` ("the query is not an A1 range"), and so are `params`, which a Sheets read does not take; both are refused before anything is sent.

The request is `GET https://sheets.googleapis.com/v4/spreadsheets/<spreadsheet_id>/values/<range>?valueRenderOption=UNFORMATTED_VALUE&dateTimeRenderOption=SERIAL_NUMBER&majorDimension=ROWS`, the range percent-encoded and its tab always quoted, with `Accept: application/json` and `User-Agent: ssc-datagw`. TLS is checked against the system trust store, no redirect is followed, and the process environment's proxy and CA settings are ignored. Time is as for the REST connector (the same request code, `ssc_datagw.rest.get`): 10 s to connect with the warm-up retry, the whole read ends at `timeout_ms`, and a cancelled read drops its request.

**The answer.**

| Answer | Result |
|---|---|
| 2xx | read on |
| 3xx | `QUERY_FAILED`, no `sqlstate` |
| 400 | `QUERY_FAILED` 42P01 (no such tab or range: the grammar was already checked) |
| 401 | `QUERY_FAILED` 28000 (Google refused the JWT: a revoked or wrong key) |
| 403 | `QUERY_FAILED` 42501 (the spreadsheet is not shared with the service account, or its project has the Sheets API off) |
| 404 | `QUERY_FAILED` 42P01 (no such spreadsheet) |
| 429 | `CONNECTION_UNAVAILABLE` (Google's quota) |
| other 4xx | `QUERY_FAILED`, no `sqlstate` |
| 5xx, no connection, a TLS failure | `CONNECTION_UNAVAILABLE` |
| a body over 32 MiB (decoded) | `QUERY_FAILED`, no `sqlstate` |
| a body that is not JSON, or not `{"values": [[cell, ...], ...]}` with each cell a string, number or boolean | `QUERY_FAILED` 22P02 |

**Records and columns.** Values are unformatted: a number is a JSON number, a date or time Google's serial number (days since 1899-12-30), a formula its result. The first row of `values` is the header: a cell's text names its column (a number or boolean its JSON text), an empty cell `col<n>` by its 1-based position, and a name already taken gets `_2`, `_3`, ... The rows after it are the records: a short row is padded with `null`, a longer one cut at the column count, and an empty cell (Google's `""`) is `null`. No `values` (an empty tab or range) is no columns and no rows; a header alone is columns and no rows. A column's type is that of its first non-null value in the first 100 rows after the header: boolean `boolean`, integer `integer`, other number `float`, string `string`, none `string`; `db_type` is `boolean`, `number` or `string`. A value of another type than its column's is kept as it is. At most `max_rows` plus one rows are read, as for every connector; the answer itself is one request, so Google's own limits (a request's size and the per-minute quota) apply first.

The connector logs one line per read, `gsheets read: status=<status> bytes=<body bytes>`, with no id, range or URL. Contract-fake-proven: a fake Sheets API that checks the JWT (`packages/ssc_datagw/tests/test_gsheets.py`, over TLS in-process, which also runs the connector suite); live proof is a GA-5 C step.

<!-- s3 -->
## The S3 connector

`{kind: "s3", bucket, region, prefix?, access_key_id, secret_access_key, endpoint?, ca?}` (`ssc_datagw.s3.S3Target`; unknown members are refused), the value of an `s3` connection's `SSC_CONNECTION_*` variable ([Connectors by kind](#connectors-by-kind)). The address is the control plane's (`ssc_contracts.connections.S3Address`), with the same patterns.

| Member | Rule |
|---|---|
| `bucket` | `^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$`. |
| `region` | `^[a-z]{2}(-[a-z]+)+-\d$`, the bucket's region (`eu-west-2`). |
| `prefix` | Default `""`; `^[^\x00-\x1f]{0,512}$`. Every key read or listed starts with it, as text: `exports` also covers `exports-old/`, so end it with `/` to mean a folder. |
| `access_key_id` | `^[A-Z0-9]{16,128}$`, the IAM user's access key id. |
| `secret_access_key` | The access key's secret. Never shown in a repr, an error or a log line, not even the error that refuses the target; nor is the signing key or any `Authorization` value made from it. |
| `endpoint` | Optional. `https://<host>[:<port>]`, no path, for an S3-compatible store, addressed path-style (`<endpoint>/<bucket>/<key>`). Left out, AWS: `https://<bucket>.s3.<region>.amazonaws.com/<key>` (virtual-hosted), or `https://s3.<region>.amazonaws.com/<bucket>/<key>` (path-style) when the bucket's name holds a `.`, which the wildcard certificate `*.s3.<region>.amazonaws.com` does not cover. |
| `ca` | As for Postgres: with it the chain must lead to it and the host name is not checked; without it the system trust store and the host name decide. There is no plain `http` and no unverified mode. |

**The policy.** `packages/ssc_datagw/src/ssc_datagw/s3_policy.json` is the IAM policy the customer attaches to the IAM user whose access key the connection holds, with `<bucket>` and `<prefix>` replaced by the connection's (an empty prefix is removed, leaving `*`). It allows `s3:ListBucket` on `arn:aws:s3:::<bucket>` when the request's `s3:prefix` is like `<prefix>*`, and `s3:GetObject` on `arn:aws:s3:::<bucket>/<prefix>*`; nothing else, and no write. The user needs no other policy, no console access and no other key. With `s3:ListBucket` limited by prefix, S3 may answer a missing key with 403 (`AccessDenied`, 42501) rather than 404 (`NoSuchKey`, 42P01); the live proof settles which.

**The signing.** Each request is signed with AWS Signature Version 4 (`ssc_datagw.s3.sign`, written on `hmac` and `hashlib`): `AWS4-HMAC-SHA256`, service `s3`, the connection's region, signed headers `host;x-amz-content-sha256;x-amz-date`, the payload hash of the empty body, the canonical URI with each path segment URI-encoded once (S3's rule: not twice), the canonical query sorted by name. The signing key is derived for each request and kept nowhere. `sign` reproduces the AWS test suite's `get-vanilla` and `get-vanilla-query-order-key-case` vectors.

**The query.** For this kind `sql` is not SQL: it is one of two requests, the keyword in any case, then one space, then the argument:

| Query | Request |
|---|---|
| `list <prefix>` | the keys under `<prefix>`: ListObjectsV2, `?list-type=2&prefix=<prefix>&max-keys=<min(1000, rows left)>`, following `continuation-token` until `max_rows` plus one keys are read or the list ends. A bare `list` is `list` of the empty prefix. |
| `get <key>` | one object, read as records by its extension. |

The argument must start with the connection's `prefix`, be at most 1,024 bytes of UTF-8, not start with `/`, hold no control character (`\x00` to `\x1f`, `\x7f`) and no `.` or `..` segment between `/`s. A `get` key must end in `.csv`, `.json`, `.jsonl` or `.ndjson` (any case); another is `QUERY_REFUSED` ("only .csv, .json, .jsonl and .ndjson objects are read"). Anything else is `QUERY_REFUSED` ("the query is not list <prefix> or get <key> under the connection's prefix"), and so are `params`, which an S3 read does not take; all are refused before anything is sent. Each request is a `GET` with `x-amz-content-sha256`, `x-amz-date`, `Authorization` and `User-Agent: ssc-datagw (<tag>)` (the query's tag kept to printable ASCII without `(` and `)`, at most 128 characters; CloudTrail data events record the user agent). It follows no redirect and ignores the process environment's proxy and CA settings.

**Time.** As for the REST connector (the same request code, `ssc_datagw.rest.get`): 10 s to connect with the warm-up retry, each read of an answer has `timeout_ms`, and the whole read, every list page included, ends at `timeout_ms`: past it is `QUERY_TIMEOUT`. When the gateway cancels a read the request is dropped and its connection closed.

**The answer.** Every answer's body is read (at most 32 MiB) and an error's XML `Code` is read with its status:

| Answer | Result |
|---|---|
| 2xx | read on |
| 301, or `PermanentRedirect` | `CONNECTION_UNAVAILABLE` ("the bucket is in another region") |
| 400 `AuthorizationHeaderMalformed` | `CONNECTION_UNAVAILABLE` ("the bucket is in another region": the request was signed for the connection's region) |
| other 3xx | `QUERY_FAILED`, no `sqlstate` (no redirect is followed) |
| 400 | `QUERY_FAILED`, no `sqlstate` |
| 403 (`AccessDenied`, `InvalidAccessKeyId`, `SignatureDoesNotMatch`, ...) | `QUERY_FAILED` 42501 (the key is wrong or revoked, or the policy does not allow the read) |
| 404 (`NoSuchKey`, `NoSuchBucket`) | `QUERY_FAILED` 42P01 |
| other 4xx | `QUERY_FAILED`, no `sqlstate` |
| 429, 5xx (`SlowDown`, `InternalError`, ...) | `CONNECTION_UNAVAILABLE` |
| no connection, a TLS failure | `CONNECTION_UNAVAILABLE` |
| a body over 32 MiB (decoded) | `QUERY_FAILED`, no `sqlstate`; reading stops at the cap |
| a body that does not parse: XML, CSV, JSON, or not UTF-8 | `QUERY_FAILED` 22P02 |

An error message names the status and the S3 error code when it is one of a fixed list (`ssc_datagw.s3.KNOWN_CODES`), never the body, a header or the URL. XML is read with Python's `xml.etree.ElementTree`, which expands no external entity; Python 3.14's expat carries its limits on entity amplification, and a body with a `<!DOCTYPE` (S3 never sends one) is 22P02 before it is parsed.

**Records and columns.**

| Read | Columns and rows |
|---|---|
| `list` | `key` (`string`, `db_type` `string`), `size` (`integer`, `integer`), `last_modified` (`timestamp`, `timestamp`, UTC), `etag` (`string`, `string`, without S3's quotes), one row per key in S3's order (UTF-8 binary). |
| `.csv` | UTF-8, a leading BOM dropped, RFC 4180 quoting (a malformed quote is 22P02). The first row is the header, named as for Google Sheets (an empty name `col<n>`, a repeat `_2`, `_3`, ...); each later row is a record, a short one padded with `null`, a longer one cut, a blank line no row. An empty field is `null`; `true` and `false` are booleans; an integer (`-?(0\|[1-9][0-9]*)`, within 64 bits) a number; a number with a fraction or an exponent (JSON's form, finite) a float; anything else (`007`, `+1`, `1_000`, `NaN`) the text. A column's type is that of its first non-null value in the first 100 records: `boolean`, `integer`, `float` or `string`, `db_type` `boolean`, `number` or `string`. A field over 128 KiB (Python's `csv` limit) is 22P02. |
| `.json` | As for the REST connector without `items`: an array is one record per element, an object one record, anything else 22P02; columns and types are the REST connector's. |
| `.jsonl`, `.ndjson` | UTF-8, a leading BOM dropped; each line that is not blank is one JSON record, then as for `.json`. |

At most `max_rows` plus one rows are kept, as for every connector; a CSV or JSON Lines object is parsed only that far (or 100 records, which decide the types), within the 32 MiB cap.

The connector logs one line per read, `s3 read: op=<list|get> status=<status> bytes=<body bytes> pages=<requests>`, with no bucket, key or URL. Local-proven on S3Mock 4.9.1 over TLS (wire protocol) and on a contract fake that verifies SigV4 (signing, timeout, cancel); live proof against a real bucket is a GA-5 C step (`packages/ssc_datagw/tests/test_s3.py`, which runs the connector suite against both).

<!-- gcs -->
## The GCS connector

`{kind: "gcs", bucket, prefix?, service_account}` (`ssc_datagw.gcs.GcsTarget`; unknown members are refused), the value of a `gcs` connection's `SSC_CONNECTION_*` variable ([Connectors by kind](#connectors-by-kind)). The address is the control plane's (`ssc_contracts.connections.GcsAddress`), with the same patterns.

| Member | Rule |
|---|---|
| `bucket` | `^[a-z0-9][a-z0-9._-]{1,221}[a-z0-9]$`. |
| `prefix` | Default `""`; `^[^\x00-\x1f]{0,512}$`. Every object read or listed starts with it, as text: `exports` also covers `exports-old/`, so end it with `/` to mean a folder. |
| `service_account` | The service account's JSON key file, as for Google Sheets: JSON with `client_email`, `private_key` (a PEM RSA key) and `private_key_id`, or the variable is refused with a message that quotes none of them. Never shown in a repr, an error or a log line, nor are the email, the key or a JWT made from it. |

**The binding.** The customer grants the service account Storage Object Viewer (`roles/storage.objectViewer`) on the bucket, with a condition that keeps object reads under the prefix (`<bucket>` and `<prefix>` the connection's):

```sh
gcloud storage buckets add-iam-policy-binding gs://<bucket> \
  --member="serviceAccount:<client_email>" \
  --role="roles/storage.objectViewer" \
  --condition='title=ssc-read-under-prefix,expression=resource.name.startsWith("projects/_/buckets/<bucket>/objects/<prefix>") || resource.name == "projects/_/buckets/<bucket>"'
```

The bucket needs uniform bucket-level access for a conditional binding. The account needs no other role and no role in any project. The bucket-level clause is there because Cloud Storage checks a list against the bucket, not an object, so the account may **list every object name in the bucket**, though it reads objects only under the prefix. The connector itself lists and reads only under the prefix (the grammar below), but whoever holds the key can see every name. A customer who must keep names outside the prefix private puts the exported objects in a bucket of their own.

**Who reads.** Each read signs one JWT with the service account's key, as for Google Sheets (`ssc_datagw.google`: RS256, header `kid` the `private_key_id`, `iss` and `sub` the `client_email`, `iat` now and `exp` an hour later) with `aud` `https://storage.googleapis.com/`, and sends it as `Authorization: Bearer <jwt>` on each request of that read, every list page included.

**The query.** As for the S3 connector (`ssc_datagw.s3.s3_request`): `list <prefix>` or `get <key>`, the keyword in any case, one space, then the argument, which starts with the connection's `prefix`, is at most 1,024 bytes of UTF-8, does not start with `/`, and holds no control character and no `.` or `..` segment; a `get` key ends in `.csv`, `.json`, `.jsonl` or `.ndjson`. Anything else, and `params`, which a GCS read does not take, is `QUERY_REFUSED` with the S3 connector's messages, before anything is sent.

| Query | Request |
|---|---|
| `list <prefix>` | `GET https://storage.googleapis.com/storage/v1/b/<bucket>/o?prefix=<prefix>&maxResults=<min(1000, rows left)>&pageToken=<token>&fields=items(name,size,updated,etag),nextPageToken` (`objects.list`, `pageToken` from the second page on), following `nextPageToken` until `max_rows` plus one objects are read or the list ends. A bare `list` is `list` of the empty prefix. |
| `get <key>` | `GET https://storage.googleapis.com/storage/v1/b/<bucket>/o/<key>?alt=media` (`objects.get`, the media), the key percent-encoded as one segment, its `/` as `%2F`. |

Each request has `Authorization`, `User-Agent: ssc-datagw (<tag>)` (as for S3: the query's tag kept to printable ASCII without `(` and `)`, at most 128 characters; Cloud Storage's Data Access audit logs, when the customer turns them on, record it as `callerSuppliedUserAgent`), and a list `Accept: application/json`. TLS is checked against the system trust store, no redirect is followed, and the process environment's proxy and CA settings are ignored.

**Time.** As for the S3 connector (the same request code, `ssc_datagw.rest.get`): 10 s to connect with the warm-up retry, and the whole read, every list page included, ends at `timeout_ms`: past it is `QUERY_TIMEOUT`. When the gateway cancels a read the request is dropped and its connection closed.

**The answer.** Every answer's body is read (at most 32 MiB); an error's reason is Google's `error.errors[0].reason`, and the message names the status and the reason (only when it is a plain word), never the body, a header or the URL:

| Answer | Result |
|---|---|
| 2xx | read on |
| 401 | `QUERY_FAILED` 28000 (Google refused the JWT: a revoked or wrong key) |
| 403 | `QUERY_FAILED` 42501 (the binding does not allow the read: an object outside the prefix, or no binding) |
| 404 | `QUERY_FAILED` 42P01 (no such object, or no such bucket) |
| 400, 3xx, other 4xx | `QUERY_FAILED`, no `sqlstate` |
| 429, 5xx | `CONNECTION_UNAVAILABLE` |
| no connection, a TLS failure | `CONNECTION_UNAVAILABLE` |
| a body over 32 MiB (decoded) | `QUERY_FAILED`, no `sqlstate`; reading stops at the cap |
| a body that does not parse: a list that is not JSON or not an object list, CSV, JSON, or not UTF-8 | `QUERY_FAILED` 22P02 |

**Records and columns.** As for the S3 connector ([The S3 connector](#the-s3-connector), "Records and columns"; the same code, `ssc_datagw.s3.object_table`). A `list` has the same four columns: `key` (`string`, the object's `name`), `size` (`integer`, from Google's decimal string), `last_modified` (`timestamp`, from `updated`, RFC 3339, in UTC) and `etag` (`string`, Google's base64 ETag as given, `null` when absent), one row per object in Google's order (lexicographic by name). A `.csv`, `.json`, `.jsonl` or `.ndjson` object is read by the S3 rules, and at most `max_rows` plus one rows are kept, as for every connector.

**Limits.** An object larger than 32 MiB (decoded) is not read. A list reads at most 1,000 objects a page and stops at `max_rows` plus one. Google's own request quotas apply first (a 429 is `CONNECTION_UNAVAILABLE`).

The connector logs one line per read, `gcs read: op=<list|get> status=<status> bytes=<body bytes> pages=<requests>`, with no bucket, key or URL. Contract-fake-proven (an in-process fake of the JSON API that verifies the service-account JWT, `packages/ssc_datagw/tests/test_gcs.py`, over TLS, which also runs the connector suite); live proof on a bucket in our GCP sandbox project is a GA-5 C step, and it also settles that Cloud Storage takes the self-signed JWT with `aud` `https://storage.googleapis.com/`, which only the live API can show.

<!-- bigquery -->
## The BigQuery connector

`{kind: "bigquery", project, dataset, location?, service_account, max_bytes_billed?}` (`ssc_datagw.bigquery.BigQueryTarget`; unknown members are refused), the value of a `bigquery` connection's `SSC_CONNECTION_*` variable ([Connectors by kind](#connectors-by-kind)). The address is the control plane's (`ssc_contracts.connections.BigQueryAddress`), with the same patterns.

| Member | Rule |
|---|---|
| `project` | `^[a-z][a-z0-9-]{4,28}[a-z0-9]$`, the project the jobs run and bill in. |
| `dataset` | `^[A-Za-z0-9_]{1,1024}$`, the dataset an unqualified table is read from (`defaultDataset`). |
| `location` | `^[A-Za-z0-9-]{1,32}$`, default `US`; the location the jobs run in. |
| `service_account` | The service account's JSON key file, as for Google Sheets: JSON with `client_email`, `private_key` (a PEM RSA key) and `private_key_id`, or the variable is refused with a message that quotes none of them. Never shown in a repr, an error or a log line, nor are the email, the key or a JWT made from it. |
| `max_bytes_billed` | Default 1,073,741,824 (1 GiB), from 1,048,576 (1 MiB) to 1,099,511,627,776 (1 TiB). The most one read may bill. |

**Who reads.** Each read signs one JWT with the service account's key, as for Google Sheets (`ssc_datagw.google`: RS256, header `kid` the `private_key_id`, `iss` and `sub` the `client_email`, `iat` now and `exp` an hour later) with `aud` `https://bigquery.googleapis.com/`, and sends it as `Authorization: Bearer <jwt>` on each request of that read. The customer grants the service account BigQuery Job User on `project` and BigQuery Data Viewer on `dataset`; what the classifier misses, those roles stop, since Data Viewer cannot change a table.

**The statement.** `sql` is GoogleSQL (never legacy SQL). Before anything is sent:

1. **Classify.** `ssc_datagw.classify.bigquery_refusal`, sqlglot reading the text as BigQuery: exactly one plain `SELECT`, as for Postgres, and also refused (`QUERY_REFUSED`):
   - a table, or a function, qualified by another project than `project` (compared without case): `other-project.d.t`, `` `other-project.d.t` ``, `` `other-project`.d.fn() ``;
   - `EXTERNAL_QUERY` (a query given as text, on another database) and `SESSION_USER` (it answers the service account's email);
   - any `ML.` or `AI.` function (they train, call or bill models);
   - any `INFORMATION_SCHEMA.JOBS*` view, which shows the service account's earlier queries (another app's text and parameters on the same connection), and any region-qualified `INFORMATION_SCHEMA` (`` `region-us` ``), which is project-wide. A dataset's own `INFORMATION_SCHEMA` (`reporting.INFORMATION_SCHEMA.TABLES`) is read;
   - scripting (`DECLARE`, `SET`, `BEGIN`, `CALL`, `EXECUTE IMMEDIATE`), `EXPORT DATA`, `LOAD DATA` and every DDL and DML, as statements that are not a `SELECT` or do not parse.
2. **Parameters.** Placeholders are BigQuery's own positional `?`, counted by sqlglot's BigQuery tokenizer (one in a string or a comment is not one); a count that differs from `params` is `QUERY_FAILED` 07001. Each parameter is typed by its JSON value: a boolean `BOOL`, an integer `INT64` (outside its range `QUERY_FAILED` 22003), another number `FLOAT64`, a string `STRING`, `null` a `STRING` null. Write `CAST(? AS DATE)` for a date.

**The job.** One `POST https://bigquery.googleapis.com/bigquery/v2/projects/<project>/queries` (`jobs.query`) with `query`, `useLegacySql: false`, `parameterMode: "POSITIONAL"`, `queryParameters`, `defaultDataset: {projectId: <project>, datasetId: <dataset>}`, `location`, `maximumBytesBilled` (the cost guard: BigQuery refuses before running a job that would bill more), `timeoutMs` (`timeout_ms`, at most 60,000), `jobTimeoutMs` (`timeout_ms`), `maxResults` (`max_rows` plus one), `formatOptions: {useInt64Timestamp: true}` and `labels: {"ssc-tag": <tag>}`, the query's tag in lower case with each character outside `[a-z0-9_-]` an `_`, at most 63 characters, which the customer's job history and billing export show. While `jobComplete` is false the connector polls `GET .../queries/<jobId>?location=&timeoutMs=&maxResults=` (`getQueryResults`); further pages follow `pageToken` until `max_rows` plus one rows. Every request has `Accept: application/json` and `User-Agent: ssc-datagw`; TLS is checked against the system trust store, no redirect is followed, and the process environment's proxy and CA settings are ignored.

**Time.** As for the REST connector (the same request code, `ssc_datagw.rest.get`): 10 s to connect with the warm-up retry, each answer at most 32 MiB, and the whole read, polls and pages included, ends at `timeout_ms`: past it is `QUERY_TIMEOUT`. Past it, and when the gateway cancels a read (the kill watch or its deadline), the connector cancels the job with `POST .../jobs/<jobId>/cancel?location=`, from a client of its own, 5 s at most and run to its end even if the read is cancelled again; a cancel that fails is logged by its error class. A read that ends before `jobs.query` named its job cannot cancel it: `jobTimeoutMs` stops it in BigQuery.

**The answer.** BigQuery's reason (`error.errors[0].reason`) decides first, then the status; the message names the reason (only when it is a plain word), never the body:

| Answer | Result |
|---|---|
| 2xx | read on |
| `invalidQuery` | `QUERY_FAILED` 42601 |
| `notFound` | `QUERY_FAILED` 42P01 |
| `accessDenied`, `billingNotEnabled`, `billingTierLimitExceeded`, or any other 403 | `QUERY_FAILED` 42501 |
| `bytesBilledLimitExceeded` | `QUERY_FAILED` 53400 ("the read would bill more than max_bytes_billed") |
| `responseTooLarge` | `QUERY_FAILED` 54000 |
| `stopped` (the job was cancelled outside the gateway) | `QUERY_FAILED` 57014 |
| `invalid` (e.g. a parameter BigQuery cannot take) | `QUERY_FAILED` 22023 |
| `timeout` (`jobTimeoutMs` ran out) | `QUERY_TIMEOUT` |
| `rateLimitExceeded`, `quotaExceeded`, `backendError`, `jobRateLimitExceeded`, `internalError` | `CONNECTION_UNAVAILABLE` |
| 401 | `QUERY_FAILED` 28000 (Google refused the JWT: a revoked or wrong key) |
| 429, 5xx, no connection, a TLS failure | `CONNECTION_UNAVAILABLE` |
| other 3xx, 4xx | `QUERY_FAILED`, no `sqlstate` |
| a complete job with `errors` and no `schema` | as its first reason, by this table (`errors` beside a `schema` are warnings) |
| a body over 32 MiB (decoded) | `QUERY_FAILED`, no `sqlstate` |
| a body that is not JSON or not a query result, or a value that does not convert to its column's type | `QUERY_FAILED` 22P02 |

**Columns and values.** The columns are `schema.fields` in order; `db_type` is the GoogleSQL name (the API's legacy `INTEGER`, `FLOAT`, `BOOLEAN`, `RECORD` are given as `INT64`, `FLOAT64`, `BOOL`, `STRUCT`), `ARRAY<...>` for a `REPEATED` column. Values arrive as text and are converted by column:

| BigQuery | Portable type | Value |
|---|---|---|
| `INT64` | `integer` | number |
| `FLOAT64` | `float` | number; `NaN`, `Infinity`, `-Infinity` as those strings |
| `NUMERIC`, `BIGNUMERIC` | `decimal` | exact, as a string |
| `BOOL` | `boolean` | |
| `STRING`, `GEOGRAPHY` (WKT) | `string` | |
| `BYTES` | `bytes` | base64 |
| `DATE`, `TIME` | `date`, `time` | ISO 8601 |
| `DATETIME` | `timestamp` | ISO 8601 without a zone |
| `TIMESTAMP` | `timestamp` | ISO 8601 in UTC, to the microsecond |
| `JSON` | `json` | the JSON itself |
| `INTERVAL` | `interval` | an ISO 8601 duration, each part with its own sign (`1-2 3 4:5:6.5` is `P1Y2M3DT4H5M6.5S`) |
| `ARRAY<...>`, `STRUCT` | `json` | an array, an object by member name, each member by this table |
| any other (`RANGE`, ...) | `string` | BigQuery's text |

At most `max_rows` plus one rows are read, as for every connector.

The connector logs one line per read, `bigquery read: pages=<n> polls=<n> bytes=<body bytes>`, and `could not cancel the job: <error class>` when a cancel fails, with no project, job id or query text. Contract-fake-proven: a fake BigQuery API that checks the JWT (`packages/ssc_datagw/tests/test_bigquery.py`, over TLS in-process, which also runs the connector suite); live proof on the BigQuery sandbox is a GA-5 C step.
<!-- sqlserver -->
## The SQL Server connector

`{kind: "sqlserver", host, port, database, user, password, ca}` (`ssc_datagw.sqlserver.SqlServerTarget`; `port` defaults to 1433). `database` is the database the session must be in; `user` is a SQL Server login (no Windows or Entra authentication). `ca` is required: the server's CA certificate in PEM, which must parse when the connector is built. The value is held, tagged and pinned as for Postgres. Each query gets its own connection and runs in this order; the first step that refuses answers:

1. **Classify.** sqlglot parses the text as T-SQL (`ssc_datagw.classify.TSQL`, `tsql_refusal`); anything but exactly one `SELECT` (or `UNION`, `INTERSECT`, `EXCEPT` of them) is `QUERY_REFUSED`, as for Postgres: a second statement, `SELECT ... INTO`, `EXEC`, `WAITFOR`, `SET`, and any call to `OPENROWSET`, `OPENQUERY`, `OPENDATASOURCE` or `OPENXML`. On top of that it refuses a variable (`@x`, and `@@` functions such as `@@SPID`), a temporary table (`#t`, `##t`), `NEXT VALUE FOR` (it advances a sequence), a three- or four-part name (another database or a linked server) and a function called in another database, and a table or function whose name starts `xp_`, `sp_` or `fn_` (a column of that name is fine). `FOR XML` passes; `FOR JSON` is refused because sqlglot 28 does not parse it, and text the classifier cannot parse is refused: the app builds JSON from the rows. `WITH (NOLOCK)` and `OPTION (...)` hints pass.
2. **Connect over TLS 1.2**, `verify-ca` with the pasted `ca`: the chain must lead to it and the name is not checked, the rule of `ssc_datagw.tls` and the Postgres connector. The driver (python-tds) speaks TLS 1.2 only and refuses a server that offers no encryption; there is no plaintext or unverified mode. The connector opens the TCP connection itself, so a server that redirects the login elsewhere (Azure SQL's redirect policy) is refused as `CONNECTION_UNAVAILABLE`: point the connection at the server's own address, or use the proxy policy. The login sets `program_name=ssc-datagw` and `autocommit`. 10 s to connect, with the same 60 s warm-up retry. A refused connection, a TLS failure, a bad password (18456), a database the login cannot open (4060) or a time-out after the warm-up is `CONNECTION_UNAVAILABLE` at once.
3. **Settle the session and read it back.** One batch sets `TRANSACTION ISOLATION LEVEL READ COMMITTED`, `LOCK_TIMEOUT` to `timeout_ms`, `ARITHABORT ON` and `DATEFORMAT ymd`; then one `SELECT` reads the session back. SQL Server has no read-only session, so the login's grants and the classifier are the guard, and the read-back checks them: `CONNECTION_UNAVAILABLE` when `DB_NAME()` is not `database`, when the login is in `sysadmin`, in `db_owner`, `db_datawriter` or `db_ddladmin`, when `HAS_PERMS_BY_NAME` on the database answers yes for `INSERT`, `UPDATE`, `DELETE`, `ALTER`, `CONTROL` or `EXECUTE`, or when the login's user holds any of those as a grant of its own on a schema or object (`sys.database_permissions`). A check that answers `NULL` counts as a yes.
4. **Bind and read.** Each `?` placeholder that sqlglot's T-SQL tokenizer finds (a `?` inside a string, a bracketed name or a comment is not one) becomes a parameter of `sp_executesql` (`@P1`, `@P2`, ...), so values never enter the text; a null is sent as `NULL`. A wrong count is `QUERY_FAILED` 07001; `%` is never a format directive. The statement carries the query's tag in a leading comment, `/* <tag> */` (at most 128 characters; `/*` and `*/` are dropped until none is left, since T-SQL comments nest), which `sys.dm_exec_sql_text` shows the customer's DBA. The rows are fetched 500 at a time, at most `max_rows` plus one.
5. **Close.** The connection is closed. Every read is one statement in autocommit, so nothing is left open.

**Stopping a read.** One deadline, `timeout_ms` after the statement is sent, covers the statement and every fetch; `LOCK_TIMEOUT` (error 1222) ends a read that waits on a lock sooner. Both are `QUERY_TIMEOUT`. When the deadline passes, or the gateway cancels the read (the kill watch or its deadline), the connector shuts its TCP connection down: SQL Server ends the request of a client that went away and drops the session (gone within seconds in `tests/test_sqlserver.py`). There is no `KILL`: it needs `ALTER ANY CONNECTION`, a server-wide right to end anyone's session, which this login must not hold.

**Types.** By the type the driver reports:

| SQL Server | `type` | `db_type` |
|---|---|---|
| `bit` | `boolean` | `bit` |
| `tinyint`, `smallint`, `int`, `bigint` | `integer` | its name |
| `real`, `float` | `float` | its name |
| `decimal`, `numeric` | `decimal` | `decimal` |
| `money`, `smallmoney` | `decimal` | its name |
| `char`, `varchar`, `varchar(max)` | `string` | `varchar` |
| `nchar`, `nvarchar`, `nvarchar(max)` | `string` | `nvarchar` |
| `text`, `ntext`, `xml`, `sql_variant` | `string` | its name |
| `uniqueidentifier` | `uuid` | `uniqueidentifier` |
| `date`, `time` | `date`, `time` | its name |
| `datetime`, `datetime2`, `smalldatetime` | `timestamp`, without a zone | its name |
| `datetimeoffset` | `timestamp`, with its offset | `datetimeoffset` |
| `binary`, `varbinary`, `varbinary(max)`, `image` | `bytes` | `varbinary` (`image` for `image`) |
| a CLR type (`hierarchyid`, `geography`, `geometry`) | `bytes`, its serialized form | its name |

The driver reports some types by one wire type, so `db_type` merges them: `char` is `varchar`, `nchar` is `nvarchar`, `binary` is `varbinary`, `numeric` is `decimal`. A `sql_variant` arrives as its value's own JSON.

**Errors.** Error 1222 (lock timeout) is `QUERY_TIMEOUT`; 18456, 4060, 701 (out of memory), 1204 (out of locks), 17809 (out of connections) and a lost connection are `CONNECTION_UNAVAILABLE`; any other database error is `QUERY_FAILED` with the SQLSTATE its number stands for (`ssc_datagw.sqlserver.SQLSTATE`), or none:

| Number | SQLSTATE | Meaning |
|---|---|---|
| 208, 4104 | 42P01 | no such object; a multi-part name that does not bind |
| 207 | 42703 | no such column |
| 209 | 42702 | an ambiguous column |
| 102, 156 | 42601 | syntax |
| 195, 4121 | 42883 | no such function |
| 229, 230, 262, 297, 916 | 42501 | permission denied (an object, a column, the database, the action, the database for the login) |
| 8134 | 22012 | division by zero |
| 245, 8114, 241, 242 | 22P02 | a value that does not convert |
| 8115, 220 | 22003 | arithmetic overflow |
| 512 | 21000 | a subquery returned more than one value |
| 1205 | 40P01 | a deadlock victim |

The message names the error class and number only, never the server's text, which quotes names and values. The driver's own log is held at `WARNING`: at `INFO` it would log the statement.

**The login.** `packages/ssc_datagw/src/ssc_datagw/sqlserver_setup.sql` is the script the customer runs with `sqlcmd` (or SSMS in SQLCMD mode), as a sysadmin, in the connection's database: `read -rs password && export password`, then `sqlcmd -S <host> -d reporting -U admin -N -C -i sqlserver_setup.sql -v login=ssc_datagw schemas=reporting,finance`. sqlcmd reads the environment variable `password` as `$(password)`, so the password is never in a file or on the command line; in SSMS, `:setvar` lines above the script, not saved. It makes the login `WITH CHECK_POLICY = ON` (the password may not contain the login's name, nor a single quote) or gives it the new password, makes its user in the database, takes the login out of every server role and the user out of every database role, and grants `SELECT` on each named schema; a schema that does not exist stops it. It ends by acting as the user (`EXECUTE AS USER`) and stops with an error if the user may still write, alter, control or execute anything, such as a grant made by hand, which it does not revoke. It is safe to run again, which is how a new password or another schema lands.

What stops each attack: local-proven on SQL Server 2022 in a container; no 2025 run yet (`tests/test_sqlserver.py`, in `packages/ssc_datagw`, which also runs the connector suite). The classifier column is tested for every row, the database-alone column for every row with an error number and the two logins:

| Attack | Classifier | Database alone |
|---|---|---|
| `INSERT`, `UPDATE`, `DELETE` | refused | 42501 (229): the login has `SELECT` only |
| `CREATE TABLE`, `SELECT ... INTO` | refused | 42501 (262): no `CREATE TABLE` |
| a schema not granted (`secret.payroll`) | passes (a read) | 42501 (229) |
| `EXEC`, `WAITFOR`, `OPENROWSET`, a second statement | refused | not tested with the classifier off |
| a login in `db_datawriter`, or `sa` | passes (a read) | the read-back refuses it before any read; `sqlserver_setup.sql` takes the login out of every role |

<!-- snowflake -->
## The Snowflake connector

`{kind: "snowflake", account, user, database, schema?, warehouse, role?, private_key}` (`ssc_datagw.snowflake.SnowflakeTarget`; unknown members are refused), the value of a `snowflake` connection's `SSC_CONNECTION_*` variable ([Connectors by kind](#connectors-by-kind)). The address is the control plane's (`ssc_contracts.connections.SnowflakeAddress`), with the same patterns; `schema` may also be given as `schema_name`.

| Member | Rule |
|---|---|
| `account` | `^[A-Za-z0-9_.-]{1,128}$`, the account identifier (`<org>-<account>`, or a locator such as `xy12345.us-east-1`), never a URL. |
| `user` | `^[A-Za-z0-9_.-]{1,128}$`, the service user `snowflake_setup.sql` makes. |
| `database` | `^[A-Za-z0-9_.-]{1,128}$`, the database a read runs in and the only one it may name. |
| `schema` | `^[A-Za-z0-9_.-]{1,128}$`, default `PUBLIC`; the schema an unqualified table is read from. |
| `warehouse` | `^[A-Za-z0-9_.-]{1,128}$`, the warehouse the reads run on and bill to. |
| `role` | `^[A-Za-z0-9_.-]{1,128}$`, optional; the user's default role (`SSC_DATAGW_READ`) when left out. |
| `private_key` | The user's RSA private key as an unencrypted PKCS#8 PEM (`-----BEGIN PRIVATE KEY-----`) of at least 2048 bits, or the variable is refused with a message that names the field and quotes none of it (a PKCS#1 `BEGIN RSA PRIVATE KEY`, an encrypted key and an EC key are refused). Never shown in a repr, an error or a log line, nor are its fingerprint or a JWT made from it. |

**The user.** `packages/ssc_datagw/src/ssc_datagw/snowflake_setup.sql` is the script the customer runs as `ACCOUNTADMIN`, in a worksheet or with SnowSQL, after five `SET`s: `ssc_public_key` (the public key you generated, with or without its PEM lines), `ssc_user`, `ssc_warehouse`, `ssc_database` and `ssc_schemas` (comma-separated). It makes the role `SSC_DATAGW_READ` with `USAGE` on the warehouse, the database and each schema and `SELECT` on every table and view in each schema, now and future; and the user with `TYPE = SERVICE` (key-pair sign-in only), no password, the public key as `RSA_PUBLIC_KEY`, `DEFAULT_ROLE = SSC_DATAGW_READ` and `DEFAULT_SECONDARY_ROLES = ()`, so no other role it holds is active. A name that is not letters, digits and `_`, a key that is not base64, or no schema stops it before any change. It revokes nothing; the readback at the end (`SHOW GRANTS TO ROLE`, `DESC USER`, `SHOW GRANTS TO USER`) shows every grant, and `RSA_PUBLIC_KEY_FP` must equal the fingerprint of the key you generated. It is safe to run again, which is how a new key or another schema lands. What the classifier misses, the role stops: it holds `SELECT` and `USAGE` only, and Snowflake gives every role what is granted to `PUBLIC`.

**Who reads.** Each read signs one JWT with the private key (RS256, pyjwt): `iss` `<ACCOUNT>.<USER>.SHA256:<fingerprint>`, `sub` `<ACCOUNT>.<USER>`, `iat` now and `exp` 59 minutes later. `<ACCOUNT>` is `account` in upper case with everything from its first `.` removed (a locator's region; an `org-account` identifier has no `.` and is whole), `<USER>` is `user` in upper case, and the fingerprint is the base64 of the SHA-256 of the public key's DER `SubjectPublicKeyInfo` (what `DESC USER` shows as `RSA_PUBLIC_KEY_FP`). Every request of the read sends it as `Authorization: Bearer <jwt>` with `X-Snowflake-Authorization-Token-Type: KEYPAIR_JWT`. The requests go to `https://<account>.snowflakecomputing.com`, each `_` of the account a `-`. Both rules, the JWT's account and the host, are Snowflake's documented ones; the live proof is a GA-5 C step with the founder's trial account.

**The statement.** `sql` is Snowflake SQL. Before anything is sent:

1. **Classify.** `ssc_datagw.classify.snowflake_refusal`, sqlglot reading the text as Snowflake: exactly one plain `SELECT`, as for Postgres, and also refused (`QUERY_REFUSED`):
   - the functions `RESULT_SCAN`, `GET_QUERY_OPERATOR_STATS` (another statement's result or plan), `VALIDATE`, `GET_DDL`, `IDENTIFIER` (a name given as text), `INFER_SCHEMA` and `GENERATE_COLUMN_DESCRIPTION` (a stage named in a string), any `SYSTEM$` function, and any function starting `QUERY_HISTORY`, `LOGIN_HISTORY`, `TASK_HISTORY` or `COPY_HISTORY`, called plainly or in `TABLE(...)`;
   - a stage (`@s`, `@s/path`, `@~`, `@%t`, and `@s` as a function's argument) and a session variable or positional stage column (`$v`, `$1`);
   - `TABLE(...)` over a string, a bind or a variable (`TABLE('db.s.t')`, `TABLE(?)`, `TABLE(:1)`, `TABLE($t)`), which names a table this check never sees;
   - a table or function qualified by another database than `database` (compared without case): `other.s.t`, `other.s.f()`;
   - anything in the `SNOWFLAKE` database (`SNOWFLAKE.ACCOUNT_USAGE`, `SNOWFLAKE.CORTEX`), even when it is the connection's;
   - `CALL`, `COPY`, `PUT`, `GET`, `USE`, `SET`, `EXECUTE IMMEDIATE` and every DDL and DML, as statements that are not a `SELECT` or do not parse.
2. **Bindings.** Placeholders are positional `?`, counted by sqlglot's Snowflake tokenizer (one in a string or a comment is not one); a count that differs from `params` is `QUERY_FAILED` 07001. Each parameter is bound by its JSON value as `bindings` `"1"`, `"2"`, ...: a boolean `BOOLEAN` (`"true"`, `"false"`), an integer `FIXED` (more than 38 digits is `QUERY_FAILED` 22003), another number `REAL` (`NaN`, `inf`, `-inf` as those strings), a string `TEXT`, `null` a `TEXT` null. Write `?::DATE` for a date. (The connector also binds a date `DATE`, a time `TIME` and a datetime `TIMESTAMP_NTZ` as ISO 8601 text and bytes `BINARY` as hex; an app's JSON parameters never are those.)

**The requests.** SQL API v2, every one through `ssc_datagw.rest.get` with `Accept` and `Content-Type: application/json` and `User-Agent: ssc-datagw`:

1. `POST /api/v2/statements` with `statement`, `timeout` (`timeout_ms` in seconds, rounded up, at least 1: Snowflake stops the statement itself), `database`, `schema`, `warehouse`, `role` (when set), `bindings`, `parameters: {query_tag: <tag>, MULTI_STATEMENT_COUNT: "1"}` and `resultSetMetaData: {format: "jsonv2"}`. The tag shows in the customer's `QUERY_HISTORY`.
2. A 202 means the statement is still running: the connector polls `GET /api/v2/statements/<statementHandle>` every 500 ms until a 200.
3. A 200 carries `resultSetMetaData.rowType`, `partitionInfo`, the first partition's `data` and `statementHandle`; further partitions are `GET /api/v2/statements/<handle>?partition=<n>`, read in order until `max_rows` plus one rows or the last partition.

**Time.** As for the REST connector: 10 s to connect with the warm-up retry, each answer at most 32 MiB, TLS checked against the system trust store, no redirect followed, the process environment's proxy and CA settings ignored, and the whole read, polls and partitions included, ends at `timeout_ms`: past it is `QUERY_TIMEOUT`. Past it, and when the gateway cancels a read (the kill watch or its deadline), the connector posts `POST /api/v2/statements/<handle>/cancel` from a client of its own, 5 s at most and run to its end even if the read is cancelled again; a cancel that fails is logged by its error class. A read that ends before Snowflake named the statement cannot cancel it: the request's `timeout` stops it in Snowflake.

**The answer.** Snowflake's `code` decides first, then the status; the message names the code (only when it is six digits), never Snowflake's `message`, which quotes names and values:

| Answer | Result |
|---|---|
| 200 | read on; 202 polls |
| `002003` (no such object, or not authorized to see it) | `QUERY_FAILED` 42P01 |
| `001003` (a syntax error) | `QUERY_FAILED` 42601 |
| `003001` (insufficient privileges) | `QUERY_FAILED` 42501 |
| `000630` (the statement passed its `timeout`) | `QUERY_TIMEOUT` |
| `000604` (the statement was cancelled outside the gateway) | `QUERY_FAILED` 57014 (`QUERY_TIMEOUT` when the connector cancelled it) |
| `390100`, `390144`, `390142`, or any 401 | `QUERY_FAILED` 28000 (Snowflake refused the JWT: a wrong or replaced key, another user) |
| 429, 5xx, no connection, a TLS failure | `CONNECTION_UNAVAILABLE` |
| any other 3xx, 4xx | `QUERY_FAILED` with the answer's `sqlState` when it is five digits or capitals, else no `sqlstate` |
| a body over 32 MiB (decoded) | `QUERY_FAILED`, no `sqlstate` |
| a body that is not JSON, a 200 without `rowType`, `data` or a handle, a 202 without a handle, a partition without `data`, or a value that does not convert to its column's type | `QUERY_FAILED` 22P02 |

**Columns and values.** The columns are `rowType` in order; `db_type` is Snowflake's type name in lower case (`fixed`, `text`, `timestamp_tz`, ...). Values arrive as text (`jsonv2`) and are converted by column; a null stays `null`:

| Snowflake | Portable type | Value |
|---|---|---|
| `FIXED`, scale 0 | `integer` | number |
| `FIXED`, any other scale | `decimal` | exact, as a string |
| `REAL` | `float` | number; `NaN`, `Infinity`, `-Infinity` as those strings |
| `TEXT` | `string` | |
| `BOOLEAN` | `boolean` | |
| `DATE` | `date` | ISO 8601 (Snowflake sends days since the epoch) |
| `TIME` | `time` | ISO 8601 (seconds since midnight) |
| `TIMESTAMP_NTZ` | `timestamp` | ISO 8601 without a zone |
| `TIMESTAMP_LTZ` | `timestamp` | ISO 8601 in UTC |
| `TIMESTAMP_TZ` | `timestamp` | ISO 8601 with the value's own offset (Snowflake sends seconds since the epoch and the offset in minutes plus 1440) |
| `BINARY` | `bytes` | base64 (Snowflake sends hex) |
| `VARIANT`, `OBJECT`, `ARRAY` | `json` | the JSON itself |
| any other (`GEOGRAPHY`, `VECTOR`, ...) | `string` | Snowflake's text |

Fractions of a second past the microsecond are cut, toward the past. At most `max_rows` plus one rows are read, as for every connector.

**Limits.** One statement a read (`MULTI_STATEMENT_COUNT` 1). No stage, no session variable, no other database, no `SNOWFLAKE` database, no result of an earlier statement. The warehouse bills for the time each read runs; `timeout_ms` bounds it. Only key-pair sign-in: no password, no OAuth, no encrypted key.

The connector logs one line per read, `snowflake read: partitions=<n> polls=<n> bytes=<body bytes>`, and `could not cancel the statement: <error class>` when a cancel fails, with no account, handle or statement text. Contract-fake-proven (an in-process fake of the SQL API v2 that verifies the key-pair JWT; `packages/ssc_datagw/tests/test_snowflake.py`, over TLS in-process, which also runs the connector suite); live proof on a real account is a GA-5 C step with the founder's trial account.
<!-- airtable -->
## The Airtable connector

`{kind: "airtable", base_id, table?, token}` (`ssc_datagw.airtable.AirtableTarget`; unknown members are refused), the value of an `airtable` connection's `SSC_CONNECTION_*` variable ([Connectors by kind](#connectors-by-kind)). The address is the control plane's (`ssc_contracts.connections.AirtableAddress`), with the same patterns.

| Member | Rule |
|---|---|
| `base_id` | `^app[A-Za-z0-9]{14}$`. |
| `table` | Optional; `^[^\x00-\x1f]{1,100}$`, a table name or id. When set, every query must read exactly this table, as written (a name and its `tbl…` id are not the same). |
| `token` | A personal access token with the scope `data.records:read`, its access limited to this base and nothing else: `pat`, then the rest, 20 to 200 visible ASCII characters in all (`!` to `~`). Never shown in a repr, an error or a log line, not even the error that refuses it. |

The connector reaches `https://api.airtable.com` with the system trust store and the host name checked; the address of the API and the trust are not members of the target (only tests replace them).

**The query.** For this kind `sql` is not SQL: it is one list request, `<table>[ view <view>][ where <formula>]`, the keywords in any case, each part parted from the next by one space. The first ` view ` and the first ` where ` split it; everything after ` where ` is the formula, unchanged.

| Part | Rule |
|---|---|
| `<table>` | A table name or id (`tbl` and 14 characters), 1 to 100 characters, no control character (`\x00` to `\x1f`, `\x7f`), no space at either end, no `;`, and a first word (up to the first space) that is not `insert`, `delete`, `update`, `select`, `create`, `drop`, `alter`, `replace`, `upsert`, `merge` or `truncate` (any case). A table whose name breaks a rule, starts with such a word, or holds ` view ` or ` where ` is read by its `tbl…` id. With the connection's `table` set, it must equal it ("the query's table is not the connection's table"). |
| `<view>` | Optional. A view name or id (`viw…`), the same rules. Airtable reads the view's records in the view's order, its filters applied. |
| `<formula>` | Optional. An Airtable formula, 1 to 2,000 characters, no control character, sent as `filterByFormula`: a record is read when the formula is true for it. |

Anything else is `QUERY_REFUSED` ("the query is not <table>[ view <view>][ where <formula>]"), and so are `params`, which an Airtable read does not take; all are refused before anything is sent. A write cannot be expressed: the connector sends only `GET`, and the token's scope reads only.

**The requests.** Each page is `GET https://api.airtable.com/v0/<base_id>/<table>?pageSize=<min(100, rows left)>[&view=<view>][&filterByFormula=<formula>][&offset=<offset>]`, the table and every value percent-encoded (a `/` too), with `Authorization: Bearer <token>`, `Accept: application/json` and `User-Agent: ssc-datagw (<tag>)` (the query's tag as for the S3 connector: printable ASCII without `(` and `)`, at most 128 characters). Pages follow the previous page's `offset` until `max_rows` plus one records are read or a page has no `offset`. Airtable allows 5 requests a second per base, so each page after the first waits 200 ms (`ssc_datagw.airtable.PAGE_PAUSE_SECONDS`); a 50,000-row read is 500 pages, at least 100 s of pauses, so `timeout_ms` (30 s at most) bounds a read at fewer than 150 pages, 15,000 records. No redirect is followed; the process environment's proxy and CA settings are ignored.

**Time.** As for the REST connector (the same request code, `ssc_datagw.rest.get`): 10 s to connect with the warm-up retry, each read of an answer has `timeout_ms`, and the whole read, every page and pause included, ends at `timeout_ms`: past it is `QUERY_TIMEOUT`. When the gateway cancels a read the request is dropped and its connection closed.

**The answer.** Every answer's body is read (at most 32 MiB); an error's `error.type` (or `error`, when it is a string) is named in the message when it is an upper-case word (`^[A-Z][A-Z0-9_]{0,63}$`, e.g. `the source answered 422 INVALID_FILTER_BY_FORMULA`), and Airtable's `message` never is.

| Answer | Result |
|---|---|
| 2xx | read on |
| 401 (`AUTHENTICATION_REQUIRED`) | `QUERY_FAILED` 28000 (the token is wrong, expired or revoked) |
| 403 (`INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND`, ...) | `QUERY_FAILED` 42501: no permission, or no such base or table; Airtable does not tell the two apart |
| 404 (`NOT_FOUND`) | `QUERY_FAILED` 42P01 (the base or table) |
| 422 (`INVALID_FILTER_BY_FORMULA`, `UNKNOWN_FIELD_NAME`, `VIEW_NAME_NOT_FOUND`, ...) | `QUERY_FAILED` 42601, the type only |
| 3xx, other 4xx | `QUERY_FAILED`, no `sqlstate` (no redirect is followed) |
| 429, 5xx | `CONNECTION_UNAVAILABLE` (not retried; Airtable asks for 30 s before the next request) |
| no connection, a TLS failure | `CONNECTION_UNAVAILABLE` |
| a body over 32 MiB (decoded) | `QUERY_FAILED`, no `sqlstate`; reading stops at the cap |
| a body that is not JSON, or not `{"records": [...], "offset"?: "<text>"}` with each record `{"id": "<text>", "createdTime": "<ISO 8601 with a zone>", "fields"?: {...}}` | `QUERY_FAILED` 22P02 |

**Records and columns.** One row per record, in Airtable's order (the view's, or the table's default). The columns are `id` (`string`, `db_type` `string`), `created_time` (`timestamp`, `db_type` `timestamp`, UTC, from `createdTime`), then one column per field name, in the order names first appear across the records read. Airtable leaves an empty field out of a record, so a record without a field is `null` there. A field column's type and `db_type` are the REST connector's: its first non-null value in the first 100 records, boolean `boolean`, integer `integer`, other number `float`, string `string`, array or object `json` (attachments, linked records, lookups, collaborators: as Airtable sends them), none `string`; a date or date-time field is Airtable's ISO text, a `string`. A field named like a column already taken (`id`, `created_time`, or a repeat) gets `_2`, `_3`, ..., as Google Sheets header names do. No records is the two fixed columns and no rows. At most `max_rows` plus one rows are read, as for every connector.

The connector logs one line per read, `airtable read: status=<status> bytes=<body bytes> pages=<requests>`, with no base, table, formula or URL. Contract-fake-proven (an in-process fake of the Airtable REST API that checks the token); live proof on a real base is a GA-5 C step with the founder's free-plan base (`packages/ssc_datagw/tests/test_airtable.py`, which runs the connector suite against the fake).

## Limits

Each limit is the minimum of the platform, the connection's `limits`, the grant's `limits` and the request's ask (`docs/contracts/access-snapshot.md`, amendment SSC-050). A cap a layer leaves out puts no cap at that layer; `0` is a cap of zero.

| Limit | Platform ceiling | Default ask |
|---|---|---|
| `max_rows` | 50,000 | 5,000 |
| `max_bytes` (the encoded rows) | 50 MB | 10 MB |
| `timeout_ms` | 30,000 | none |
| `concurrency` (per grant) | 4 | |
| `daily_rows` (per grant, UTC day) | 1,000,000 | |
| `daily_bytes` (per grant, UTC day) | 1 GB | |

A grant is one connection and one environment. Within the day's budget a result is cut at what is left (`daily_rows`, `daily_bytes`); with nothing left the query is refused. **v1 counts the budget and the slots per instance**: with several instances a grant can exceed them by up to that many times, at most 10 (the service's instance cap, `DATAGW_MAX` in `infra/ssc_infra/cell.py`), so 40 queries at once and 10,000,000 rows or 10 GB a day at the platform ceilings. A shared count needs storage the cell does not have yet. For GA the three are called **advisory** wherever a customer meets them (the API's field descriptions, the console's limit fields, runbook ssc-052, the trust pack); `max_rows`, `max_bytes` and `timeout_ms` are exact (decision 034).

## Snapshot and kill switch

Nothing runs between requests. The snapshot is read before the first request is accepted (waited for up to 10 s), again by a request when no read confirmed it in the last 2 s (waited for up to 4 s), and by the kill watch while any query runs: every second it re-reads on the same 2 s rule, and cancels each running query its environment or connection is no longer admitted to. A suspended connection or a stopped app is therefore refused within 2 s plus one bucket read of `latest.json` moving, whether the service was awake or at zero, and a running query is ended within 3 s plus that read.

<a id="files"></a>
## Files

The file broker (SSC-046, `ssc_datagw.files`) gives an app environment signed links to its own files in the cell bucket. An app asks for it with `[files]` in `ssc.toml` (`docs/contracts/manifest.md#files`); the first such deploy in a cell waits while the data gateway is created ("connections", `docs/contracts/manifest.md`), with no human step. The app never holds storage credentials: it asks the data gateway for a link and sends the bytes to Cloud Storage itself. Helpers: `ssc_app.files` (Python) and `@delimitus/ssc-files` (Node), which find the data gateway from the metadata server and retry once while it starts from zero.

`POST /v1/files/put`, `POST /v1/files/get` and `POST /v1/files/delete`, with `Authorization` and `X-Request-Id` as for a query (no identity note). The body is JSON, at most 1 MB, with no other member:

| Member | Type | Meaning |
|---|---|---|
| `name` | string | The file: `/`-separated segments of `A-Z a-z 0-9 . _ -`, each starting with a letter or digit, at most 256 characters. No `..`, no hidden, empty or leading segment. |
| `content_type` | string | `put` only. The type the upload must carry, such as `image/png`. Default `application/octet-stream`. |

The file is `files/<env_id>/<name>` in the cell bucket, and `env_id` is the caller's environment from the workload token, never from the body: no name reaches another environment, of the same app or another. `put` and `get` answer `200` with a link:

| Member | Meaning |
|---|---|
| `url` | A V4 signed URL on `https://storage.googleapis.com/`, for this one object, valid 10 minutes. Editing its path, its query or the headers it names breaks the signature. |
| `method` | `PUT` or `GET`. |
| `headers` | Send each with the request. `put`: `content-type` as asked and `x-goog-content-length-range: 0,26214400`, so the bucket refuses another type or a body over 25 MB. `get`: none. |
| `expires_at` | ISO 8601, UTC. |
| `max_bytes` | `put` only: 26,214,400. |
| `request_id` | As in `X-Request-Id`. |

A `get` link answers with `Content-Disposition: attachment; filename="<last segment>"`, signed into the link (`response-content-disposition`), so a stored HTML or SVG file is saved by a browser, never rendered as a page. A `put` replaces a file of the same name. `delete` removes the file at once and answers `{"deleted": true, "request_id": ...}`.

The links are signed by the data gateway as its own account, `ssc-data@<cell project>`, through IAM `signBlob` on itself; there is no key file (SSC-095). The files sit in the customer's cell bucket, encrypted with the cell's own key (`infra/README.md`, "File storage").

| Code | Status | Stage | Fix owner | When |
|---|---|---|---|---|
| `NOT_FOUND` | 404 | request | app | The operation is not `put`, `get` or `delete`. |
| `FILE_NOT_FOUND` | 404 | files | app | `get` or `delete` of a file the environment does not have. |
| `FILES_QUOTA_EXCEEDED` | 413 | files | app | `put` while the environment's files already use 1 GB; delete some first. |
| `FILES_UNAVAILABLE` | 503 | files | platform | The bucket or IAM did not answer, or this data gateway has no broker. |

`BODY_TOO_LARGE`, `UNAUTHENTICATED`, `UNAVAILABLE`, `DATA_SNAPSHOT_STALE`, `UNKNOWN_ENVIRONMENT`, `APP_NOT_ACTIVE` and `VALIDATION_FAILED` are as for a query, in the same order: **the kill switch suspends the broker**, so a disabled or quarantined app gets no link, within the 2 s the snapshot rule allows.

What v1 does not do, and says so:

- **No virus scanning.** Files are stored and served as the app sent them. Scanning is deferred; the attachment disposition is what keeps a stored file from running in a browser.
- **The quota is checked when a link is made**, by summing the environment's files. Links made just before the quota fills can each add one more file of up to 25 MB.
- **A link outlives the kill switch** for up to its 10 minutes: a disabled app gets no new link, but one it already holds still works until it expires.
- **No browser-direct upload.** The bucket has no CORS rule, so the app's server sends and fetches the bytes (it reaches Cloud Storage over Private Google Access; the storage host is never on the egress allowlist, `docs/contracts/manifest.md`, egress).
- **Deletion with the environment** is a seam: the cell agent's `POST /v1/files/drop` removes an environment's live files once its service is gone, and the bucket deletes their noncurrent versions after 7 days. The flow that deletes an environment, and the database's grace period it waits out first, are not built yet; that flow calls the drop after the grace.

## Logs

One `datagw query` line per answer, JSON: `request_id`, `connection`, `env_id`, `snapshot_version`, `user`, `user_context` (`verified`, `schedule`, `app_only`), `outcome` (`served` or the code), `reason` (for refusals), `rows`, `bytes`, `truncated_reason`, `received_at`, `elapsed_ms`, `instance_started_at`, `cold` (the instance's first answer) and, on that first answer, `ready_ms` (process start to ready). One `datagw ready` line at start. The SQL text, parameters and rows are never logged. One `datagw file` line per file request: `request_id`, `file_op`, `env_id`, `snapshot_version`, `outcome`, `reason`, and the timing members as above. A file's name, which may say who it is about, and its link are never logged.
