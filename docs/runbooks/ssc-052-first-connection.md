# SSC-052 first connection, set up by hand

Connects one customer database (Postgres) so apps can read it through the cell's data gateway, with the classification and audience ceiling the data owner chose (SSC-052). The first connections are set up this way with the customer; there is no self-service screen. Nothing here puts a database password in the control plane: the address is stored, the credentials go through the cell's secret intake.

## Before you start

- The customer's data owner has said, in writing, the classification (`internal`, `confidential` or `restricted`) and who may see the data: the whole org, or a list of groups and users. `confidential` and `restricted` need a list; the API refuses them without one (`CEILING_REQUIRED`).
- The owner is an active user of the org. They decide, with the org admins, when an app wants a wider audience than the ceiling (`exceed_ceiling`, SSC-045).
- You are an active org admin in a person session. An agent session is refused (`AGENT_SESSION_REFUSED`).
- The customer ran `packages/ssc_datagw/src/ssc_datagw/postgres_setup.sql` on the database (`docs/contracts/data-gateway.md`, "The Postgres connector") and gave you the host, port, database, the role's password and the server CA.
- The ids of the groups (`GET /v1/groups?name=`) and users (`GET /v1/users?email=`) for the ceiling.

## 1. Create the connection

As an admin, with the session token in `$TOKEN` (do not write it to a file):

```sh
curl -sS -X POST "$API/v1/connections" \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: $(uuidgen)" -H "Content-Type: application/json" \
  -d '{"name": "finance", "owner_user_id": "usr_...", "classification": "confidential",
       "ceiling": {"audience": "subjects", "subjects": [{"kind": "group", "id": "grp_..."}]},
       "allowed_schemas": ["reporting"], "limits": {"max_rows": 5000},
       "host": "db.customer.internal", "port": 5432, "database": "warehouse"}'
```

Pass: `201` and `setup_status` is `pending`. The answer, `GET /v1/connections` and the audit row `connection.created` never show the host, port or database. A `pending` connection can be granted to an environment but the data gateway does not serve it.

## 2. Give the data gateway its credentials

Follow `infra/README.md`, "Data gateway", steps for a connection secret: the cell agent ensures `ssc-conn-<20>` (the connection's `con_` id, 20 characters), the customer's `{host, port, database, user, password, ca}` goes in through the secret intake, and the operator sets `datagw_connections` to `con_<20>:<version>` and applies the cell stack. This is a live step on the customer's cell; nothing in this ticket does it.

## 3. First read, then ready

From an app environment that will be granted it (step 4), or with a throwaway app, run one real query with `ssc_app.data.query("finance", "select 1")`. Pass: a row comes back; `CONNECTION_UNAVAILABLE` means the credentials or the network are wrong (`infra/README.md`, "Data gateway" checks 6 and 9).

Then make it ready:

```sh
curl -sS -X PATCH "$API/v1/connections/finance" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"setup_status": "ready"}'
```

Pass: the next snapshot version carries the connection (`connections` in `docs/contracts/access-snapshot.md`). `{"status": "suspended"}` stops every query on it at the next snapshot and `"active"` restores it.

## 4. Grant an environment

```sh
curl -sS -X PUT "$API/v1/apps/<app>/environments/<env>/connections/finance" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"limits": {"max_rows": 1000}}'
```

Pass: `200` listing the connection; `ssc connections <app>` shows it and its `over ceiling` state. When the environment's audience is already wider than the ceiling the answer is `APPROVAL_REQUIRED`: ask `POST /v1/approvals` with `kind: exceed_ceiling`, `payload: {"connection": "finance", "grants": [<the environment's current grants>]}`; the owner or another admin decides it through the operator; then repeat the `PUT`. Approving `connect_data_source` for the name does not grant anything.

## 5. Check the ceiling

1. Share an environment that uses the connection with a person outside the ceiling (or the whole org). Pass: `APPROVAL_REQUIRED` until an `exceed_ceiling` request for that connection and that audience is approved by the owner or an admin (never the requester).
2. Lower the ceiling (`PATCH` with a shorter list). Pass: every environment now over it shows `over_ceiling_since` in `ssc connections`, with one `connection.flagged` audit row each. No approval is opened and the data gateway is not told; the flag clears when the audience is narrowed inside the ceiling.

## Know before you rely on it

- A user is inside a group ceiling when they are an active member of a listed group at the moment of the check. Nobody re-checks when directory membership changes later; a change to the ceiling or to the sharing does.
- The ceiling is not enforced at the data gateway. Flagging is the signal; narrowing the audience or suspending the connection is the action.
- Rolling back: `DELETE /v1/apps/<app>/environments/<env>/connections/finance` stops an environment reaching it; `{"status": "suspended"}` stops all of them.
