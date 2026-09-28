# SSC-002 notes: what the WorkOS docs say before anything is measured

Fetched 2026-09-28 from workos.com. Quotes are verbatim. Anything the docs do not say is marked
**Unknown, must be measured** and the harness measures it.

## a. SSO profile

Source: https://workos.com/docs/reference/sso/profile

| Field | Doc text |
|---|---|
| `id` | Unique user identifier assigned by WorkOS; can be persisted as a key for identifying specific users |
| `idp_id` | "Unique identifier for the user, assigned by the Identity Provider" |
| `connection_id`, `connection_type`, `organization_id` | which connection produced the login (`connection_type` example `OktaSAML`) |
| `email`, `first_name`, `last_name`, `groups` | mapped attributes |
| `custom_attributes` | attributes mapped in the WorkOS Dashboard |
| `raw_attributes` | "Object containing unmapped attributes from the Identity Provider" |

Token exchange: `POST /sso/token` with `client_id`, `client_secret`, `grant_type=authorization_code`, `code`;
response `access_token` (JWT, 5 minutes), `profile`. Authorization: `GET /sso/authorize` with `client_id`,
`redirect_uri`, `response_type=code`, `connection` (or `organization`/`provider`), `state`, `login_hint`,
`domain_hint`. Sources: https://workos.com/docs/reference/sso/authorize and
https://workos.com/docs/reference/sso/profile/get-profile-and-token.

What `idp_id` is per provider (the docs never say it directly; this is what the required mappings imply):

| Provider | Required mapping in the integration guide | Implied `idp_id` | Source |
|---|---|---|---|
| Okta SAML | `id` -> `user.id`, `email` -> `user.email`, `firstName`, `lastName` | Okta user id (`00u...`), stable across email change | https://workos.com/docs/integrations/okta-saml |
| Entra ID SAML | NameID claim `.../claims/name` -> `user.userprincipalname`; email -> `user.mail`; no `id` mapping requested | UPN. **Changes when the email/UPN changes.** Entra also sends `objectidentifier` in `raw_attributes` when enabled; the harness checks that claim | https://workos.com/docs/integrations/entra-id-saml |
| Entra ID OIDC | ID token claims `email`, `family_name`, `given_name`; `oid`/`sub` handling not documented | **Unknown, must be measured** whether `idp_id` is `sub` (pairwise, per app) or `oid` (tenant object id) | https://workos.com/docs/integrations/entra-id-oidc |
| Google SAML | "Set **Name ID format** to **UNSPECIFIED** and **Name ID** to **Basic Information > Primary email**." and "Google SAML does not provide the option to map a user's id attribute claim." | primary email. **Not stable across email change by construction** | https://workos.com/docs/integrations/google-saml |
| Google OAuth | no claim mapping documented | **Unknown, must be measured**; expected to be Google `sub` | https://workos.com/docs/integrations/google-oauth |

## b. Directory Sync user

Source: https://workos.com/docs/reference/directory-sync/directory-user

| Field | Doc text |
|---|---|
| `id` | Unique identifier for the Directory User |
| `idp_id` | "Unique identifier for the user, assigned by the Directory Provider. Different Directory Providers use different ID formats." |
| `emails` | list of `{value, primary, type}` |
| `username`, `state` (`active`, `inactive`), `raw_attributes`, `custom_attributes`, `role`, `roles` | as named |
| `groups` | deprecated: "will default to an empty array for newly created teams starting May 1, 2026. Use the List Directory Groups endpoint with a user filter" |

List: `GET /directory_users?directory=...&limit=100&after=...`, `GET /directory_groups?directory=...` and
`GET /directory_groups?user=<directory_user_id>` for memberships (https://workos.com/docs/reference/directory-sync/directory-user/list,
https://workos.com/docs/reference/directory-sync/directory-group). Directory group `idp_id`: "Unique identifier for the group, assigned by the Directory Provider".

Per directory provider:

| Provider | Doc text | Implied user `idp_id` | Source |
|---|---|---|---|
| Entra ID SCIM | "Make sure that you are mapping `objectId` to `externalId` within the Attribute Mapping section." and for groups the SCIM `externalId` "is persisted as the `idp_id`" | Entra `objectId` | https://workos.com/docs/integrations/entra-id-scim |
| Okta SCIM | unique id not stated. "Deactivating or Deleting a User in Okta will result in a `inactive` status in connected applications (i.e., WorkOS)." and "Suspending a User in Okta will only affect their login and will not alter their status in any connected applications." | **Unknown, must be measured** (expected Okta user id or SCIM externalId) | https://workos.com/docs/integrations/okta-scim |
| Google Workspace | admin OAuth consent, not SCIM. "Google Workspace directories are synced approximately every 30 minutes starting from the time of the initial sync." Users removed when "removed or archived on Google and no longer returned by their API". Group `idp_id` is the Google group id; user id not stated | **Unknown, must be measured** (expected Google user id) | https://workos.com/docs/integrations/google-directory-sync |

## c. Is SSO `idp_id` == directory `idp_id`? (the ticket's join key)

Not stated anywhere in the docs. From the mappings above the expectation is:

- Okta: yes, both are the Okta user id. Stable across email change. **Measure.**
- Entra SAML: no. SSO `idp_id` is the UPN; directory `idp_id` is the objectId. Join must use the
  `objectidentifier` claim from `raw_attributes` (the harness looks for it). **Measure whether WorkOS passes that claim through.**
- Entra OIDC: unknown; if `idp_id` is `oid` it equals the directory objectId; if it is `sub` it does not. **Measure.**
- Google SAML: no stable key at all (email NameID). Fallback design applies.
- Google OAuth: `sub` vs Google directory user id: **Unknown, must be measured.**

JIT provisioning guidance (https://workos.com/docs/sso/jit-provisioning): "A linking field (e.g. `email`)
should be established to find a current user with the incoming WorkOS SSO Profile." and the identity "can be
linked with the existing user account via a persistent identifier in case of an email change later." WorkOS
itself therefore leaves the join to us. The AuthKit `User` object has no `idp_id` or directory link field
(https://workos.com/docs/reference/user-management/user).

## d. CLI login: device authorization flow

Exists. Source: https://workos.com/docs/authkit/cli-auth

- `POST https://api.workos.com/user_management/authorize/device`, form-encoded, body `client_id` only.
  Response: `device_code`, `user_code`, `verification_uri`, `verification_uri_complete`, `expires_in`, `interval`.
- Poll `POST /user_management/authenticate` with `grant_type=urn:ietf:params:oauth:grant-type:device_code`,
  `device_code`, `client_id`. Errors: `authorization_pending` (wait), `slow_down` (increase interval),
  `access_denied` and `expired_token` (terminal). Default interval 5 s.
- Success returns user, `organization_id`, `access_token` (JWT), `refresh_token`, `authentication_method`.
- Refresh: same endpoint, `grant_type=refresh_token`. "Public device clients omit the client secret."
  "Refresh tokens may be rotated after use, so be sure to replace the old refresh token with the newly
  returned one." (https://workos.com/docs/authkit/sessions)
- Connect (third-party) variant: `https://<authkit_domain>/oauth2/device_authorization` and `/oauth2/token`.

## e. Deactivation and revocation

- Directory events: `dsync.user.deleted` carries `state` "at time of deletion" (example `inactive`);
  Okta deactivation -> `state: inactive` (see b). https://workos.com/docs/events/directory-sync
- Sessions: `GET /user_management/users/{id}/sessions` (fields `status`, `expires_at`, `ended_at`) and
  `POST /user_management/sessions/revoke` with `session_id` ("can be extracted from the `sid` claim of the
  access token"). https://workos.com/docs/reference/authkit/session/list, https://workos.com/docs/reference/user-management/session/revoke
- Whether a directory deactivation (or a WorkOS user delete, or membership deactivation) invalidates
  refresh tokens: **Unknown, must be measured.** The docs for user delete say only "Permanently deletes a
  user in the current environment. It cannot be undone." Organization membership has `active`, `inactive`,
  `pending` with no statement about sessions. The harness measures it: refresh before and after
  deactivation, plus the sessions list. If refresh still succeeds, SSC must revoke on `dsync.user.updated`/`deleted`
  itself (fallback design, RESULTS.md).
- Access token lifetime is configurable; docs recommend keeping it short "so that changes in the session
  are quickly reflected in your app". https://workos.com/docs/authkit/sessions

## f. Data residency

**Unknown.** No data residency page was reachable on workos.com/docs on 2026-09-28 (`/docs/data-residency`
redirects to the docs root, `/docs/security/data-residency` has no such section, the API reference lists only
`https://api.workos.com`). Ask WorkOS sales whether an EU region exists and on which plan before promising it
to a European buyer. The harness reads `WORKOS_API_BASE` so a regional base URL can be tested when given.

## g. Cost

https://workos.com/pricing: "Staging environments: Free for testing. Only production environments are
billed." SSO and Directory Sync connections in production: "1–15 $125/ea" per month, falling to "51–100 $65/ea".

## SDK decision

No `workos` Python package added. The harness uses six REST calls over httpx2; the PyPI JSON fetch for the
`workos` package did not return a trustworthy upload date, so the 7-day rule could not be checked.
