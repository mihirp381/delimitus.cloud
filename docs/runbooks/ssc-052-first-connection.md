# SSC-052 first connection, set up by hand

Connects one customer database (Postgres) so apps can read it through the cell's data gateway, with the classification and audience ceiling the data owner chose (SSC-052). The first connections are set up this way with the customer; there is no self-service screen. Nothing here puts a database password in the control plane: the address is stored, the credentials go through the cell's secret intake.

## Before you start

- The customer's data owner has said, in writing, the classification (`internal`, `confidential` or `restricted`) and who may see the data: the whole org, or a list of groups and users. `confidential` and `restricted` need a list; the API refuses them without one (`CEILING_REQUIRED`).
- The owner is an active user of the org. They decide, with the org admins, when an app wants a wider audience than the ceiling (`exceed_ceiling`, SSC-045).
- You are an active org admin in a person session. An agent session is refused (`AGENT_SESSION_REFUSED`).
- The customer ran `packages/ssc_datagw/src/ssc_datagw/postgres_setup.sql` on the database (`docs/contracts/data-gateway.md`, "The Postgres connector") and gave you the host, port, database, the role's password and the server CA.
- For a MySQL connection the customer ran `packages/ssc_datagw/src/ssc_datagw/mysql_setup.sql` instead (`docs/contracts/data-gateway.md`, "The MySQL connector") and gave you the host, port, a database the user may read, the user's password and the server CA.
- For a SQL Server connection the customer ran `packages/ssc_datagw/src/ssc_datagw/sqlserver_setup.sql` with `sqlcmd`, as a sysadmin, in the connection's database (`docs/contracts/data-gateway.md`, "The SQL Server connector") and gave you the host, port, the database, the login, its password and the server CA (required).
- For a `gsheets` connection, the customer shared the spreadsheet with the service account's email as a viewer and gave you the service-account JSON key and the spreadsheet id (`docs/contracts/data-gateway.md`, "The Google Sheets connector").
- For an `s3` connection, the customer created an IAM user with the policy from `packages/ssc_datagw/src/ssc_datagw/s3_policy.json` attached (`<bucket>` and `<prefix>` filled in) and gave you its access key id and secret access key, the bucket, its region and the prefix (`docs/contracts/data-gateway.md`, "The S3 connector").
- For a `gcs` connection, the customer granted the service account `roles/storage.objectViewer` on the bucket with the condition that keeps reads under the prefix (the `gcloud storage buckets add-iam-policy-binding` command in `docs/contracts/data-gateway.md`, "The GCS connector") and gave you the service-account JSON key, the bucket and the prefix.
- For a `bigquery` connection, the customer granted the service account BigQuery Job User on the project and BigQuery Data Viewer on the dataset and gave you the service-account JSON key, the project, the dataset and the location (`docs/contracts/data-gateway.md`, "The BigQuery connector").
- For an `airtable` connection, the customer created a personal access token with the `data.records:read` scope, its access restricted to the base, and gave you the token, the base id and, optionally, the table (`docs/contracts/data-gateway.md`, "The Airtable connector").
- The ids of the groups (`GET /v1/groups?name=`) and users (`GET /v1/users?email=`) for the ceiling.

## 1. Create the connection

As an admin, with the session token in `$TOKEN` (do not write it to a file):

```sh
curl -sS -X POST "$API/v1/connections" \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: $(uuidgen)" -H "Content-Type: application/json" \
  -d '{"name": "finance", "owner_user_id": "usr_...", "classification": "confidential",
       "ceiling": {"audience": "subjects", "subjects": [{"kind": "group", "id": "grp_..."}]},
       "allowed_schemas": ["reporting"], "limits": {"max_rows": 5000},
       "kind": "postgres", "address": {"host": "db.customer.internal", "port": 5432, "database": "warehouse"}}'
```

`kind` is one of `ssc_contracts.connections.KINDS` and `address` the kind's non-secret address (the API's `ConnectionIn.address` lists each kind's members; `port` defaults to the engine's). A kind without a connector yet is refused with `CONNECTOR_UNAVAILABLE`; the console offers only the available ones. For the SQL kinds, `host`, `port` and `database` at the top level still work in place of `address`.

Pass: `201` and `setup_status` is `pending`. The answer, `GET /v1/connections` and the audit row `connection.created` show the kind and never the address. A `pending` connection can be granted to an environment but the data gateway does not serve it (step 3).

## 2. Give the data gateway its credentials

Follow `infra/README.md`, "Data gateway", steps for a connection secret: the cell agent ensures `ssc-conn-<20>` (the connection's `con_` id, 20 characters), the customer's `{host, port, database, user, password, ca}` goes in through the secret intake, and the operator sets `datagw_connections` to `con_<20>:<version>` and applies the cell stack. This is a live step on the customer's cell; nothing in this ticket does it.

## 3. Ready, then grant, then the first read

The data gateway serves only `ready` connections: the snapshot leaves a `pending` one out, so a query on it is refused `CONNECTION_NOT_GRANTED`, which says nothing about the credentials. `ready` is therefore set before the first read, not after it (GA-5.1); until an environment is granted the connection in step 4, no app can reach it, so the order is safe.

```sh
curl -sS -X PATCH "$API/v1/connections/finance" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"setup_status": "ready"}'
```

Pass: the next snapshot version carries the connection (`connections` in `docs/contracts/access-snapshot.md`). `{"status": "suspended"}` stops every query on it at the next snapshot and `"active"` restores it; if the first read goes wrong, `pending` takes it out of the snapshot again.

## 4. Grant an environment

```sh
curl -sS -X PUT "$API/v1/apps/<app>/environments/<env>/connections/finance" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"limits": {"max_rows": 1000}}'
```

Pass: `200` listing the connection; `ssc connections <app>` shows it and its `over ceiling` state. When the environment's audience is already wider than the ceiling the answer is `APPROVAL_REQUIRED`: ask `POST /v1/approvals` with `kind: exceed_ceiling`, `payload: {"connection": "finance", "grants": [<the environment's current grants>]}`; the owner or another admin decides it through the operator; then repeat the `PUT`. Approving `connect_data_source` for the name does not grant anything. For the first read a throwaway app's environment, shared with nobody but you, is the right grantee.

## 5. The first read

From the granted environment, run one real query with `ssc_app.data.query("finance", "select 1")`, then one on the customer's data, `select * from reporting.<a table> limit 5`. Pass: rows come back (a read is logged by the data gateway in the cell, never in the org's audit chain, which records the grant and the approvals). If not, the code says who acts; none of the first four is a credential problem:

| Answer | Means | Do |
|---|---|---|
| `CONNECTION_NOT_GRANTED` | the connection is `pending`, the grant is missing, or the snapshot is not there yet (under a minute after the change) | step 3, step 4, wait once |
| `CONNECTION_SUSPENDED` | `status` is `suspended` | `PATCH {"status": "active"}` |
| `APP_NOT_ACTIVE` | the app is disabled or quarantined | `ssc enable <app>` |
| `DATA_SNAPSHOT_STALE` | the data gateway has not read a snapshot for 120 s | platform: `infra/README.md`, "Data gateway", snapshot |
| `CONNECTION_UNAVAILABLE` | the credentials, the CA, the address or the network are wrong, or a pooler sits between (`a pooler is between` in the gateway log) | `infra/README.md`, "Data gateway" checks 6, 7 and 9; re-enter the secret (step 2) |
| `QUERY_FAILED` 42501 | the role may not read that schema or table | the customer runs `postgres_setup.sql` again with the schema named |
| `QUERY_FAILED` 42000 (MySQL) | the user may not read that schema or table (MySQL answers 42000 where Postgres answers 42501) | the customer runs `mysql_setup.sql` again with the schema named in `@schemas` |
| `QUERY_FAILED` 42501 (SQL Server) | the login may not read that schema or table (errors 229 and 230) | the customer runs `sqlserver_setup.sql` again with the schema named in `schemas` |
| `QUERY_FAILED` 42P01 | no such table | the query |
| `QUERY_REFUSED` | the text is not one plain read | the query; `docs/contracts/data-gateway.md`, "The Postgres connector", step 1 |

A failure here never reaches a user: only the throwaway environment is granted. Set `{"setup_status": "pending"}` while you fix it and `ready` again after.

## 6. Check the ceiling

1. Share an environment that uses the connection with a person outside the ceiling (or the whole org). Pass: `APPROVAL_REQUIRED` until an `exceed_ceiling` request for that connection and that audience is approved by the owner or an admin (never the requester).
2. Lower the ceiling (`PATCH` with a shorter list). Pass: every environment now over it shows `over_ceiling_since` in `ssc connections`, with one `connection.flagged` audit row each. No approval is opened and the data gateway is not told; the flag clears when the audience is narrowed inside the ceiling.

## Know before you rely on it

- A user is inside a group ceiling when they are an active member of a listed group at the moment of the check. Nobody re-checks when directory membership changes later; a change to the ceiling or to the sharing does.
- `max_rows`, `max_bytes` and `timeout_ms` are exact. `concurrency`, `daily_rows` and `daily_bytes` are advisory: each data gateway instance counts its own, and up to ten run, so a grant can reach up to ten times them (decision 034). Say so to the data owner when they set them.
- The ceiling is not enforced at the data gateway. Flagging is the signal; narrowing the audience or suspending the connection is the action.
- Rolling back: `DELETE /v1/apps/<app>/environments/<env>/connections/finance` stops an environment reaching it; `{"status": "suspended"}` stops all of them.
