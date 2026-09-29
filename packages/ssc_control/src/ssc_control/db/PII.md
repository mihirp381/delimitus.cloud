# Personal data in the control database

The written list the ticket asks for (SSC-010). The catalog test
`test_control_db.py::test_pii_columns_are_exactly_the_declared_ones` fails when a column with a
personal-data name (`display_name`, `email`, `subject`, `actor_ip`, `ip_address`, `given_name`,
`family_name`, `phone`, `decision_reason`) appears on a table not listed here, or when a listed
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

`ssc.kill_switch_run` (SSC-025) holds no personal data: ids, a mode, states, step timings,
reason codes, schedule ids and the actor tuple (opaque ids). It has no free-text reason column.

`ssc.org_index` (the single unscoped table, db/README.md rule 14) holds no personal data: an
`org_…` id and a timestamp, nothing else, so reading it across orgs reveals only how many orgs
exist and when each was created. Job arguments in `procrastinate.procrastinate_jobs.args` are
outside this catalog check, so tasks take ids only (`org_…`, `env_…`), never a name, email or
other personal value; a task reads what it needs inside `bound_org`.

The erasure procedure itself is an open item carried into SSC-012 (audit) and SSC-019 (directory
sync). This file records where the data is so that answering it later is a small change, not a
rewrite of the schema.
