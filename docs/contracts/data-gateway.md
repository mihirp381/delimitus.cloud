# Data gateway v1 (`POST /v1/connections/{name}/query`)

How an app reads a database its org connected, through the cell's data gateway `ssc-datagw` (SSC-050, C19), and keeps files through its file broker ([Files](#files), SSC-046). One service per customer, in the cell, on Cloud Run at minimum 0 and request-billed; it leaves through Direct VPC egress and the cell NAT, so the customer's database sees the cell's one fixed address. Implementations: `ssc_datagw.server` (the pipeline), `ssc_datagw.workload` (the caller), `ssc_datagw.note` (the identity note), `ssc_datagw.admission` (the snapshot), `ssc_datagw.limits` (limits, budget, slots), `ssc_datagw.connectors` (the connector seam), `ssc_datagw.kinds` (which connector serves each kind, GA-5), `ssc_datagw.postgres` and `ssc_datagw.classify` (the Postgres connector, SSC-051). Cell wiring: `infra/README.md`, "Data gateway".

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

**Records and columns.** The value at `items` (each key into an object; a missing key or a value that is not an object on the way is `QUERY_FAILED` 42P01) is the records: an array is one record per element, an object one record, anything else `QUERY_FAILED` 22P02. When the first record is an object its keys, in order, are the columns; a later record's missing key is `null`, a key that is not a column is dropped, and a later record that is not an object is a row of `null`. When the first record is not an object there is one column, `value`, holding each record as it is. No records is no columns and no rows. A column's type is that of its first non-null value in the first 100 records: boolean `boolean`, integer `integer`, other number `float`, string `string`, array or object `json`, none `string`; `db_type` is the JSON type (`boolean`, `number`, `string`, `array`, `object`, `null`). A value of another type than its column's is kept as it is. At most `max_rows` plus one rows are read, as for every connector.

The connector logs one line per read, `rest read: status=<status> bytes=<body bytes>`, with no URL. Local-proven against a TLS server in-process (`packages/ssc_datagw/tests/test_rest.py`), which also runs the connector suite.

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
