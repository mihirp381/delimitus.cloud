# Customer domains plan (2026-10-26, founder decisions recorded)

Status 2026-10-28 (branch `domains/custom-domains-2026-10-26`). "Done" means built and covered by
local tests; nothing here has run against a live domain. The live proof is CD-7.2.

| Ticket | Done | Owed live |
|---|---|---|
| CD-0 | Q1 to Q6 answered 2026-10-26 | - |
| CD-1.1, CD-1.2 | `apps_suffix`, retired suffixes, hosts by suffix, `suffix` and `previous_suffixes` in the snapshot | A new suffix admitted by a staging gateway within one poll, no restart |
| CD-2a | `aud` is the environment id; helpers, vectors, data gateway, decision 010 | A flip on staging with every running revision answering 200 and no redeploy |
| CD-2b | Not built (Q1) | - |
| CD-3.1 to CD-3.3 | `ssc.org_domain`, agent zone, records, certificate and map entry, agent IAM, probe, retire guard | Zone, records and certificate `ACTIVE` on staging; no 403 on the first create; resolvers see our NS |
| CD-4.1 to CD-4.3 | Minute tick, states, confirmed flip, 307 from a previous suffix | Timed transitions; the request loop during a flip; flip back and re-flip without a loop |
| CD-5.1 to CD-5.3 | `Domain=` cookies dropped, `form-action` and `frame-ancestors`, admission with the PSL | A browser on staging: the cookie absent, a foreign form post blocked |
| CD-6.1 to CD-6.3 | Console page, `ssc domains`, mails and the stall notice; each refusal its own code | Both mails and the stall notice delivered from staging |
| CD-7.1 | Decision 004 amended, 005, 023, 024 cross-referenced; contracts match the code; `docs/trust/domain-limits.md` | - |
| CD-7.2 | Runbook `docs/runbooks/ssc-111-customer-domain-proof.md`; nightly `ssc_conformance.suffix_walk` (proof `suffixes`) | The proof itself; seven green nights; then CLAIMS and the landing FAQ |
| CD-8 | Deferred (Q4) | - |

Founder answers 2026-10-26: Q1 environment id; Q2 one forced re-login at flip accepted; Q3 zone kept until
the parent stops delegating, 12-month cap, name never reused; Q4 no aliases in V1 (CD-8 on first paid
request); Q5 landing claim changes only after CD-7.2 passes; Q6 dedicated-domain option documented as the
recommended choice for regulated customers. CD-2b is not built.

Source: architecture discussion 2026-10-26, independent review of the proposal (no chat context), and a read
of Pando (trypando.ai) for comparison. Repo facts checked at main 05ce5ef. Zero customers, internal test
apps only: breaking changes to the host rule, snapshot and env contract are allowed.

Goal: an app is reached at `salesdashboard.tools.testorg.com` instead of
`salesdashboard.<label>.delimitusapps.com`, with one customer action per org, zero per app, and every
failure mode owned and visible on our side.

## 0. Constraints that stay

- One org per cell, one external LB per cell on `entry_ip` (decisions 025, 030). The customer zone resolves to
  that address and nothing else.
- Sign-in stays on `auth.delimitus.com`; Okta and WorkOS configuration never change (decision 024).
- Reserved platform hosts (`ssc--agent`, `ssc--secrets`, issuer) stay on `delimitusapps.com`.
- Host-only `__Host-ssc-session`, Fetch-Metadata and WebSocket Origin rules unchanged (decision 023).
- Snapshot stays frozen and content-addressed; new members are optional like `egress`.
- CLAIMS `no-domains` stays "Decided" until the live proof in CD-7 passes. Nothing on the landing page
  changes before that.
- 2026-10-28: `landing/CLAIMS.md` has no `no-domains` row; the page names no customer domain and shows
  only `*.<label>.delimitusapps.com` addresses. CD-7.1 adds a dated line under the table instead, and a
  claim row is added only after CD-7.2 (Q5).
- One sub-agent at a time, minimal comments in new code, `Co-Authored-By` and `Claude-Session` trailers,
  Pulumi from `ssc-ops`, never push `main`.
- Not before the open GA gates (GA-11, GA-12, GA-13.2) close, unless the founder reorders.

## 1. Decisions the founder must make first (CD-0)

| # | Question | Recommendation | Why it matters |
|---|---|---|---|
| Q1 | Identity note `aud`: keep the exact origin, or switch to the environment id (Pando's model)? | Environment id | Removes the re-apply job, the previous-origin verifier and the config roll at every suffix flip. The gateway still binds host to environment per request, so no security is lost. Changes `docs/contracts/identity-note.md`, `ssc_app`, the Node helper, conformance vectors and the data gateway check. |
| Q2 | Minutes of disruption at trial-to-paid flip acceptable? | Yes for V1 | With Q1 = environment id the blip is one forced re-login, not a redeploy. With Q1 = origin, CD-2b is required. |
| Q3 | Who owns the delegated zone after churn and for how long? | We keep it until a probe shows the parent no longer delegates, max 12 months, then delete | Closes dangling-delegation takeover; must be in the contract. |
| Q4 | Offer aliases (`salesdashboard.testorg.com`) at all in V1? | No, build CD-8 on first paid request | Per-name CT exposure and dependence on the customer's parent CAA. |
| Q5 | Landing claim after live proof: "your subdomain, one delegation" or stay quiet? | Say it, with the CT and same-site notes in the trust pack | GTM decides. |
| Q6 | Dedicated-domain option (customer buys `testorg-tools.com`) documented as the recommended choice for regulated customers? | Yes, documented only, same mechanics | Same-site risk (CD-5) is strongest argument for it. |

Success: answered above 2026-10-26; decision 004 amendment drafted (CD-7.1) with the answers.

- Q2 note, 2026-10-28: a timer call is not retried by `timers/runner.py`, and schedule tokens still
  use the origin as `aud`. So the HTTPS dispatcher (`timers/https.py`) sends a call once more when
  the gateway answers a previous suffix's `307` naming the same host label and path under another
  valid suffix, with a token minted for that origin. It follows no other redirect and never a
  second one, so a flip costs a timer call one extra round trip, not a failed run.

## 2. Tickets

### CD-1 Suffix model in code (~4 days)

**CD-1.1 Org suffix and parser.**
- `ssc.org` gains `apps_suffix` (default `<label>.delimitusapps.com`), `previous_suffixes` as
  `[{suffix, until}]`, and `delegation` state (`none|pending_dns|issuing|active|failed|retired`) with the
  stored NS set and zone name.
- `ssc_shared/hosts.py`: `parse_app_host(host, suffixes)` returns `(slug, env, suffix)`; no `cell_label`
  in the result. Reserved slugs, `--preview`, length proof (first label ≤ 49, total ≤ 253) recomputed
  against the longest allowed suffix. `app_origin(slug, env, suffix)`.
- Every caller compares suffix identity, not label: `ssc_edge/gate.py` (`_admit`, `_decide`, `_own_origin`),
  `identity/rules.py` `parse_return_to`, `timers/https.py:124`, `api/routes/v1/apps.py:93-123`.
- Success: `pytest packages/ssc_shared packages/ssc_edge packages/ssc_control` green; a host under a
  previous suffix parses as `previous`; a host under no suffix of the org is a 404 at the gate exactly as
  today; `ssc status` prints the current suffix URL.
- 2026-10-28: delegation state, zone name and NS set live in `ssc.org_domain` (one non-retired row
  per org), not on `ssc.org`.

**CD-1.2 Suffix in the access snapshot.**
- `docs/contracts/access-snapshot.md`: optional members `suffix` (string) and `previous_suffixes`
  (`[{suffix, until}]`). `hosts` stays keyed by first label. Compiler reads the org row in the same
  transaction that freezes the snapshot.
- Gateway, data gateway (`ssc_datagw/server.py` audience build) and timer dispatcher read the suffix from
  the snapshot; `SSC_APPS_DOMAIN` becomes the bootstrap default only, used until the first snapshot lands.
- Success: a snapshot with a new suffix is admitted by the gateway within one poll with no restart;
  contract conformance vectors updated; `snapshot_feed` tests cover both members absent.

### CD-2 Identity audience (Q1) (~3 days)

**CD-2a (Q1 = environment id).**
- `aud` becomes the environment id. `ssc_app` Python helper and Node helper accept `SSC_APP_ENV_ID`;
  `SSC_APP_ORIGIN` still set for links but no longer used for verification. Data gateway checks environment
  id. `docs/contracts/identity-note.md` and vectors updated. `agentpack/content.py` text updated.
- Success: adversarial matrix cases that replay a note across apps still fail; a suffix flip on a staging
  org leaves every running revision answering 200 with no redeploy; `ssc_app` conformance green; the
  reconciler rolls each app once for the new `SSC_APP_ORIGIN`; verification never depends on it.

**CD-2b (not built; Q1 answered environment id. Kept for the record.)**
- Verifier accepts `SSC_APP_ORIGIN` and `SSC_APP_ORIGIN_PREVIOUS`; flip job re-applies every active
  environment with both set before the suffix is published, and drops the previous origin on the next
  deploy. Preview URL re-posted on GitHub (`github/push.py`).
- Success: same flip on staging, every revision answers 200 before and after publish; no window where a
  note is rejected, proven by a request loop during the flip.

### CD-3 Delegated zone infrastructure (~5 days)

**CD-3.1 Zone as an org-owned, long-lived resource.**
- New Pulumi component outside `cell.py`'s per-cell stack (own stack or `protect: true` with
  `retainOnDelete`): Cloud DNS public zone `<suffix>` in the cell project, DNSSEC off in V1.
- Records written by us: `*.<suffix>` A → `entry_ip`; `_acme-challenge.<suffix>` CNAME → Certificate
  Manager DNS authorization target; `<suffix>` CAA `0 issue "pki.goog"`; `<suffix>` MX `0 .` (null),
  TXT `v=spf1 -all`, `_dmarc.<suffix>` TXT `v=DMARC1; p=reject`.
- NS set read back once and stored on the org.
- Success: `pulumi preview` shows zero changes to the zone on a cell rebuild; stored NS set equals the zone's
  `nameServers`; `dig +short NS` from 1.1.1.1 and 8.8.8.8 matches after delegation.
- 2026-10-28: zone created by the cell agent; the cell stack never manages it, so a cell rebuild
  cannot touch it.
- 2026-10-28: delegation state, zone name and NS set live in `ssc.org_domain` (one non-retired row
  per org), not on `ssc.org`.

**CD-3.2 Wildcard certificate and map entry at runtime.**
- Cell agent creates `DnsAuthorization`, managed `Certificate` for `*.<suffix>` and `<suffix>`, and a
  `CertificateMapEntry` on the existing map, behind a role limited to `certificatemanager.*` on the cell and
  `dns.changes.create` on that one zone (same pattern as `_certificate()` in `cell.py`).
- Success: certificate reaches `ACTIVE` on staging with the `_acme-challenge` CNAME in our zone only;
  `authorizationAttemptInfo` and `provisioningIssue` surfaced in the org's delegation state; audit log
  shows every change.

**CD-3.3 Zone lifecycle and takeover guard.**
- Offboarding: zone kept while a daily probe sees the parent delegating to our NS set; deleted after Q3's
  limit; never recreated under the same name for another org.
- Success: test that `retire` refuses while the probe still sees the delegation; runbook entry in
  `docs/README.md` ops section.

### CD-4 Flip job and redirects (~4 days)

**CD-4.1 State machine and probes.**
- `pending_dns`: every 30 s for 72 h compare the parent's NS answer (1.1.1.1, 8.8.8.8, 9.9.9.9) to the
  stored set; `issuing`: poll certificate state; `active` on `ACTIVE`; `failed` with a reason after 72 h
  or a `provisioningIssue`. Operator page at 120 min in `issuing`.
- Success: states advance on staging without manual steps; each transition is an audit event; a wrong NS
  set stays `pending_dns` with the observed records shown.
- 2026-10-28: the tick (`domains:tick`) runs every minute, not every 30 s: a minute is enough for a
  wait counted in hours, and the 72 h and 120 min timers are unchanged. A certificate `provisioning`
  with a detail (an authorization still in progress reads the same) stays `issuing` with the detail
  shown as its failure text; only a `FAILED` certificate fails the row. `stalled_at` (revision 0047)
  makes the 120 min `domain_stalled` log line fire once per row across workers and restarts.

**CD-4.2 Atomic publish with confirm.**
- Admin confirms the flip in the console (list of apps that move, like Pando's `--confirm`). Job: write `apps_suffix`, push old suffix to `previous_suffixes` with
  `until = now + 30 d`, compile and publish one snapshot. Sessions on the old host are not migrated.
- Success: one snapshot version carries the change; a request loop across all staging apps sees at most
  one 302-to-login per app and no 5xx; `ssc status` and the console show the new URLs.
- 2026-10-28: the flip runs in the request (`POST /v1/domain/flip`), not a job: one transaction
  locks the org row, swaps the current and previous suffix and marks the snapshot dirty, so one
  compile carries it. It refuses `DOMAIN_NOT_READY` while any live environment would still change
  under the org's suffix list (`reconciler.rolling`), which holds the coming suffix from the moment
  the domain is `active` (the pre-roll), so apps roll once before the flip and once after. The
  default suffix stays a previous one for 30 days after a flip back, as the customer suffix does
  after a flip.

**CD-4.3 Redirect from previous suffix.**
- Gate: a host under a `previous_suffixes` entry answers `307` to the same slug, env, path and query under
  the current suffix, `Cache-Control: no-store`, no cookie, no session. After `until`, 404 as for unknown
  hosts. Flip-back is the same job in reverse and leaves a 307 the other way.
- Success: deep links with query strings land correctly; a flip-back on staging does not loop any
  browser; preview hosts redirect too.

### CD-5 Gateway hardening on customer domains (~3 days)

**CD-5.1 Cookie domain strip.**
- Envoy or `ssc_edge` response filter removes any app `Set-Cookie` carrying a `Domain=` attribute and
  counts it in logs.
- Success: adversarial case added to the matrix; app that sets `Domain=testorg.com` sees the cookie absent
  in the browser on staging.

**CD-5.2 Outbound form and navigation policy.**
- Gateway adds `Content-Security-Policy: form-action 'self'` (and `frame-ancestors 'self'`) to app
  responses unless the app's snapshot lists allowed destinations (new optional member, same shape as
  `egress`). Document that same-site CSRF against the customer's other `*.testorg.com` systems is the
  reason.
- Success: form post from a staging app to another host is blocked by the browser; allowed destination
  works; `docs/trust/app-access-limits.md` gains the same-site section.

**CD-5.3 Suffix admission rules.**
- Refuse suffix with fewer than three labels, on the Public Suffix List (vendored list, refreshed by the
  7-day rule), or equal to, above or below any other org's suffix, current or previous.
- Success: unit tests for each refusal; DB unique constraint plus the ancestor check in one transaction.
- Amended 2026-10-28 (CD-5.3): the vendored list (ICANN section only) is refreshed by
  `tools/psl_refresh.py`, and `test_psl.py` fails once its `// VERSION:` date is more than 90 days
  old, not 7: the list changes slowly. Names under `delimitus.com` and `delimitusapps.com` are
  refused too, and the overlap check holds advisory lock `(31, 0)` until the request commits.

### CD-6 Surfaces (~4 days)

**CD-6.1 Console onboarding card "Your own address".**
- Settings → Environment: input for the subdomain, the four NS records with copy buttons, the three states
  in plain words ("Waiting for your DNS change", "Issuing certificate", "Active"), the observed parent NS
  answer while waiting, the confirm screen for the flip, and a retire action for operators only.
- Plain-sentence risk note: cookies and forms on the customer's domain, recommend a dedicated sub-label
  or a dedicated domain (Q6).
- Success: Playwright e2e for each state against a mocked API; design tokens only; `gen:api` diff clean.

**CD-6.2 CLI and agent pack.**
- `ssc domains show|add <suffix>|status|retire`. No MCP tool: an agent must never change where apps live.
  Agent pack text says addresses come from the org suffix and are read from `get_status`.
- Success: CLI tests; `tools.py` list unchanged; agent-limits doc updated.

**CD-6.3 Notifications.**
- Mail through the SMTP relay (decision 033) to org admins on `active` and `failed`; operator page on the
  120-minute stall.
- Success: both mails rendered in tests; stall page fires in staging with a deliberately wrong NS set.

### CD-7 Proof and paperwork (~3 days)

**CD-7.1 Decision and contract amendments.**
- Decision 004 amended: per-org suffix, default `<label>.delimitusapps.com`, delegated zone, reserved hosts
  stay, CT note. Decisions 023 and 024 cross-referenced. `identity-note.md`, `access-snapshot.md`,
  `data-gateway.md` updated per CD-1/CD-2. Trust pack: CT logs on the customer zone, same-site note,
  mail lockdown records, zone retention (Q3).
- Success: `docs/decisions/README.md` row 004 cites this plan; contracts match code.

**CD-7.2 Live proof.**
- A test domain we own, delegated to a staging org, from NS change to first app on the customer suffix,
  timed; flip-back and re-flip once; `GA-14` nightly extended to walk every org's suffix and previous
  suffixes with HTTPS and certificate expiry checks.
- Success: proof document with timestamps in `docs/`; nightly green for seven days; only then CLAIMS
  `no-domains` row flips (text per Q5) and the landing FAQ changes.

### CD-8 Aliases (deferred, Q4) (~5 days)

- `environment_alias` table (`host`, `env_id`, cert state); IT adds one CNAME `salesdashboard.testorg.com`
  → `salesdashboard.<suffix>`; cell agent creates a load-balancer-authorized certificate and map entry;
  gate answers the alias with `307` to the canonical host, no cookie, no session; alias admitted only when
  the CNAME resolves to the canonical host; console card states CAA and CT costs; apex names refused.
- Success: alias on the staging test domain redirects with path and query; removed alias returns 404
  within one snapshot poll; certificate deletion on removal.

## 3. Cut from V1

DNSSEC and DS records; customer choice of sub-label after delegation; dual-origin serving (CD-2b); white-labelled login host; Cloudflare for SaaS; per-app CNAME as the primary model; customer DNS
provider API tokens (Pando's approach).

## 4. Order and estimate

CD-0 → CD-1 → CD-2 → CD-3 → CD-4 → CD-5 → CD-6 → CD-7, then CD-8 on request. About 23 developer days
end to end with one sub-agent at a time, plus the seven-day nightly soak in CD-7.2. CD-5.1 and CD-5.2 can
land before CD-3 since they are safe on the default suffix too.
