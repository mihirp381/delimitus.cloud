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

## If a sign-in says "Too many tries"

The auth host's abuse limits (decision 024, amendment of 2026-10-19; table in
`docs/trust/identity-limits.md`) answer `429`: in a browser, the "Too many tries" page; at
`/token`, `/revoke` and `/internal/redeem`, `{"error": "invalid_request", "error_description":
"too many token requests"}` with `retry-after: 3600`. The `auth` log has one warning naming the
route and the address or org (`/login refused: too many requests for address …`), never a code
or token. Rerunning the checks above many times from one machine can reach them: wait up to an
hour or restart `auth`, which keeps its counts only in memory. There is no operator override in
production; the person waits up to an hour or signs in from another network.

## Invite codes

A pilot user without an org id signs in to the console with an invite code (decision 029,
amendment of 2026-10-26). With `SSC_DATABASE_DSN` set:

```
uv run python -m ssc_control.identity invite create --org <org_…> --operator op_<you> --expires-in 14d --max-uses 10 [--note "pilot cohort"]
uv run python -m ssc_control.identity invite list --org <org_…>
uv run python -m ssc_control.identity invite revoke <pinv_…> --operator op_<you>
```

`create` prints the code once on stdout (the row goes to stderr); only its SHA-256 is kept, so
a lost code is revoked and made again. A revoked, expired or used-up code gets the same "We
could not find a sign-in" answer as a wrong one.

## Pilot tenant branded as Delimitus

The pilot org signs in through an Okta tenant Delimitus runs, branded so the person sees
Delimitus on the console card, Delimitus on the password page, then the console. Nothing in SSC
is Okta-specific: the tenant is an ordinary WorkOS organisation and SSC org. Steps marked
**[real]** create or change a real resource; run them in order. No value from Okta or WorkOS
goes into a file, a ticket or this runbook.

### 1. The Okta tenant **[real]**

1. Sign up for an Okta developer org at `developer.okta.com` with an operator's Delimitus
   address. Turn on MFA for that admin account.
2. Customizations → Brands → the default brand: set the brand name to `Delimitus`.

### 2. Custom domain **[real]**

1. Customizations → Brands → Delimitus → Domains → Add domain: `signin.delimitus.com`,
   certificate **Okta-managed**. Okta lists a TXT record (ownership) and a CNAME (the host).
2. Add both to the `delimitus` Cloud DNS zone in `ssc-platform-0`, with the names and values
   exactly as Okta shows them:

   ```sh
   gcloud dns record-sets create <TXT name Okta shows>. --zone=delimitus --project=ssc-platform-0 \
     --type=TXT --ttl=300 --rrdatas='"<TXT value>"'
   gcloud dns record-sets create signin.delimitus.com. --zone=delimitus --project=ssc-platform-0 \
     --type=CNAME --ttl=300 --rrdatas=<CNAME target Okta shows>.
   ```

3. Back in Okta, Verify. The certificate is issued once both records resolve.

Pass: `dig +short CNAME signin.delimitus.com` prints Okta's target, Okta shows the domain
verified, and `https://signin.delimitus.com` opens the sign-in page with a valid certificate.

### 3. Brand **[real]**

1. Theme: upload the Delimitus logo (the mark from `console/src/components/Brand.tsx` with the
   word Delimitus, as a PNG) and a favicon; primary colour `#0071e3`, secondary `#1d1d1f`,
   background `#f5f5f7` (the console's `accent`, `textPrimary` and `surfaceSunken` tokens,
   `console/src/tokens/tokens.css`).
2. Sign-in page: title `Sign in to Delimitus`, the help and footer links pointed at
   `delimitus.com` or removed, and every "Okta" removed (page title, footer, "Powered by").
   The code editor needs the custom domain from step 2.
3. Error page: the same theme and no Okta name.
4. Emails: sender name `Delimitus`; a custom email domain under `delimitus.com` (add the
   records Okta lists to the same zone as in step 2; if Okta asks for an SPF TXT on a name that
   already has one, merge the include into that record instead of adding a second). Edit every
   template a pilot user can receive (activation, password reset, MFA, account unlock) so no
   subject or body names Okta.

Pass: in a private window, `https://signin.delimitus.com` and a test activation mail show
Delimitus and no Okta name.

### 4. WorkOS and SCIM **[real]**

1. Create the WorkOS organisation for the pilot in the production dashboard and verify a domain
   for it that the pilot accounts' emails use (a Delimitus subdomain, verified by its TXT record
   in the same zone). WorkOS's default domain policy refuses a profile whose email is outside
   the organisation's domains (Before you start, above); SSC does not check the email domain
   itself, and WorkOS support can relax the policy. Keep the default, so pilot users get
   accounts on that domain, not their own company's.
2. Connect the tenant as SAML (or OIDC) and Okta SCIM directory sync, as in Before you start
   and `docs/runbooks/ssc-110-onboarding.md` step 2 (the Admin Portal links, followed here as
   the tenant's admin). Create the Okta app after step 2 so its sign-on URL is on
   `signin.delimitus.com`; check that the metadata WorkOS shows starts there.
3. Assign the pilot users (and the founder) to the app in Okta; wait for WorkOS to list them as
   directory users.
4. Note the organisation, directory and connection ids and the founder's `idp_id` from WorkOS
   (Before you start; `docs/runbooks/ssc-064-control-plane.md` step 10 has the `curl` that
   lists them).

### 5. The pilot org **[real]**

With the database reachable as in `docs/runbooks/ssc-064-control-plane.md` step 10 (production
un-parked, `SSC_DATABASE_DSN` as `ssc_app`, both WorkOS settings read into the shell):

```sh
ORG=$(uv run python -m ssc_control.identity create-org --name "Delimitus pilot" \
  --founder-name "<founder name>" --founder-email "<founder email>" --founder-idp-id "<founder idp_id>" \
  --operator op_<you> --workos-org <org_01…> --directory <directory_01…> --sso <conn_01…> \
  --join-rule idp_id --cell-label l$(openssl rand -hex 6))
echo $ORG
```

Pass: the org id on stdout and `ok` for the founder on stderr. A refusal writes nothing and
says why. Building the org's cell for apps follows `docs/runbooks/ssc-110-onboarding.md`; the
sign-in check below needs no cell.

Then mint the pilot's invite codes as in Invite codes, above (`invite create --org $ORG …`), and
hand each code to its person by a channel other than this runbook.

### 6. First sign-in **[real]**

In a private window, as a pilot user who is not the founder:

1. `https://console.delimitus.com/login`: enter the org id (or an invite code), Continue.
2. The browser reaches `https://signin.delimitus.com`: Delimitus logo and colours, no Okta name.
3. Sign in with the Okta username and password (and MFA if set).
4. The console opens signed in; reload and it stays signed in without the card.

Pass: all four. "We could not find a sign-in for that organisation or code" on the card means
the hint missed: a wrong id, a revoked, expired or used-up code, a frozen connection or too many
tries. It never says which; check the id, `invite list --org $ORG` and the WorkOS connection.
Record the date, the pass or fail and the person's `usr_` id under Record.

### 7. Teardown **[real]**

1. Revoke every live code: `invite list --org $ORG`, then `invite revoke <pinv_…> --operator
   op_<you>` for each.
2. Retire the org as the existing runbooks allow: delete the directory in WorkOS (sync freezes
   the org's connection, `directory.frozen`, and every later sign-in misses), then the WorkOS
   organisation. The SSC org row and its audit chain stay; there is no org-delete command. A
   cell built for it is retired as `docs/runbooks/ssc-110-onboarding.md` describes.
3. Delete the Okta tenant (Okta support or the account's settings).
4. Delete the DNS records added in steps 2–4:

   ```sh
   gcloud dns record-sets delete signin.delimitus.com. --type=CNAME --zone=delimitus --project=ssc-platform-0
   gcloud dns record-sets delete <TXT name>. --type=TXT --zone=delimitus --project=ssc-platform-0
   ```

   and each email-domain and WorkOS verification record the same way (merge back any SPF change).

### Google Workspace or Microsoft Entra

The same flow works with a Google Workspace SAML app or a Microsoft Entra enterprise app as the
WorkOS connection: the SSC side (WorkOS organisation, `create-org`, invite codes, the console
card) is identical, since nothing in the code is Okta-specific. Only the identity provider's own
branding differs: Google Workspace and Entra show their own sign-in page, branded in their
admin consoles (Entra company branding; Google has no custom domain for its page). Which
providers are offered and proven is decided by `landing/CLAIMS.md` (`idp`), not by this runbook.

## Pilot social sign-in

Pilot orgs only (decision 002 amendment 2026-10-27; `landing/CLAIMS.md` row `social`). SSC uses
WorkOS `/sso/authorize` with `provider=` and `/sso/token` only, never User Management or AuthKit.

### WorkOS setup **[real]**

1. In the WorkOS dashboard, Authentication, enable each provider the pilot needs: Google OAuth,
   Microsoft OAuth, GitHub OAuth. For production, give each its own OAuth client (Google Cloud
   console, Microsoft Entra app registration, GitHub OAuth app) with the redirect URI WorkOS
   shows for that provider; WorkOS's shared test credentials are for staging only. Register the
   Microsoft app for "Personal Microsoft accounts only" (WorkOS requires it, and SSC trusts the
   address only from personal accounts). GitHub sign-in is refused until SSC checks GitHub's
   verified addresses (decision 002 amendment), so leave `github` out of `--providers` for now.
2. Keep `<auth_url>/callback` (`http://localhost:8100/callback` on the rig) as a WorkOS redirect
   URI, as in Before you start.

### Create a social org **[real]**

A small team pilots without a WorkOS organisation, a directory or an SSO connection. With the
database reachable as in `docs/runbooks/ssc-064-control-plane.md` step 10 (`SSC_DATABASE_DSN` as
`ssc_app`; no WorkOS settings needed):

```sh
LABEL=l$(openssl rand -hex 6)
ORG=$(uv run python -m ssc_control.identity create-social-org --name "<org name>" \
  --admin-email <admin email> --providers google,microsoft --cell-label $LABEL --operator op_<you>)
echo $ORG
```

Pass: the org id on stdout. One transaction makes the org in social mode, its first admin as an
invited person and its cell label, audited `org.created`, `org.updated` (the mode),
`user.invited` and `org.updated` (the label). A bad provider, label or address, or a label another org
has, writes nothing and says why. Leave `github` out of `--providers`, as above. The org has no
onboarding row and directory sync skips it.

Then build the org's cell, before anyone deploys an app, the way `create-org` expects: the label
names the cell, and the build takes the org id the command printed.

```sh
uv run python -m ssc_control.cells request --label $LABEL --stage prod --org-id $ORG --operator op_<you>
uv run python -m ssc_infra.onboard $LABEL --stage prod --settings settings.json --set org_id=$ORG \
  --probe-image <probe image>
```

and run the `cells ready` line it prints last (`docs/runbooks/ssc-110-onboarding.md`, step 3).
Signing in and inviting people need no cell.

The admin signs in first, at `https://console.delimitus.com/login` with the org id and one of the
org's providers: the sign-in links them, makes them active and takes their name from the profile.
Until then People shows them as Invited, and `restore-admin` refuses them: in a social org it
restores only a person who has signed in once.

### Change a social org's providers **[real]**

`create-social-org` is the only way into social mode: a directory org is never moved to it, and
`set-sign-in --mode social` on one refuses and writes nothing. For an org already in social mode:

```sh
uv run python -m ssc_control.identity set-sign-in --org <org_…> --mode social \
  --providers google,microsoft --operator op_<you>
```

Pass: `<org_…> social` on stdout. `--mode directory` with no `--providers` takes the org out of
social mode, for good.

### Invite people **[real]**

As an org admin, open People in the console, Invite by email, and invite each pilot person. They
show under Invited until their first sign-in. A person signs in with one provider: the first one
links, and another provider with the same address is refused, so each provider's check below
needs its own invited address.

### Live check **[real]**

Not yet run. Tick each line and record the date and the `usr_` ids under Record.

- [ ] The sign-in page for the org lists exactly its enabled providers and nothing that names the org.
- [ ] An invited person signs in with Google; People shows them active.
- [ ] An invited person signs in with Microsoft.
- [ ] With `github` enabled for a moment, an invited person's GitHub sign-in is refused with the
      same page and `login.failed` reason `email_not_verified`; then set the providers back.
- [ ] An address nobody invited is refused with the same page as every other refusal, and the
      audit chain has `login.failed` with the reason.
- [ ] `ssc login` from the command line goes through the same chooser.
- [ ] Deactivate a signed-in person from People: their next console request and `ssc whoami` fail,
      and the command line's refresh fails.
- [ ] Reactivate them: they sign in again with the same `usr_` id.
- [ ] `create-social-org` on the staging or pilot cell; its admin signs in with Google and People
      shows them as an active admin.
- [ ] That admin invites one person, who signs in with Google.

## Record

Copy the times, the `usr_` ids and the pass or fail lines into the SSC-019 status in the tickets file. Stop both servers with Ctrl-C; `.ssc-dev/` keeps the state for a rerun.
