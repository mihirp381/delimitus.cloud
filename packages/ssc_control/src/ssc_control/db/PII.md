# Personal data in the control database

The written list the ticket asks for (SSC-010). The catalog test
`test_control_db.py::test_pii_columns_are_exactly_the_declared_ones` fails when a column with a
personal-data name (`display_name`, `email`, `subject`, `actor_ip`, `ip_address`, `given_name`,
`family_name`, `phone`, `decision_reason`, `repository`) appears on a table not listed here, or when a listed
column is missing.
The machine-readable copy is `ssc_control.db.catalog.PII_COLUMNS`.

| Table | Column | What it is | Why we hold it | Erasure |
|---|---|---|---|---|
| `user_account` | `display_name` | The person's name from the directory | Shown in the console, in `ssc access explain`, in identity notes, and to active org admins by `GET /v1/users?email=` | Overwrite on request |
| `user_account` | `email` | Work email from the directory | Display, and finding people to share with: `GET /v1/users?email=` returns every exact match, ignoring case, to active org admins only. Never a key: emails are reused and reassigned. The key is `identity_link` | Overwrite on request |
| `user_group` | `display_name` | Cached group name (for example "Finance") | Rendering and finding a group to share with: `GET /v1/groups?name=` returns every exact match, ignoring case, to those who may change some app's sharing. Never authorisation; the key is `directory_ref` | Overwrite on request |
| `identity_link` | `subject` | The identity provider's stable id for the person | The join between a login and a directory record | Delete the link |
| `audit_event` | `actor_ip` | Client address at the time of an action | Security investigation | Cannot be edited (the log is immutable); redact on export |
| `approval_request` | `decision_reason` | Free text an SSC operator writes when recording an approval decision; may name people from the email or chat exchange | The reason shown with the decision (SSC-045) | Overwrite on request; the audit row holds no reason |
| `audit_event` | `after.reason` | Free text an SSC operator writes with `restore-admin` (1 to 200 characters; the command refuses an `@` and any run of six digits) and that goes into the immutable log with the `operator.access` row (SSC-097) | Why the operator restored an admin; the log is the only record | Cannot be edited (the log is immutable). The runbook tells operators to name no people and quote no customer text |
| `unlinked_login` | `subject` | The SSO login's `idp_id` (for Google Workspace SAML, the person's email) when it matched no one, or more than one active person, in the directory | Shown to active org admins in the Unlinked logins list so they can link it to a person (SSC-019) | Delete the row |
| `unlinked_login` | `email` | The email the SSO login carried | The same list; display only | Delete the row |
| `repo_link` | `repository` | The connected GitHub repository as `owner/name`; the owner can be a person's GitHub login | Calling GitHub for the app's pushes and checks, and showing the connection (SSC-047). Audit rows carry the repository's numeric id instead | Disconnect the repository (deletes the row) |

Also personal data, but not stored here:

- The `name` and `email` claims placed in identity notes (SSC-020) are in flight only and expire
  after five minutes.
- `metrics_event.pseudonym` is a keyed hash of the user id (SSC-028):
  `HMAC-SHA256(org_key, user_id)`, first 32 hex characters, with
  `org_key = HMAC-SHA256(master, "ssc-metrics-v1\0" + org_id)`. The master key is
  `SSC_METRICS_KEY`, held in the API's environment (Secret Manager once SSC-013 binds it), never
  in this database, so a database copy alone cannot link a pseudonym to a person. Anyone holding
  the key can, by hashing known user ids; the key is therefore secret material.
  - Properties are flat scalars and never hold a user id or an email: the recorder refuses them
    and 0007's CHECKs refuse them again.
  - Retention: `metrics_event` is not pruned in the MVP.
  - Erasure: compute the person's pseudonym with the key and delete their rows as the migrator
    (the app role has no DELETE on the table). Every org key derives from one master, so one
    org's pseudonyms cannot be shredded by destroying a key; `PseudonymKeys` allows stored
    per-org keys later.
  - Rotation: never without a dual-write window. A new key changes every pseudonym, so counts of
    distinct users across the change would double.
- Pilot requests from delimitus.com (SSC-065, decision 028): first name, last name, work email,
  company and the free-text tools field, one JSON object each in the bucket
  `ssc-control-<stage>-pilot-requests` (the public stage's control project), not in this database.
  - Retention: a 365-day delete rule on the bucket; no versions or soft-deleted copies are kept.
  - Access: the site's service account can create objects only; reading is for the founder.
  - Erasure: on request to `privacy@delimitus.com`, find the person's object by email and delete it.
  - The service writes no access log, and its log lines name the event only; Cloud Run's platform request log is kept at the project's default retention.

Not personal data: `approval_request.recorded_by_operator` and an operator's `actor_id` (the operator credential's subject, an opaque staff id; operator credentials must not carry an email as subject), `idempotency_claim.key` (a client-chosen retry key; the API rejects keys longer than 200 characters and stores no request body, only its hash), `actor_id` (a `usr_…` id, opaque), `directory_ref` (a provider group id),
`org.name` (a company name).

`ssc.bundle` holds no personal data: digests, sizes, a commit hash, the actor tuple (opaque ids)
and the manifest the server read from the bundle, whose only free-form values are the public
build values (`build.public_env`), which are shipped to every browser by design.

`ssc.access_snapshot` and `ssc.snapshot_ack` (SSC-021) hold no personal data: org ids,
versions, digests, object keys, a cell label and timestamps. The published snapshot documents
they point at (`ssc-snapshot/v1`, `docs/contracts/access-snapshot.md`) hold ids and states only
(`usr_…`, `grp_…`, `env_…`, `gnt_…`, active or deactivated), never a name, an email, an identity
subject or a group name, so a copy of the snapshot bucket names nobody. `GET .../access`
(explain) adds the cached group name when it answers; it is not published.

`ssc.build` holds no personal data: ids, a state, a reason code, the build driver's opaque
reference and the actor tuple (opaque ids). `deployment.failure_code` is a reason code.

`ssc.audit_anchor` (SSC-012) holds no personal data: an org id, a seq, a hash, times, a reason
and an object key. The anchor objects it names (`ssc-audit-anchor/v1`, decision 012) hold the
same fields and nothing else.

`ssc.directory_connection`, `ssc.auth_session`, `ssc.login_code`, `ssc.refresh_token` and
`ssc.device_grant` (SSC-019) hold no personal data: WorkOS ids, opaque SSC ids, states, times, an
app host and SHA-256 hashes of one-time codes and tokens (never the codes or tokens). A user code
is eight random letters. `user_account.sessions_not_before` is a time.

`ssc.kill_switch_run` (SSC-025) holds no personal data: ids, a mode, states, step timings,
reason codes, schedule ids and the actor tuple (opaque ids). It has no free-text reason column.

`ssc.timer_run` (SSC-041) holds no personal data: ids, a trigger, states, reason codes, an HTTP
status, durations, times and, for a manual run, the requester as an opaque `usr_…` id. No request
or response body is stored. `schedule.path` is an app route from the manifest.

`ssc.org_index` (the single unscoped table, db/README.md rule 14) holds no personal data: an
`org_…` id and a timestamp, nothing else, so reading it across orgs reveals only how many orgs
exist and when each was created. Job arguments in `procrastinate.procrastinate_jobs.args` are
outside this catalog check, so tasks take ids only (`org_…`, `env_…`), never a name, email or
other personal value; a task reads what it needs inside `bound_org`.

The erasure procedure itself is an open item carried into SSC-012 (audit) and SSC-019 (directory
sync). This file records where the data is so that answering it later is a small change, not a
rewrite of the schema.
