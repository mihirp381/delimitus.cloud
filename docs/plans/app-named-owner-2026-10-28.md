# Every app has a named owner (2026-10-28)

Founder decision 2026-10-28: every deployed app has a named owner. The builder is the owner, and
any active org admin is the fallback. If the owner and every admin have left, the app does not
open. It shows a page saying the app needs a named owner, and that the person should ask their
admin. The admin then picks a new owner in the console from the org's list of people. This
covers both preview and production.

## What exists today

- `ssc.app.owner_user_id` is NOT NULL. It is set to the creator at `POST /v1/apps`, and the
  trigger `owner_must_be_active` refuses an owner who is not active when it is set.
- `PUT /v1/apps/{app_id}/owner` (in `api/routes/v1/inventory.py`) lets an active admin transfer
  the app to another active member. It is audited as `app.owner_transferred` and grants the new
  owner preview access.
- The console has `TransferOwner` in `app-detail/AdminActions.tsx`, where the admin types an
  email address or a `usr_` id.
- When the owner leaves, the app keeps serving. Only timers pause, with `owner_deactivated`.

## The rule

- **Who owns an app.** The owner is `owner_user_id`. When that person is not active (deactivated
  by an admin, or removed by directory sync), the app is **ownerless**.
- **Fallback.** An ownerless app keeps opening while the org has at least one active admin, and
  those admins are its fallback. It is **blocked** when it is ownerless and the org has no active
  admin.
- **Who can be picked.** A new owner may be any active person in the org. Agent and CI
  credentials cannot change an owner.
- **Production and preview.** The rule covers every environment of the app. A disabled,
  quarantined or deleting app keeps its current behaviour, and that state wins over this one.
- **Data gateway and timers.** A blocked app is also refused by the data gateway, so it cannot
  read customer data. Timers already pause when the owner leaves, and stay as they are.

## Where it shows

1. **Gateway (blocked app).** Every request to a blocked app gets a new page from
   `ssc_edge/pages.py`. Its title is "This app needs a named owner". Its text is: "This app has
   no owner right now, so it is turned off. Ask your organisation's admin to assign an owner."
   - Status 403, `no-store`, and the same style and CSP as the other pages.
   - The deny reason is `owner_needed`. Admins see the same page, because they fix it in the
     console, not in the app.
2. **Snapshot.** The compiler marks a blocked app so that the gateway and the data gateway deny
   it. Follow the versioning rule in `docs/contracts/access-snapshot.md`.
   - It must fail closed: a reader that does not know the new value must deny, not serve.
   - If the rule only allows adding a value under a new contract version, say so and do it.
   - Mark the snapshot dirty whenever a change could flip an app into or out of blocked: a
     person's status changes, a person's role changes, or an owner is transferred. Check that the
     existing deactivate, reactivate, sync and role paths already do this.
3. **API.**
   - `GET /v1/apps/{id}`, the inventory and `GET /v1/me/apps` (admin view) gain
     `owner_state: "active" | "ownerless" | "blocked"`. Regenerate OpenAPI and `schema.d.ts`.
   - `PUT /v1/apps/{app_id}/owner` is unchanged, except that its description now says this is
     how an ownerless app is fixed.
4. **Console.**
   - **The inventory and the app page show a warning on an ownerless or blocked app:** "No owner.
     Assign one." The app page shows the warning with the Transfer button, which is relabelled
     "Assign owner" when the app has no owner.
   - **The dialog lets the admin pick from the org's active people.** Use `GET /v1/users`, with
     search and paging, showing name and email. Typing an email address or a `usr_` id still
     works for keyboard users.
   - Members who open "Your apps" see a blocked app marked "Needs an owner" and cannot open it.
5. **Notification.** When an app becomes ownerless, every active admin gets one notification
   through the existing notifications service: "<app> has no owner. Assign one." Follow the
   service's existing rules for de-duplicating. It is sent once per time the app becomes
   ownerless.
6. **Audit.** `app.owner_transferred` is unchanged. Becoming ownerless is visible through the
   person's own `user.updated` row, so add no new audit action unless the notification needs
   one.

## Success criteria

Each criterion is proven by a named test.

1. A new app's owner is its builder, and `owner_state` is `active`.
2. Deactivating the owner makes the app `ownerless`. It still opens while an admin is active,
   and every active admin gets the notification once.
3. When the owner and every admin are inactive, the app is `blocked`. This is reached through
   directory sync, since social orgs refuse deactivating the last admin.
   - The gateway answers 403 with the owner-needed page for both preview and production hosts.
   - The data gateway refuses the app.
   - A disabled app still shows the disabled behaviour.
4. An admin assigning an active person through `PUT /v1/apps/{id}/owner` makes the app `active`
   again, and the next snapshot serves it. An inactive or unknown person is refused.
5. Reactivating the old owner also makes the app `active` again.
6. Agent and CI credentials, and members, cannot assign an owner. The existing refusals still
   hold.
7. A snapshot reader that does not know the new marker denies the app (fail closed).
8. The console shows the warning and the "Assign owner" label. The picker lists active people,
   searches and assigns, and the typed email or `usr_` id still works. "Your apps" marks a
   blocked app (vitest). `gen:api` leaves no diff.
9. The full suites are green: ruff, pyright, lint-imports, pytest, console typecheck, vitest,
   build and the isolation e2e. CI is green.
10. The docs match:
    - a new decision, or an amendment to the app-ownership decision;
    - `docs/contracts/access-snapshot.md`;
    - `docs/api/README.md`;
    - `landing/CLAIMS.md`, which may not mark this as live;
    - the people and offboarding text in the runbook.

## Out of scope

- A builder naming a specific backup admin.
- A grace period before blocking.
- Transferring ownership automatically to an admin.
