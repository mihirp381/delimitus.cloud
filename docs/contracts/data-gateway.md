# Data gateway v1 (`POST /v1/connections/{name}/query`)

How an app reads a database its org connected, through the cell's data gateway `ssc-datagw` (SSC-050, C19), and keeps files through its file broker ([Files](#files), SSC-046). One service per customer, in the cell, on Cloud Run at minimum 0 and request-billed; it leaves through Direct VPC egress and the cell NAT, so the customer's database sees the cell's one fixed address. Implementations: `ssc_datagw.server` (the pipeline), `ssc_datagw.workload` (the caller), `ssc_datagw.note` (the identity note), `ssc_datagw.admission` (the snapshot), `ssc_datagw.limits` (limits, budget, slots), `ssc_datagw.connectors` (the connector seam), `ssc_datagw.postgres` and `ssc_datagw.classify` (the Postgres connector, SSC-051). Cell wiring: `infra/README.md`, "Data gateway".

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
| `CONNECTION_UNAVAILABLE` | 503 | execute | platform | The database could not be reached, the cell has no `SSC_CONNECTION_*` for it, or the session is not what it must be (see the Postgres connector). |

When a running query is ended because a newer snapshot no longer admits it, the answer is that snapshot's refusal (`APP_NOT_ACTIVE`, `CONNECTION_SUSPENDED`, `CONNECTION_NOT_GRANTED`, `UNKNOWN_ENVIRONMENT` or `DATA_SNAPSHOT_STALE`) with stage `execute`.

## The workload token

A Google ID token (RS256, keys from `https://www.googleapis.com/oauth2/v3/certs`, cached for an hour and fetched again at most every 30 s for an unknown `kid`) with `iss` `https://accounts.google.com` or `accounts.google.com`, `aud` exactly `SSC_DATAGW_AUDIENCE`, `exp`, `iat`, `sub`, `email_verified` true, and `email` `ssc-a-<20>@<cell project>.iam.gserviceaccount.com`, which names the environment `env_<20>`. The metadata server leaves `email` and `email_verified` out unless the app asks with `format=full` (`/computeMetadata/v1/instance/service-accounts/default/identity?audience=...&format=full`), and such a token is refused; `ssc_app.workload.WorkloadToken` asks correctly and caches the token until 5 minutes before it expires. Any service account anywhere can mint a token for the audience, so a token from another project, another account of the cell or a person is refused.

## The identity note

When present, `X-SSC-Identity` is verified as in `docs/contracts/identity-note.md`, with the cell JWKS (`SSC_IDENTITY_JWKS`), the cell issuer, and the caller environment's own origin as the audience (its host label from the snapshot's `hosts`). Its `org`, `app` and `env` must be the caller's. A `usr_` subject must be active in the snapshot; a schedule (`sch_`) note is logged as `schedule`. The note is optional, so the app can query for itself (`app_only`); a note that is present and does not verify is refused.

## The Postgres connector

Each `SSC_CONNECTION_CON_<20>` variable on the service (the connection id in upper case) is JSON `{host, port, database, user, password, ca}` and makes that connection a Postgres connection (`ssc_datagw.postgres.PostgresTarget`; unknown members are refused, and a bad variable stops the service at start, naming the variable and the fields, never the value). The cell holds the value as the secret `ssc-conn-<20>`, written through the cell's secret intake and tagged `ssc-secret-kind=connection` when the agent creates it; the variable is a Cloud Run secret variable pinned to one version (the cell setting `datagw_connections`). The cell's deny rule refuses `ssc-data` every secret without that tag, so the service can read connection credentials and no app secret (`infra/README.md`, "Data gateway"). Each query gets its own connection and runs in this order; the first step that refuses answers:

1. **Classify.** sqlglot parses the text as Postgres; anything but exactly one `SELECT` (or `UNION`, `INTERSECT`, `EXCEPT` of them) is `QUERY_REFUSED`: a second statement, a data-changing `WITH`, `SELECT INTO`, `FOR UPDATE`/`SHARE`, `SET`, `NOTIFY`, `LISTEN`, `COPY`, `DO`, and any call to a function that writes, signals or ends other sessions, reads server files, or runs a query given as text (`query_to_xml` and the other `*_to_xml`, `ts_stat`, `ts_rewrite`, `pg_terminate_backend`, `pg_cancel_backend`, `pg_notify`, `set_config`, `nextval`, `dblink*`, `lo_*`, `pg_advisory*`, `pg_read_*`, `pg_ls_*`, and others: `ssc_datagw.classify.DENIED`). `E'...'` strings, dollar-quoted strings and `U&"..."` function names are refused, since sqlglot and Postgres can read them apart; text sqlglot cannot parse is refused.
2. **Connect over TLS.** With a pasted `ca`, the server's chain must lead to it and the name is not checked (`verify-ca`, as for a Cloud SQL certificate); without one, the system trust store and the host name decide (`verify-full`). There is no plaintext and no unverified mode. The startup packet sets `application_name=ssc-datagw`, `default_transaction_read_only=on`, `standard_conforming_strings=on` and `idle_in_transaction_session_timeout=60s`. 10 s to connect.
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

A grant is one connection and one environment. Within the day's budget a result is cut at what is left (`daily_rows`, `daily_bytes`); with nothing left the query is refused. **v1 counts the budget and the slots per instance**: with several instances a grant can exceed them by up to that many times. A shared count needs storage the cell does not have yet.

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
