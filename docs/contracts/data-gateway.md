# Data gateway v1 (`POST /v1/connections/{name}/query`)

How an app reads a database its org connected, through the cell's data gateway `ssc-datagw` (SSC-050, C19). One service per customer, in the cell, on Cloud Run at minimum 0 and request-billed; it leaves through Direct VPC egress and the cell NAT, so the customer's database sees the cell's one fixed address. Implementations: `ssc_datagw.server` (the pipeline), `ssc_datagw.workload` (the caller), `ssc_datagw.note` (the identity note), `ssc_datagw.admission` (the snapshot), `ssc_datagw.limits` (limits, budget, slots), `ssc_datagw.connectors` (the connector seam; the Postgres connector is SSC-051). Cell wiring: `infra/README.md`, "Data gateway".

## Request

`POST /v1/connections/{name}/query`, where `name` is the connection's name in the org (`^[a-z][a-z0-9-]{0,62}$`, `connections` in `docs/contracts/access-snapshot.md`).

| Header | Rule |
|---|---|
| `Authorization` | `Bearer <Google ID token>`, minted by the app's own service account from the metadata server for the audience `SSC_DATAGW_AUDIENCE` (the data gateway's `run.app` URL). Required. Cloud Run's invoker check is off on this service; the data gateway checks the token itself. |
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
| `VALIDATION_FAILED` | 422 | request | app | The body is not a query as above. |
| `DAILY_BUDGET_SPENT` | 429 | limits | admin | The grant has used today's rows or bytes; resets at 00:00 UTC. |
| `CONCURRENCY_LIMIT` | 429 | limits | app | Every slot of the grant stayed taken for 2 s. |
| `QUERY_REFUSED` | 422 | classify | app | The connector's classifier refused the statement (not one plain read). |
| `QUERY_FAILED` | 422 | execute | app | The database refused the statement. |
| `QUERY_TIMEOUT` | 408 | execute | app | The read ran past `timeout_ms` plus a 2 s grace. |
| `CONNECTION_UNAVAILABLE` | 503 | execute | platform | The database could not be reached, or the cell has no connector for it yet. |

When a running query is ended because a newer snapshot no longer admits it, the answer is that snapshot's refusal (`APP_NOT_ACTIVE`, `CONNECTION_SUSPENDED`, `CONNECTION_NOT_GRANTED`, `UNKNOWN_ENVIRONMENT` or `DATA_SNAPSHOT_STALE`) with stage `execute`.

## The workload token

A Google ID token (RS256, keys from `https://www.googleapis.com/oauth2/v3/certs`, cached for an hour and fetched again at most every 30 s for an unknown `kid`) with `iss` `https://accounts.google.com` or `accounts.google.com`, `aud` exactly `SSC_DATAGW_AUDIENCE`, `exp`, `iat`, `sub`, `email_verified` true, and `email` `ssc-a-<20>@<cell project>.iam.gserviceaccount.com`, which names the environment `env_<20>`. Any service account anywhere can mint a token for the audience, so a token from another project, another account of the cell or a person is refused.

## The identity note

When present, `X-SSC-Identity` is verified as in `docs/contracts/identity-note.md`, with the cell JWKS (`SSC_IDENTITY_JWKS`), the cell issuer, and the caller environment's own origin as the audience (its host label from the snapshot's `hosts`). Its `org`, `app` and `env` must be the caller's. A `usr_` subject must be active in the snapshot; a schedule (`sch_`) note is logged as `schedule`. The note is optional, so the app can query for itself (`app_only`); a note that is present and does not verify is refused.

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

## Logs

One `datagw query` line per answer, JSON: `request_id`, `connection`, `env_id`, `snapshot_version`, `user`, `user_context` (`verified`, `schedule`, `app_only`), `outcome` (`served` or the code), `reason` (for refusals), `rows`, `bytes`, `truncated_reason`, `received_at`, `elapsed_ms`, `instance_started_at`, `cold` (the instance's first answer) and, on that first answer, `ready_ms` (process start to ready). One `datagw ready` line at start. The SQL text, parameters and rows are never logged.
