# SSC-019 login check on the local rig

Proves SSC-019's done-when against WorkOS staging tenants (Okta and Google Workspace; Entra skipped; the Google part moved to SSC-064 after the SSC-002 Google tenant was deleted) with the real API and auth host on this machine. Decision 024. The browser leg through a real app host waits for the gateway in a cell (SSC-017, then SSC-029); the ASGI tests cover it until then.

## Before you start

- Postgres you can reach as a superuser (the rig's usual `--dsn`; the checks below read the database as that superuser).
- In the WorkOS staging dashboard, add `http://localhost:8100/callback` as a redirect URI (or run `auth` with `--port P --public-url http://127.0.0.1:P` to match one already registered).
- Each WorkOS organisation has a verified domain matching the users' emails (dashboard, or `PUT /organizations/<org_01…>` with `domain_data`). Without one, WorkOS refuses every sign-in with `profile_not_allowed_outside_organization` and the auth host shows the refused page.
- From WorkOS, for each tenant: the organisation id (`org_01…`), the directory id (`directory_01…`), the SSO connection id (`conn_01…`), and one directory user's `idp_id` and email to be the founder.
- Export `SSC_WORKOS_API_KEY` and `SSC_WORKOS_CLIENT_ID` in the shell. Never write them to a file.

## 1. Start the rig

```sh
eval "$(uv run python tools/dev_stack.py up --dsn postgresql://postgres:pw@localhost/postgres)"
uv run python tools/dev_stack.py sso-org --org-name Okta --workos-org <okta org_01…> \
    --directory <okta directory_01…> --sso <okta conn_01…> --join-rule idp_id \
    --founder-subject <founder's Okta idp_id, 00u…> --founder-email <founder email>
uv run python tools/dev_stack.py sso-org --org-name Google --workos-org <google org_01…> \
    --directory <google directory_01…> --sso <google conn_01…> --join-rule email \
    --founder-subject <founder's Google directory idp_id, numeric> --founder-email <founder email>
```

Each `sso-org` prints `SSC_ORG_ID=org_…`; note both. Then, in two terminals:

```sh
uv run python tools/dev_stack.py serve --port 8000
uv run python tools/dev_stack.py auth --port 8100     # also syncs both directories every minute
```

Wait for the first sync tick in the `auth` log, which lists each directory in full.

## 2. Login works (Okta, then Google)

For each org, as a directory user who is not the founder:

```sh
uv run ssc --api http://127.0.0.1:8000 login --org <org_…> --auth-url http://localhost:8100
uv run ssc --api http://127.0.0.1:8000 whoami
```

Pass: the browser opens, sign-in goes through the IdP, the code shown in the terminal is approved, and `whoami` names a `usr_` id in that org. Note the id. If the sign-in page refuses, the reason is in the `auth` log (`login refused …`) and in the org's audit chain as `login.failed`.

## 3. A deactivated user is locked out within 5 minutes

1. Keep the session from step 2 logged in. Run `whoami` once to confirm.
2. Deactivate the user in the IdP (Okta: Deactivate, not Suspend, which never reaches WorkOS; Google: Suspend). Okta's Deactivate and unassign both reach WorkOS as `dsync.user.deleted`. Note the time WorkOS shows the `dsync.user.updated` or `dsync.user.deleted` event in the dashboard's Events log. Google directories reach WorkOS up to about 30 minutes after the admin acts; the clock starts at the WorkOS event.
3. Run `whoami` every 15 seconds until it fails.

Pass: `whoami` fails with `LOGIN_ENDED` or `UNAUTHENTICATED` within 5 minutes of the WorkOS event (expected: within about 60 s). Then repeat with removal (Okta: unassign from the app; Google: delete the user) for a second user.

## 4. An email change does not create a second user (Okta)

1. Count the org's people:
   `psql postgresql://postgres:pw@localhost/postgres -c "select count(*) from ssc.user_account where org_id = '<okta org_…>'"`
2. Change a logged-in user's primary email in Okta. Wait for the next sync tick in the `auth` log.
3. Log in again as that user and run `whoami`.

Pass: `whoami` names the same `usr_` id as before, and the count is unchanged. The new email shows in the same `psql` with `select email from ssc.user_account where id = '<usr_…>'`.

## Record

Copy the times, the `usr_` ids and the pass or fail lines into the SSC-019 status in the tickets file. Stop both servers with Ctrl-C; `.ssc-dev/` keeps the state for a rerun.
