# AI agents: known limits

Part of the trust pack. Each line is a fact from decisions 016 and 029, the API reference, or
a check in the adversarial matrix (`packages/ssc_control/tests/test_agent_adversarial.py`,
offline) or the live sweep (`python -m ssc_conformance.adversarial`). Nothing here promises
more than what is written.

## What an agent can do

An agent acts with a person's credential: from `ssc login --agent` (the `/v1` audience) or
from a remote MCP client signed in through the auth host (the `/mcp` audience). Either way it
can do no more than that person, and every write is audited as the person with
`via_agent: true` and the agent's `client_id`.

- Read what the person can see: apps, releases, what each environment runs, operations and
  builds, the data connections the person may see (names and classifications), a connection's
  tables and columns, and logs (unless the org's admins turned agent log reading off).
- Create an app, deploy a folder to preview, and roll an environment back to an earlier
  release.
- Ask: open an approval request for sharing (`agent_share`) or a data connection
  (`connect_data_source`), and withdraw a request it opened itself (decision 016).
- Have a secret set: `set_secret` takes no value and answers with the `ssc secret set` command
  for the person to run.

## What an agent cannot do

The rule: the agent interface's tools are the only agent surface. A `/v1` write that does not
back one of them answers an agent credential with `403 AGENT_SESSION_REFUSED`, even when the
person is an org admin, before any lookup, and changes nothing. They are the routes whose
responses in `openapi.json` name that code:

- Decide an approval request (`/decide`); recording a decision (`/decision`) is for SSC staff
  only, and a staff agent is refused too.
- Promote to prod, warm an app, link or unlink a repository, delete an app.
- Pull an app's kill switch, enable a stopped app, give an app to another member.
- Pause or resume a schedule, or run one now.
- Set or delete a secret, change who may read one, rotate a database password.
- Create or change a data connection, attach or detach one from an environment.
- Change the egress allowlist, the agent policy, a cell resource, or a CI token.
- Link a login to a person.

The agent interface has no tool for any of these. An admin's own session gets past the check
on every one of them (the matrix checks this too).

## What the matrix proves

Offline, against two orgs on one control plane, with an agent of org A attacking org B. 413
cases:

| Group | What it checks | Cases |
|---|---|---|
| 1. Refused routes | each route above, with an API agent token: `AGENT_SESSION_REFUSED` and no row changed in either org; the test fails when a route that names the code is missing from the probe map, or when a new write is neither refused nor listed | 55 |
| 2. Another org | every tool with B's app ids, release, operation, connection, secret name, user and bundle digest: not found or refused, nothing of B's in the answer; for B's slug, connection, secret name and user the answer is the one for a name that exists nowhere; a share or grants change naming B's user or group is refused and opens no request; B's digest does not skip A's upload | 27 |
| 3. Hostile input | every string argument of every tool, and three `/v1` inputs, with 1 MiB, control characters, `../../`, `${HOME}`, `$(id)`, a JSON object, 64 characters and a trailing dot: a validation refusal or `413`, never a `500`, and never written to the logs | 287 |
| 4. Credentials | an MCP token on `/v1`, an API token, a CI token or a refresh token on `/mcp`, a refresh token as a bearer, B's ids in A's token, a token after `/revoke`: `401` (with `resource_metadata` on `/mcp`) or not found | 9 |
| 5. Secrets | `set_secret` has no value argument; `get_logs` masks Supabase secret keys, service-role JWTs, database URLs with passwords, GitHub tokens, AWS keys and JWTs; app, status and connection answers carry no address, no `HTTPS_PROXY` and no `SSC_CONNECTION_*` value | 14 |
| 6. Approvals | decide or record a decision on its own request, on someone else's or on B's; withdraw someone else's or B's; promote, warm, repo link, egress, new connection, delete app, link login | 14 |
| 7. Rate limits | the 61st `list_apps` in a burst is `RATE_LIMITED`; a second credential of the same person is not limited; the first recovers after waiting; `/v1` sends `Retry-After` | 3 |
| 8. Idempotency | the same key with another deploy body is `IDEMPOTENCY_KEY_REUSED`; the same key and body returns the stored answer and leaves one audit row | 2 |
| 9. Audit | agent writes are recorded as the person, `via_agent: true`, with the `client_id`; refused calls leave no row | 2 |

## What only the live sweep checks

`python -m ssc_conformance.adversarial --api URL --token <token or file> --org ORG
[--victim-app SLUG_OR_ID] [--second-token ...] --report PATH` runs groups 1, 2, 4, 5, 6 and 7
against a deployed API with an `ssc login --agent` credential. It writes
`group | case | expected | got | PASS/FAIL` to the report and to `GITHUB_STEP_SUMMARY`, and
exits 1 on any failure.

- Each probe is chosen so that a missing refusal still changes nothing (an id that does not
  exist, a confirmation that does not match, a cost no cell can meet). Three probes are skipped
  when that cannot be guaranteed: rotating a database password when preview has a database,
  unlinking a repository when one is linked, and enabling a cell resource when the cell's
  database is off.
- It opens one approval request (`connect_data_source` on preview) and withdraws it. If the
  withdrawal fails, the report and standard error name the request so the operator can
  withdraw it.
- The rate check spends the credential's bucket, so it runs last; the credential is usable
  again after `Retry-After`.
- It uses the `/v1` credential only, so the tool-level checks (groups 3, 8 and 9, the MCP
  audience cases and revocation) are offline only. Another org's app is checked by id; `/v1`
  does not look apps up by slug.
- Its log check uses SSC's own redaction patterns, so it finds a credential shape the
  redaction misses only when the offline matrix names that shape.

## Gaps found (2026-10-20)

Fixed in this change, each with a test:

- `get_logs` did not mask Supabase secret keys (`sb_secret_…`).
- A revoked session's MCP credential still opened the transport: every tool call was refused,
  but with `200` and a tool error instead of the `401` that tells the client to sign in again.
- An `idempotency_key` with a control character made the tool fail with `INTERNAL`.
- A `422` wrote the refused input into the API's refusal log.
- `POST /v1/apps/{id}/kill-switch`, `POST /v1/apps/{id}/enable`, `PUT /v1/apps/{id}/owner` and
  schedule pause, resume and run accepted an agent credential.
- `request_share`, and an agent's grants change, opened an approval request for a user or group
  the org does not have (another org's included). Both are now refused, with the same answer as
  for an id that exists nowhere.

Open:

- A rate-limited tool call carries `RATE_LIMITED` but not the wait: `Retry-After` is on `/v1`
  answers only.
- An agent may withdraw its own approval request. This follows decision 016; the T4 brief
  expected a refusal.
