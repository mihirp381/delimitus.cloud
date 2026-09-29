# Access snapshot v1 (`ssc-snapshot/v1`)

Who may reach which app environment of one org, as a document the gateway loads. Frozen contract (SSC-021, decision 019). It changes by adding v2 beside it, never by editing v1. Implementations: `ssc_contracts.snapshot` (the model), `ssc_control.snapshot` (compile, publish, acknowledge), `ssc_shared.access` (the one evaluator, used by the control plane's explain endpoint and by the gateway, SSC-018).

## Document

One JSON object, published as RFC 8785 canonical JSON (`ssc_shared.canonical`). Unknown members are refused at every level.

| Member | Type | Meaning |
|---|---|---|
| `format` | string | `ssc-snapshot/v1`. |
| `org_id` | string | `org_…`. A holder refuses a document for another org. |
| `version` | int | 1 to 2^53−1, one more than the org's previous published version. `0` means a live evaluation (explain) and is never published. |
| `compiled_at` | string | RFC 3339 timestamp with offset. |
| `environments` | object | `env_…` → `{app_id, name, status, floor}`. `name` is `prod` or `preview`; `status` is the app's, `active`, `disabled` or `quarantined`; `floor` is the least role that grants access, `user` for prod and `builder` for preview. Every environment of the org is listed. |
| `hosts` | object | host label → `env_…`. Empty until the host label rules exist (SSC-013, decision 004). |
| `grants` | object | `env_…` → list of `{grant_id, role, subject_kind, subject_id}`. `role` is `builder` or `user`; `subject_kind` is `user` (`subject_id` a `usr_…`), `group` (a `grp_…`) or `org` (`subject_id` null). |
| `groups_by_user` | object | `usr_…` → list of `grp_…`: every group membership in the org. |
| `users` | object | `usr_…` → `{status}`, `active` or `deactivated`: every user in the org. |
| `ceiling` | null | Reserved for the audience ceiling (SSC-052). Always null in v1. |

References must resolve or the document is refused: every `grants` key and `hosts` value is in `environments`, every `groups_by_user` key and every user grant's subject is in `users`. The document holds ids and states only: no name, email, identity subject or group name.

## Decision

`decide(view, environment_id, user_id)` returns `{allowed, role, via, reason}` and never raises. The checks run in this order and stop at the first that refuses:

| Reason | When |
|---|---|
| `no_view` | no valid snapshot has been loaded |
| `unknown_environment` | the environment is not in `environments` |
| `app_not_active` | the environment's `status` is not `active` |
| `user_not_active` | the user is not in `users`, or is `deactivated` |
| `no_grant` | no grant names the user, one of the user's groups, or the org |
| `below_floor` | grants match, but none has a role at or above the floor; `via` lists them |
| `granted` | allowed; `role` is the best counted role and `via` every counted grant, by `grant_id` |

There is no owner or org-admin shortcut: the app's owner and admins need a grant like everyone else. (They may always change sharing; that is the control plane's rule, not the gateway's.)

## Publishing

- Objects: `snapshots/<org_id>/v<version>-<sha256 first 12 hex>.json`, written before the version's row in `ssc.access_snapshot` commits. A publish that rolls back leaves an object no row names, and no reader ever loads it.
- Pointer: `snapshots/<org_id>/latest.json` is `{"version", "key", "digest"}` (canonical JSON), moved only after the row commits, re-read until it names the newest committed version. A reader fetches the pointer, then the object, and checks the object's sha256 against `digest`.
- Holding: `ViewHolder.apply` builds and validates the new view before swapping; an invalid document, one for another org, or version 0 is refused and the current view stays. A version not newer than the current one is ignored.
- Acknowledgement: the cell reports the version it has applied in `POST /internal/v1/heartbeat` (`snapshot_version`). An unpublished version is `REFERENCE_NOT_FOUND`; a heartbeat from a cell other than the org's is `FORBIDDEN`. The latest report is stored as is, even if lower than the one before.
