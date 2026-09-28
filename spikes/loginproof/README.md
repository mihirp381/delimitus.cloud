# SSC-002 spike: login vendor proof (WorkOS)

Standalone uv project. Nothing here is product code. `NOTES.md` holds what the docs say; `RESULTS.md` holds
what was measured (all rows `not measured` until the checklist below is run).

## What the harness does

- `serve`: a local callback server. Each `/login?provider=X` link sends you through WorkOS SSO and records
  the returned profile (`idp_id`, email, connection type, raw attribute names) with a timestamp.
- `directory`: snapshots each connected directory (users, emails, `idp_id`, state, group memberships).
- `join`: matches every recorded login to a directory user and says which key joined them, and whether the key
  survived the email change.
- `device`: logs in from the terminal with the device flow, keeps the refresh token in memory, waits for you
  to deactivate the user, then checks whether the token still works.
- `report`: writes `RESULTS.md`.

Secrets are read from environment variables only. Export them in the shell. Never write them to a `.env`
file and never source one.

## Founder checklist (about two hours, one afternoon)

Fixed costs: Google Workspace Business Starter trial needs a card and a spare domain you own (about $7 per
user per month after 14 days; cancel after the test). Microsoft Entra ID free tenant: $0. Okta Developer
Edition: $0. WorkOS staging environment: $0.

1. WorkOS: sign up at workos.com, stay in the **Staging** environment. In *API Keys* create a key and note
   the **Client ID**. In *Redirects* add `http://127.0.0.1:8765/callback`. In *Authentication* enable the
   device authorization flow if it shows as a toggle.
2. Google Workspace: start a Business Starter trial on a spare domain. Add five users and three groups
   (`Finance`, `Engineering`, `Admins`). Put one user in two groups. Pick one user you will rename later.
3. Microsoft Entra ID: create a free tenant at entra.microsoft.com. Same five users, three groups, same
   two-group user, same rename candidate.
4. Okta: create a Developer Edition org at developer.okta.com. Same users and groups.
5. WorkOS connections. Create one Organization per provider (three organizations). In each, follow the WorkOS
   setup link for: Google SAML (or Google OAuth), Entra ID **OIDC**, Entra ID **SAML** (a second connection
   in the Entra organization), Okta SAML. Then add a Directory to each organization: Google Workspace,
   Entra ID SCIM (map `objectId` to `externalId` as the guide says), Okta SCIM (turn on Create, Update,
   Deactivate). Wait for the first sync to finish (Google: up to 30 minutes).
6. Copy the ids into the shell:

```bash
export WORKOS_API_KEY=...            # sk_test_... from step 1
export WORKOS_CLIENT_ID=...          # client_... from step 1
export WORKOS_CONN_GOOGLE=conn_...
export WORKOS_CONN_ENTRA_OIDC=conn_...
export WORKOS_CONN_ENTRA_SAML=conn_...
export WORKOS_CONN_OKTA=conn_...
export WORKOS_DIR_GOOGLE=directory_...
export WORKOS_DIR_ENTRA=directory_...
export WORKOS_DIR_OKTA=directory_...
```

7. Run, in this directory:

```bash
uv sync
uv run python -m loginproof serve            # leave running in this terminal
```

   In a second terminal with the same exports:

```bash
uv run python -m loginproof directory        # snapshot all three directories
```

   Open `http://127.0.0.1:8765/` and click each of the four login links. Log in as the rename candidate
   each time. Each callback page says `recorded login`.

8. Change the rename candidate's primary email in Google, Entra and Okta. Wait for the sync (or press
   *Sync now* in the WorkOS dashboard). Run `directory` again, then log in again through all four links.
9. CLI login and revocation:

```bash
uv run python -m loginproof device
```

   Follow the printed link, log in as the rename candidate. The command then waits. Deactivate that user in
   Okta (or the provider you are testing), wait for the directory to show `inactive` in the WorkOS
   dashboard, press Enter. Repeat per provider if you want a per-provider answer. If you need to re-check
   later from a separate run, the command also accepts `--recheck` with the refresh token in
   `LOGINPROOF_REFRESH_TOKEN` for that run only.
10. Verdict:

```bash
uv run python -m loginproof join
uv run python -m loginproof report           # writes RESULTS.md
```

11. Send `RESULTS.md` and `out/records.json` is yours to keep locally; it is gitignored and holds emails and
    ids, no tokens.

## Tests

```bash
uv run pytest -q     # 7 tests: join rules on doc-shaped fixtures, report rendering
```
