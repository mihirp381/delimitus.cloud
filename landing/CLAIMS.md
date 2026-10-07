# Claims on delimitus.com

Every factual claim on `index.html`, with the ticket behind it and its status, or its public source.
Rows the agent-first copy (2026-10-07) leans on were re-checked against the tickets that day;
the others still read as of 2026-10-01. An element that makes a claim carries `data-claim="<id> ..."`, and
`packages/ssc_landing/tests/test_page.py` fails when the ids on the page and the ids in the table
below differ.

No feature ticket is Done for a customer yet, so the page words every feature as part of the
private pilot offer ("All of this is part of the private pilot offer.") and never as live today.
Update a row's status when its ticket closes; reword the page if a ticket is cut.

| Id | Claim on the page | Ticket or source | Status |
|---|---|---|---|
| `pilot` | Private pilot, not generally available | Founder, 2026-10-01 (SSC-065) | True today |
| `price` | Pricing is agreed with each pilot; no price shown | Founder, 2026-10-01; tickets section 9 | True today |
| `login` | Company login on every request to every app | SSC-018, SSC-019 | Code on main, not deployed (SSC-064) |
| `share` | Share with a person, a directory group or the whole company; nobody else gets in | SSC-021 | 2026-10-07: code on `round-2`; gateway rollout on cell 1 after T9 |
| `deploy` | Deploy from a folder (`ssc deploy`) or a GitHub push; Lovable and Replit apps come in through GitHub, each push to preview | SSC-014, SSC-015, SSC-016, SSC-022, SSC-023, SSC-047 | 2026-10-07: SSC-015 live on cell 1 (19 fixtures); SSC-047 code on `round-2`, not deployed |
| `agent` | A coding agent deploys from the editor, to preview, and can ask for a share or a data connection; the hero's chat (create, request Snowflake access, ask to share, deploy), its first step and the agent column | SSC-048, SSC-093 | 2026-10-07: control-plane half done (deploy to preview, request share, request connection); agent login, `get_logs` and `set_secret` on `round-2`, not deployed |
| `langs` | Python and Node apps | SSC-003 (20 apps run), SSC-015 | SSC-003 done; SSC-015 not started |
| `tools` | Claude Code, Codex and Cursor deploy directly; Lovable and Replit through GitHub | SSC-003 corpus: Cursor, Lovable, Replit; SSC-048 for the direct path, SSC-047 for GitHub | Not yet recorded for Claude Code and Codex, and no agent-driven deploy recorded for any of the three; runs in backlog SSC-099. Published by the founder's choice (2026-10-06) |
| `preview` | Preview, then production, each at its own address; a person promotes | SSC-042, decision 004 | 2026-10-07: code on `round-2` |
| `cloud` | Runs on Google Cloud | Decision 001 (SSC-001) | Decided |
| `account` | In the US, each company in a cloud account of its own | Decisions 001 and 021, assumption A2 (SSC-013) | Done for the cell layout; region us-central1 |
| `idp` | Sign in with Google Workspace or Okta | Decision 002 (SSC-002), SSC-019 live check on Okta | Proven; Entra is not named |
| `names` | Product names belong to their owners; no affiliation | Legal line, SSC-065 | True today |
| `db` | A database for each app | SSC-005, SSC-040 | SSC-005 done; SSC-040 not started |
| `timers` | Timers for scheduled jobs | SSC-041 | Partly done, control-plane half |
| `files` | File storage | SSC-046 | Not started |
| `approvals` | Approvals before production | SSC-045, SSC-049 | SSC-045 done (operator-recorded); SSC-049 not started |
| `data` | Read-only access to company databases; never writes; IT approves each connection | SSC-050, SSC-051, SSC-052 | 2026-10-07: code on `round-2` (Postgres only), not deployed; T6 used a stand-in |
| `egress` | Internet access only to the hosts IT allowed | SSC-027, SSC-053 | 2026-10-07: SSC-053 code on `round-2`, not deployed |
| `secrets` | Secrets kept out of the code, never shown again to people or agents; the agent never needs the value | SSC-026, SSC-048 (`set_secret` takes no value) | 2026-10-07: code on `round-2`, live checks on cell 1 under way |
| `owner` | When the owner moves on, IT hands the app to someone else | SSC-025 (owner transfer) | Partly done, control-plane half |
| `logs` | Logs, including why something was held | SSC-024 | 2026-10-07: code on `round-2`; live on cell 1, follow fix 00ea845 not yet deployed |
| `rollback` | Roll back to an earlier release with one command | SSC-016, SSC-022, SSC-043 | Partly done, control-plane half |
| `inventory` | A list of every app and its owner | SSC-025, SSC-057 | Partly done, control-plane half |
| `kill` | One switch that turns an app off; no time is printed | SSC-025, SSC-054 | Partly done; no time until SSC-054 publishes one |
| `audit` | An audit log of every change | SSC-012 | Partly done, control-plane half |
| `agents` | Coding agents held to the same rules as people; an agent can ask but never approve, and another admin approves its share and connection requests | SSC-045 (agent approval refused), SSC-048 (`request_share`, `request_connection`) | 2026-10-07: SSC-045 done; SSC-048 partly done |
| `isolation` | An app can't reach other apps | SSC-017 (internal ingress), SSC-027, SSC-029 | SSC-017 done; SSC-027 and SSC-029 not started |
| `leaks` | Every build is checked for leaked keys before it goes live | SSC-015 | 2026-10-07: live on cell 1 |
| `snowflake` | Snowflake named as a company data source in the hero's chat (the agent requests access) and in the example conversation: read-only, waiting for IT's approval | SSC-078 (Snowflake connection, SQL API v2) | 2026-10-07: not built; SSC-078 is in the backlog, built when a pilot needs it. Only Postgres is built (SSC-051, `round-2`). Kept on the page by the founder's choice (2026-10-07) |
| `https` | Every app address is HTTPS | SSC-064 (the cell's wildcard certificate) | 2026-10-07: live on cell 1 (wildcard certificate active 2026-10-04) |
| `minutes` | The hero: a secure, shareable app "in minutes, not weeks" and "ready for your team in minutes" | SSC-003 corpus (`spikes/corpus20/RESULTS.md`): Cloud Run made a ready revision in 8 to 49 s, median about 17 s; "not weeks" is the cost model's own estimate of 80 hours to set up the do-it-yourself path (`econ-labour`) | 2026-10-07: build plus deploy end to end not yet timed on a cell; the 19 SSC-015 fixtures on cell 1 recorded outcomes, not durations. Wording from the founder's hero design (2026-10-07) |
| `no-public` | No public apps; every request needs a company login | Tickets section 8, SSC-085 out of MVP | Decided |
| `no-domains` | No custom domains | Assumption A1, tickets section 8 | Decided |
| `no-webhooks` | No inbound webhooks | Tickets question 7, SSC-085 | Decided |
| `retention` | Form details kept 12 months, used for nothing else, deletion on request | SSC-065: bucket lifecycle rule (`infra/ssc_infra/landing.py`) | `privacy@delimitus.com` receives mail (forwarding set up by the founder, 2026-10-07) |
| `econ-cloud` | Do-it-yourself cloud bill, $129.53 a month plus $9.86 an app; the app line assumes one instance kept warm (minimum instances 1) so the first visit is not slow, a choice the company makes, not ours | See below | Prices fetched again 2026-10-07 from the Cloud Billing Catalog API: every unit price unchanged, so every figure stands |
| `econ-labour` | IT rate $68 an hour; hours are our estimate | See below | Hours are labelled "our estimate" |

## Economics sources

Google Cloud list prices, us-central1, USD, 730 hours a month, no free tier, no discounts.

| Line | Monthly | Source | Fetched |
|---|---|---|---|
| External HTTPS load balancer, one forwarding rule, $0.025 an hour | $18.25 | cloud.google.com/vpc/network-pricing#lb | 2026-10-07 |
| Cloud SQL Postgres, smallest high-availability tier with point-in-time recovery | $102.02 | `spikes/bakeoff/COST_SHEET.md` (Cloud SQL price list) | 2026-10-07 |
| Static outbound IP for Cloud NAT | $4.67 | `spikes/bakeoff/COST_SHEET.md` (Cloud NAT price list) | 2026-10-07 |
| Cloud Logging, 5 GB | $2.50 | `spikes/bakeoff/COST_SHEET.md` | 2026-10-07 |
| Secret Manager, 20 secrets | $1.23 | `spikes/bakeoff/COST_SHEET.md` | 2026-10-07 |
| Cloud Build, 60 minutes | $0.36 | `spikes/bakeoff/COST_SHEET.md` | 2026-10-07 |
| Artifact Registry, 5 GB | $0.50 | `spikes/bakeoff/COST_SHEET.md` | 2026-10-07 |
| **Fixed** | **$129.53** | | |
| Each app: Cloud Run, 1 vCPU and 512 MiB, one instance kept warm (minimum instances 1) at the idle rate: $6.57 for CPU and $3.29 for memory over 730 hours | $9.86 | cloud.google.com/run/pricing; `spikes/bakeoff/RESULTS.md` | 2026-10-07 |

Fetched again 2026-10-07 from the Cloud Billing Catalog API (`cloudbilling.googleapis.com/v1/services/<id>/skus`); each unit price matched the earlier figure: forwarding rule minimum $0.025/h (DEE3-C42E-3E4D); Cloud SQL for PostgreSQL regional vCPU $0.0826/h, RAM $0.014/GiB-h, standard storage $0.34/GiB-month (1912-86A6-9950, 9BE2-CB5B-66F8, F5EC-5814-93C3); Cloud NAT gateway uptime $0.0014/h and IP $0.005/h (32E2-4EFC-EF9F, 8515-9425-D2CE); log storage $0.50/GiB (143F-A1B0-E0BE); secret versions $0.06 a month and $0.03 per 10,000 accesses (7756-ADEF-84F4, EBA7-264F-2D2C); Cloud Build e2-standard-2 $0.006/min (A464-9020-6404); Artifact Registry $0.10/GiB-month (8502-299A-ABAF); Cloud Run minimum-instance CPU $0.0000025/vCPU-s and memory $0.0000025/GiB-s (7EBB-8579-2C98, 740D-08F2-7A11). Free tiers are left out, as above.

Not on the page, by the moat rule: the cost sheet's gateway and egress-proxy lines, our own cost
of a customer's cloud account, and our margin.

IT labour:

- $47.66 an hour: median pay of network and computer systems administrators, May 2025, BLS
  Occupational Outlook Handbook
  (bls.gov/ooh/computer-and-information-technology/network-and-computer-systems-administrators.htm).
- Times 1.43: wages and salaries are 70.0% of private-industry employer costs per hour worked
  (BLS Employer Costs for Employee Compensation, bls.gov/news.release/ecec.nr0.htm), and 1 / 0.70 = 1.43. $47.66 × 1.43 = $68.15,
  shown as $68.
- Hours, our estimate: 80 to set up the account, network, login, build pipeline, logs and a
  security review; 12 to put each app live; 2 a month per app for updates, access changes and
  fixes. The founder signs off these defaults before publishing.

The calculator's formula and its test vectors are in `calculator_vectors.json`.

## Before publishing

1. Record one Claude Code and one Codex app through the SSC-003 harness (`tools`). Moved to backlog SSC-099; the founder chose to publish first (2026-10-06).
2. Fetch the 2026-09-29 prices again and update the dates on the page and above (`econ-cloud`). Done 2026-10-07: no price changed.
3. Make `privacy@delimitus.com` receive mail (`retention`). Done 2026-10-07 (founder).
4. Founder sign-off on the copy and the calculator's default inputs. Given 2026-10-06 ("publish the page").
5. Apply the hosting from `main`, after `round-2` is merged: `landing: true` makes the account, bucket and alert; `landing_image` (a digest in the platform registry) puts the page on the control entry (decision 028). Done 2026-10-07: applied 03:20 UTC with `ssc-landing@sha256:efa08dcb…`, certificate active 03:31 UTC, founder's live check passed (page, pilot request, alert email).
