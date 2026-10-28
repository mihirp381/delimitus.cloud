# SSC-110 onboard a customer

One customer is one row of `ssc.onboarding` (ticket 19D, decision 035), made by `python -m ssc_control.onboarding start` and moved by the worker's `onboarding:tick` every five minutes. It has two tracks:

- **The cell** (`cell=`): `awaiting_cell` until the cell `start` requested is `ready` in `ssc.cell`, then `cell_ready`. You build the cell yourself, under the just-in-time grant: this is the one human step, kept by founder decision (2026-10-24).
- **The identity** (`identity=`): `requested` until the WorkOS organisation and its two Admin Portal links exist, `awaiting_idp` while the customer connects their identity provider and directory, then `org_created` once the org exists with its first admin.

When both are done the row is `live`: the org's audit log gets `org.onboarded` and a welcome mail is queued to the admin. A row is never deleted; `abandon` keeps it.

## Before you start

Use the shell of the control-plane runbook, step 10 (`docs/runbooks/ssc-064-control-plane.md`): the proxy running and, as `ssc_app`:

```sh
export SSC_DATABASE_DSN="postgresql://ssc_app:$APP_PW@127.0.0.1:5433/ssc"
read -rs SSC_WORKOS_API_KEY; export SSC_WORKOS_API_KEY
read -r SSC_WORKOS_CLIENT_ID; export SSC_WORKOS_CLIENT_ID
```

`start`, `links` and `tick` call WorkOS and need both WorkOS settings; `status`, `list`, `confirm` and `abandon` need only the database. Every command takes the row's `onb_` id or its cell label for `--id`.

## Onboard

1. Start. The customer name names the WorkOS organisation; the domain is the customer's email domain; the admin email is the person who becomes the org's first admin.

   ```sh
   uv run python -m ssc_control.onboarding start --customer-name "<company>" --domain <domain> \
     --admin-email <admin email> --stage prod --operator op_<your name>
   ```

   In one transaction it requests the cell (`building`, for the new org id) and writes the row; then it makes the WorkOS organisation, or reuses the one organisation that has verified the domain if no org or other onboarding uses it, and the two links. It prints the row (id, label, stage, org id, the two tracks), the SSO and directory links, and the command that builds the cell. `--label` and `--org-id` choose them instead of the generated ones.

   If WorkOS fails, the row prints `reason=WORKOS_ERROR` and the error, and the next tick tries again. An organisation made before the failure is kept on the row and never made twice.

2. Send the two links to the customer's IT admin. They expire five minutes after they are made (WorkOS Admin Portal), so make new ones just before you send them, or on a call:

   ```sh
   uv run python -m ssc_control.onboarding links --id <label>
   ```

   No mail can go out before the org exists (the outbox names an org and a user), so the links go by hand.

3. Build the cell, under the just-in-time grant (`cells-jit`), with the command `start` printed. `settings.json` holds `gateway_image`, `billing_account` and `operator` (`infra/README.md`, Onboarding in one command); the org id is already on the command:

   ```sh
   uv run python -m ssc_infra.onboard <label> --stage prod --settings settings.json --set org_id=<org> --probe-image <probe image>
   ```

   After a full run it prints the `cells ready` line last. Run it as printed. A build that does not finish is recorded with `cells fail --label <label> --reason "<one line>"`, and `ready` still works after a rebuild.

4. Watch the row:

   ```sh
   uv run python -m ssc_control.onboarding status --id <label>
   uv run python -m ssc_control.onboarding list
   ```

   `status` prints both tracks, the reason and what to do in one line, and the links while the identity track waits. With `identity=awaiting_idp` and no reason, the customer has not yet finished both setups: one active SSO connection and one linked directory. `tick` runs one tick now instead of waiting for the worker:

   ```sh
   uv run python -m ssc_control.onboarding tick
   ```

5. Live: `status` prints `live cell=cell_ready identity=org_created`. The org was made exactly as `identity create-org` makes it (the founder checked in WorkOS, the directory connected, the cell label set), with the admin from the directory and every accepted SSO connection, and the welcome is queued. Binding GitHub (`python -m ssc_control.github bind`) is a separate step.

## Waiting reasons

| Reason | What it means | What to do |
|---|---|---|
| `JOIN_RULE_CONFIRM` | The directory is Google Workspace, whose logins join by email (decision 002). | Check that the domain is the customer's and that Google Workspace is offered to them (decision 002), then `uv run python -m ssc_control.onboarding confirm --id <label> --join-rule email --operator op_<your name>`. The next tick creates the org. |
| `IDP_UNSUPPORTED` | Every active connection is consumer sign-in (any OAuth type or magic link), the directory is neither Okta nor Google Workspace, or more than one directory is linked. `error=` says which. | Have the customer fix it in WorkOS through the Admin Portal; the next tick reads it again. Or abandon. |
| `ADMIN_NOT_IN_DIRECTORY` | The admin email names no active member of the linked directory, names two, or the founder check refused. `error=` says which, without the address. | Have the customer add or assign the admin in their identity provider. The next tick reads it again; a Google directory reaches WorkOS within about 30 minutes. If the address was wrong, abandon and start again. |
| `WORKOS_ERROR` | A WorkOS call failed; `error=` has the HTTP status and path. | Nothing: the next tick retries. If it lasts, check the WorkOS key and WorkOS's status. |

## Abandon

```sh
uv run python -m ssc_control.onboarding abandon --id <label> --reason "<one line>" --operator op_<your name>
```

It retires the cell and marks the row `abandoned`, keeping it, then prints the command that destroys the cell and the WorkOS organisation it leaves: deleting that is your call, in the WorkOS dashboard.

```sh
uv run python -m ssc_infra.onboard <label> --stage prod --destroy
```

It is refused once the row is `live`, and while the org exists (`identity=org_created`): `cells retire` refuses a cell an org names, and an org is not removed by onboarding. The reason goes to the log only; it names no people and no emails.

## Switch-over notes

- **Mail unset.** With no mailer (`SSC_SMTP_HOST` unset) the welcome stays queued. Tell the admin by hand: the console link, the org id and `uvx --from ssc-cli ssc login`. The welcome offers "Reply to this mail for help" only when `SSC_MAIL_FROM` is a mailbox someone reads; production's `noreply@delimitus.com` gets no help line.
- **Worker without WorkOS.** The tick uses the worker's directory-sync client (`SSC_WORKOS_API_KEY`, `SSC_WORKOS_CLIENT_ID`). Without them it logs `onboarding tick skipped` and does nothing; run `tick` by hand. `SSC_ONBOARDING_TICK_CRON` changes its schedule (default `*/5 * * * *`).
- **Social orgs.** A pilot org made by `identity create-social-org` (runbook ssc-019, Pilot social sign-in) has no onboarding row: no WorkOS organisation, no directory and no welcome mail. Tell its admin the console link and the org id by hand.
- **Orgs made before.** Orgs made by `identity create-org` with `cells request` (the earlier step 10) keep working and have no onboarding row. Those commands stay for repairs; a new customer starts here.
