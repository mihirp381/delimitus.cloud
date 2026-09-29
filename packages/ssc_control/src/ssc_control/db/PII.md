# Personal data in the control database

The written list the ticket asks for (SSC-010). The catalog test
`test_control_db.py::test_pii_columns_are_exactly_the_declared_ones` fails when a column with a
personal-data name (`display_name`, `email`, `subject`, `actor_ip`, `ip_address`, `given_name`,
`family_name`, `phone`) appears on a table not listed here, or when a listed column is missing.
The machine-readable copy is `ssc_control.db.catalog.PII_COLUMNS`.

| Table | Column | What it is | Why we hold it | Erasure |
|---|---|---|---|---|
| `user_account` | `display_name` | The person's name from the directory | Shown in the console, in `ssc access explain`, and in identity notes | Overwrite on request |
| `user_account` | `email` | Work email from the directory | Display only. Never a key: emails are reused and reassigned. The key is `identity_link` | Overwrite on request |
| `user_group` | `display_name` | Cached group name (for example "Finance") | Rendering only, never authorisation; the key is `directory_ref` | Overwrite on request |
| `identity_link` | `subject` | The identity provider's stable id for the person | The join between a login and a directory record | Delete the link |
| `audit_event` | `actor_ip` | Client address at the time of an action | Security investigation | Cannot be edited (the log is immutable); redact on export |

Also personal data, but not stored here:

- The `name` and `email` claims placed in identity notes (SSC-020) are in flight only and expire
  after five minutes.
- `metrics_event.pseudonym` is a keyed hash of the user id. It is not reversible without the key,
  which lives in the cell's Secret Manager, never in this database.

Not personal data: `idempotency_claim.key` (a client-chosen retry key; the API rejects keys longer than 200 characters and stores no request body, only its hash), `actor_id` (a `usr_…` id, opaque), `directory_ref` (a provider group id),
`org.name` (a company name).

The erasure procedure itself is an open item carried into SSC-012 (audit) and SSC-019 (directory
sync). This file records where the data is so that answering it later is a small change, not a
rewrite of the schema.
