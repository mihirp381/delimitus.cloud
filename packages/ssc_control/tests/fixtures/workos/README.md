# WorkOS response fixtures

Read from the WorkOS API reference on 2026-10-24; never recorded from the live API. Ids, names
and addresses are invented.

| Endpoint | Reference | What SSC relies on |
| --- | --- | --- |
| `POST /organizations` | https://workos.com/docs/reference/organization/create | Body `name` and `domain_data: [{domain, state}]`; `state` is `pending` or `verified`. SSC sends `pending` (claimed, not verified). Returns the organization; SSC keeps `id`. The page names no status for a domain already in use. |
| `POST /portal/generate_link` | https://workos.com/docs/reference/admin-portal/portal-link/generate | Body `organization`, `intent` (`sso`, `dsync`, others), optional `return_url` and `success_url`. Returns `{"link": ...}`. |
| `GET /connections?organization_id=` | https://workos.com/docs/reference/sso/connection/list | Paged (`limit` 1 to 100, `list_metadata.after`). Each has `id`, `connection_type` (such as `OktaSAML`, `GoogleSAML`), `state` (`requires_type`, `draft`, `active`, list marked incomplete). Usable: `active`. |
| `GET /directories?organization_id=` | https://workos.com/docs/reference/directory-sync/directory/list | Paged. Each has `id`, `type`, `state` (`linked`, `validating`, `invalid_credentials`, `unlinked`, `deleting`). Usable: `linked`. Google Workspace is `gsuite directory`. The type list is truncated (`azure scim v2.0`, `bamboohr`, `breathe hr`, ...); Okta is not shown, so `okta scim v2.0` follows the documented `<provider> scim v2.0` naming (`azure scim v2.0`, and `generic scim v2.0` in https://workos.com/docs/events/directory-sync) and is to be confirmed on the staging tenant. |
| `GET /directory_users?directory=&email=` | https://workos.com/docs/reference/directory-sync/directory-user/list | `email` filters by primary email and requires `directory`; the page does not say whether it ignores case. Each user has `email`; `emails` is optional and deprecated. |

Note: the `dsync.activated` event example (https://workos.com/docs/events/directory-sync)
shows the directory `state` as `active`, while the directory object lists `linked`. The
fixtures follow the directory object.

Files: `organization_create.json`, `portal_link.json`, `connections_list.json`,
`directories_list.json`, `directory_users_by_email.json`.
