# SSC admin console

The SSC admin console (SSC-057), decision 018. React 19, Vite 8, TanStack Router (file routes) and
TanStack Query, with an API client generated from `docs/api/openapi.json`. Node 22.22.2+, 24.15+
or 26+ (jsdom's range); CI uses Node 24.

| Path | What |
|---|---|
| `src/api/` | `schema.d.ts` (generated, committed), `client.ts` (bearer token, `Idempotency-Key` on writes, problems become `ApiProblem`), `grants.ts` (sharing changes with `If-Match` and the 412 retry), `directory.ts` (group and person lookup), `lifecycle.ts` (kill switch, enable, owner transfer, rollback, delete), `audit.ts` (filters and export), `approvals.ts` (reading request payloads), `cell.ts` (the cell, usage, health and database types and how their figures are worded), `warm.ts` (the warm option's cost and change), `connections.ts` (data connections: add, change, link to an environment, and the `exceed_ceiling` ask), `timers.ts` (pause, resume, run now, runs), `egress.ts` (allow and remove internet hosts), `github.ts` (the connected repository; NOT_FOUND reads as none), `promote.ts` (promote, then deploy the release the build made), `access.ts` (why a person can open an environment, with words for every reason), `logs.ts` (the newest lines, the follow request and the line cap), `inventory.ts` (the inventory's types, its filter and the sharing summary), `people.ts` (unmatched logins in the words of `ssc logins`, linking one, and the exact-match lookups of a person and a group). |
| `src/auditChain.ts` | The audit export's hash chain check, a port of the CLI's `ssc audit verify` (`packages/ssc_cli/src/ssc_cli/audit_chain.py`) on Web Crypto SHA-256: the same checks in the same order, the same report and the same words. It reads and writes JSON the way Python does (whole numbers of any size, `1.0` kept a decimal, keys sorted by code point), because the canonical bytes are Python's. It only reads the bytes it is given; the file the browser saves is the API's answer untouched. |
| `src/help.ts` | What the Help page is built on: the support address and the trust pack link a build was given, each taken only when well formed (one plain address; an `https` address), and the status check, where any answer from the API counts, a refusal included, and no answer or a 5xx does not. |
| `src/tokens/` | Colour, type, radius, spacing, shadow and layout tokens, matched to the landing page (`landing/index.html`): its accent, tone colours, font stacks and spectrum, each with a dark counterpart. Hex literals live only in `primitives.ts`; `tokens.css` is generated from them. Components and `styles.css` use only `var(--ssc-*)`. |
| `src/auth/` | `oauth.ts` (sign-in through the auth host as the OAuth client `ssc-console`, decision 029: S256 PKCE, state and `iss` checked), session (tokens in memory only, refreshed a minute before the access token expires; a pasted dev token may be copied to `sessionStorage`), the dev token login, and `isAdmin`, the one place that decides which admin-only screens to show (to callers whose `whoami` role is `admin`). |
| `src/routes/` | `/login`, `/auth/callback` (the auth host's answer), `/` (the apps: for an org admin the inventory from `/v1/inventory`, paged, with each app's owner and last use and, per environment, its release, last deploy, sharing and billing mode from one `/v1/usage` read; for anyone else the plain list from `/v1/apps`; filters by slug or owner, status and environment over the loaded rows), `/apps/$appId` (Overview, Logs, Access and Settings tabs: environments, who has access, share, promote, rollback and admin actions), `/apps/$appId/kill-switch/$runId` (one kill switch run: its mode, state, total and each step's times, read again while it runs; linked from the admin actions' result and from the audit log's kill switch rows), `/apps/$appId/delete/$runId` (one removal of an app, GA-12: each step with what it removed; it still answers once the app is deleted and its own page is gone), `/approvals` (the requests waiting for the caller, or every request filtered by state: Request, Requested by, Age and one Status in the words Waiting, Approved, Rejected and Withdrawn; the row opens the request) and `/approvals/$approvalId` (one request: what it asks for, and Approve or Reject with a reason for those who may decide; approving the last request of a sharing change applies it), `/connections` (the data connections the caller may see by name, kind, state and owner, with a filter once there are more than ten; org admins add one with its name, kind, owner and address, the rest under "More options", and a connection's address is sent once and never kept, cached or shown) and `/connections/$connectionId` (one connection: its overview, schemas, limits and ceiling, and status; org admins change each in place, suspend it by typing its name and resume it with one confirming click), `/egress` ("Internet access": the fixed outbound IP once, beside its copy button, the allowed hosts with who added each by name, and for org admins the catalogue to pick from, adding with the high-risk tick, and removing), `/people` (admins only: the logins that matched no person, with why, attempts and first and last seen, and linking one to an active person found by exact email or `usr_` id, as `ssc logins` does; a person looked up by exact email and a group by exact name, read-only, because the directory is managed in the identity provider), `/audit` (time, actor, action and target; a row opens the event with its ids and JSON to copy; search over the loaded events and the date range on show, the other filters under "More filters" with a count of those set; one Export menu with CSV, JSON Lines and "Verify a file…"; a JSON Lines export's hash chain is checked in the browser once it is saved, and "Verify a file…" checks one saved earlier, with no API call), `/environment` ("Usage", admins only, in three panels: Plan and usage (what is added a month, each app's use this month, and under Details the other figures and the parts created when first needed with what asked for each and what each adds), Capacity (the warm option, SSC-092: which production apps and whether the gateway stay at one instance, suggested where use and cold starts say so, the cost shown before saving and sent with the change, and the gateway offered in the same step), and Database (places, connections and the bigger-database step); "not a bill" is said once, in the header; the outbound IP is only on Internet access), `/help` ("Help", for everyone: the support address and the trust pack link the build was given, and whether SSC is answering, from one `GET /v1/whoami` on the console's own origin made when the page opens and again on "Check again", with the time it was checked). `src/routeTree.gen.ts` is generated by the router plugin and committed. |
| `src/app-detail/` | The app page's four tabs (`tabs.ts`: Overview, Logs, Access, Settings; an old `?tab=data`, `secrets` or `timers` address redirects to the tab and section it moved to). Overview has one card per environment (`EnvironmentCard.tsx`): state, address, who can open it by name, last deploy by name and time, a count of data connections linking to Access, one primary action (Share on production, Promote on preview), Roll back, and a Details disclosure, closed by default, with the ids, versions, use this month and database (`Running.tsx`). Access holds who has access with the one Share dialog, the data connections and why a person can open it (`Access.tsx`); Settings holds secrets, timers, the repository and the admin danger zone, in that order. The share dialog, rollback, admin-only actions; each environment's data connections (the card reads the list for its count, a connection's columns only once its row is opened) and timers, read only when their section is opened (`EnvConnections.tsx`, `Timers.tsx`); each environment's secrets, read-only and only when opened: names, latest and live versions, flagged when they differ, with the `ssc secret set` commands to copy and never a value (`Secrets.tsx`); the connected repository (`Repository.tsx`); one kill switch run, shared by the admin actions and the run's own page (`KillSwitchRun.tsx`); one removal of an app, the same way (`DeleteRun.tsx`); why one person can or cannot open an environment, with the grants that decided it (`AccessExplain.tsx`); an environment's logs, read once opened and then followed with a long poll while the section is open and the tab is on show, with log text shown as text only (`Logs.tsx`); and promote to production, which builds what preview runs and then offers to deploy the release it made (`Promote.tsx`). Builder-only actions are shown to everyone and the API's refusal is shown; admin-only ones are hidden from others. |
| `src/connections/` | The connections pages' parts: the add dialog (`AddConnection.tsx`), the detail panels (`Overview.tsx`, `Settings.tsx`, `Status.tsx`) and their in-place change form (`Editable.tsx`), the shared fields (`fields.tsx`), and `Picker.tsx`, groups and people picked by name with the directory lookup and kept as ids. |
| `src/components/` | Button, Badge (the status pill; an approval's state in the words Waiting, Approved, Rejected and Withdrawn), Table (a card with an empty state that says what to do next), Dialog, ConfirmAction (type the slug to confirm, or one click for what is not destructive), ProblemNotice, Phrases (short phrases that each stay on one line), Brand (the mark, the product name and the spectrum rule), PageHeader (title, status, one sentence of purpose of at most 140 characters, which `test/page-header.test.ts` checks, an "About this page" disclosure for the rest, the page's actions), EmptyState, Notice (the info, success, warning and danger boxes), Panel (a titled card with optional actions), AdminOnly (the admin pages' gate and refusal; `quiet` shows nothing to others), FilterBar (the main filters with the rest behind "More filters", closed by default, with a count of the hidden ones set), Menu (a button that opens a list of actions, by mouse or the arrow keys), Loading (skeleton rows, or one inline bar), Person (a name with the email as its title), and the `useClipboard` hook behind every copy button. Lists name people, groups and apps; an id shows only on a detail view, beside its copy button, and `test/names.test.tsx` fails any list cell that is a bare id. |
| `test/` | Vitest: tokens and contrast, the client, the screens against a fake API, sign-in (PKCE, the callback's checks, refresh scheduling, sign-out), the dev login's absence from production builds, and the console's words (`wording.test.ts`: never "Denied"). `test/fixtures/audit-chain.json` holds export files and what the CLI's own check says of each (an intact chain, changed rows, gaps, rows that are not rows); `test/audit-chain.test.ts` holds the browser's check to every one. Write it again with `uv run python console/test/fixtures/audit_chain_vectors.py` from the repo root when the CLI's check or the canonical form changes. |
| `e2e/` | Playwright tests against Postgres and the real API in Docker: sharing (`smoke.spec.ts`), approvals and their status words, the audit log's closed filters, Export menu and event detail, and the hash chain check of a real export (`admin.spec.ts`), the share dialog, the kill switch and deleting an app (`lifecycle.spec.ts`), adding a connection with the first step only, changing its owner and status and linking it to an environment, with the list's width at 1280 by 800 (`connections.spec.ts`), and the role matrix (`roles.spec.ts`: what an org admin, a builder and a plain user each see, what is hidden from them, that the API refuses what is hidden, and the outbound IP on Internet access and not on Usage). |

## Commands

```
npm ci
npm run dev          # Vite on :5173; /v1 is proxied to SSC_API_URL (default http://127.0.0.1:8000)
npm run typecheck
npm test
npm run build        # production build, without the dev login
npm run gen:api      # after docs/api/openapi.json changes; CI fails on a stale client
npm run gen:tokens   # after changing src/tokens/*.ts; CI fails on a stale tokens.css
npm run e2e          # needs Docker; see below
```

## Against a local API

Start the API and database from the repository root using the
[local control plane walkthrough](../CONTRIBUTING.md#local-control-plane). Then, from `console/`:

```sh
SSC_API_URL=http://127.0.0.1:8000 npm run dev
```

From the repository root, run `uv run python tools/dev_stack.py token` and paste the result
into the console's sign-in page.

The token sign-in form exists only in `npm run dev` and in builds made with
`VITE_SSC_DEV_LOGIN=1`; `test/devlogin-absent.test.ts` checks the build output.
`npm run build` ends with `scripts/check-dist.mjs`, which fails when `dist/` holds the dev login
or a local address, as a stray `.env`, `.env.local` or `.env.production` would cause; the
console image's build context leaves those files out.

## Production sign-in

"Continue with your work account" sends the person to the auth host's `/authorize` (decision 029)
and back to `/auth/callback`, which exchanges the code at `/token`, using the auth settings below.

In production the console is served at `https://console.delimitus.com` by `packages/ssc_console_host` (its Dockerfile runs `npm ci && npm run build` with the first four settings below as build arguments), and the control entry sends `/v1` on that host to the API; see "Console" in `infra/README.md`.

The auth host must know the console's origin as `SSC_CONSOLE_URL`: its client `ssc-console`
redirects only to `<SSC_CONSOLE_URL>/auth/callback`, and `/token` and `/revoke` answer CORS for that
origin alone. Tokens live in memory, so a reload asks the auth host again (its own session cookie
skips the email step). Sign-out calls `/revoke`.

While a signed-in page is on show the shell asks `/v1/whoami` every 4 seconds
(`WHOAMI_EVERY_MS` in `src/routes/_authed.tsx`). A 401 there, as for a person deactivated in the
directory, signs the console out and opens the login page, within 5 seconds.

## Settings

The build reads five settings, from the environment or a `.env` file; `npm run dev` and
`vite preview` read one more.

| Variable | Default | What |
|---|---|---|
| `VITE_SSC_AUTH_URL` | `https://auth.delimitus.com` | The auth host's origin; also the `iss` the callback must carry. |
| `VITE_SSC_API_AUDIENCE` | `https://api.delimitus.com` | The resource asked for, the API's user audience (`SSC_API_USER_AUDIENCE`). |
| `VITE_SSC_SUPPORT_EMAIL` | empty | The support address on the Help page, as a `mailto:` link. Empty, or anything but one plain address: the page says "Ask your SSC contact" and links nothing. |
| `VITE_SSC_TRUST_URL` | empty | The trust pack's `https` address, linked from the Help page. Empty, or not an `https` address: no link is shown. |
| `VITE_SSC_DEV_LOGIN` | unset | `1` compiles the token sign-in form into a production build (the end-to-end test does). Never set for a release; `npm run build` refuses the result. |
| `SSC_API_URL` | `http://127.0.0.1:8000` | Dev and preview only: where the proxy sends `/v1`. Not part of the build; the built console calls the API on its own origin. |

## End-to-end test

`npm run e2e` runs `e2e/run.mjs`: it starts `e2e/compose.yaml` (Postgres 18 and the API built
from `e2e/api.Dockerfile`) under a random project name and host port, mints a token with
`dev_stack.py token` and an operator token for the directory, builds the console with the dev
login, runs Playwright, and removes the containers. `roles.spec.ts` adds its own people to the
directory and mints a token for each with `dev_stack.py token --sub <user id>` inside the API's
container; `run.mjs` hands it the compose project and file to do so. Install the browser once
into `node_modules`:

```
PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install chromium
```

## Dependencies

Exact versions only. `.npmrc` sets `before=` to a date at least 7 days back, so npm resolves
nothing newer; move it forward when upgrading. CI also runs `tools/npm_lock_age_check.py` on
`package-lock.json`. Never commit `node_modules`.
