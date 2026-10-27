# Claims on delimitus.com

Every factual claim on `index.html`, with its truth state and the date it was last checked.
"Proven on our test cell" means proven on a cell Delimitus runs for testing, not with a customer.
The evidence behind each row (tickets and proof-run records) is internal and not in this
repository. An element that makes a claim carries `data-claim="<id> ..."`, and
`packages/ssc_landing/tests/test_claims.py` fails when the ids on the page and the ids in the
table below differ, when a paragraph, list item or heading in a `data-claims` section has no id,
or when a phrase in `forbidden_claims.txt` is on the page.

No feature is live for a customer yet, so the page words every feature as part of the private
pilot offer ("All of this is part of the private pilot offer.") and never as live today. Update
a row when its truth state changes; reword the page if a feature is cut.

| Id | Claim on the page | Truth state | Checked |
|---|---|---|---|
| `pilot` | Private pilot, not generally available | True | 2026-10-17 |
| `price` | Pricing is agreed with each pilot; no price shown | True | 2026-10-17 |
| `login` | Company login on every request to every app | Proven on our test cell, not yet with a customer | 2026-10-17 |
| `share` | Share with a person, a directory group or the whole company; nobody else gets in | Proven on our test cell, not yet with a customer | 2026-10-17 |
| `deploy` | Deploy from a folder (`ssc deploy`) or a GitHub push; Lovable and Replit apps come in through GitHub, each push to preview | Proven on our test cell, not yet with a customer, for `ssc deploy`, a GitHub push and a Lovable export through `ssc deploy`; no Replit export and no Lovable-synced repository through GitHub yet | 2026-10-17 |
| `agent` | A coding agent deploys from the editor, to preview, and can ask for a share or a data connection; the hero's chat (create, request Snowflake access, ask to share, deploy), its first step, the agent column, and the example's share of a data-connected production app, applied only after an org admin approves both the share request and the audience widening | Proven on our test cell, not yet with a customer. Data connections: on preview the connection's preview access rule may link it at once when the agent asks; prod waits for an approval. Known gap: `request_share` takes a group id only. F21 fixed in code on 2026-10-20 (`request_share` opens every needed approval; the last one applies the share), not yet re-run live | 2026-10-20 |
| `langs` | Python and Node apps | Proven on our test cell, not yet with a customer, for both | 2026-10-17 |
| `tools` | Claude Code and Codex deploy directly today; Cursor through the same MCP server; Lovable and Replit through a GitHub push. Also the works section, the step-1 chips (Claude Code, Codex) and the FAQ | Claude Code and Codex proven on our test cell: each built and deployed an app to preview. Cursor (`ssc init` writes its config), Lovable and Replit (through GitHub): not proven end to end | 2026-10-20 |
| `preview` | Preview, then production, each at its own address; a person promotes | Proven on our test cell, not yet with a customer | 2026-10-17 |
| `cloud` | Runs on Google Cloud | True by design | 2026-10-17 |
| `account` | In the US, each company in a cloud account of its own | True for the cell layout: each company runs in a cell of its own in us-central1; another cell's app is refused by the network and by IAM | 2026-10-17 |
| `idp` | Sign in with Okta | Okta only, proven on our test cell. Google Workspace is not offered in V1 and Microsoft Entra is untested. An Okta Suspend does not revoke access; Deactivate or unassign does | 2026-10-17 |
| `db` | A database for each app | Proven on our test cell, not yet with a customer: a password rotation kept the app healthy, a production deploy has a recovery point, and the eleventh database refused `DB_TIER_FULL` with the bigger-tier hint | 2026-10-17 |
| `timers` | Timers for scheduled jobs | Proven on our test cell, not yet with a customer: runs on time, no overlap, paused by the kill switch. Known gap: a run longer than 300 s is cut on an app without sessions | 2026-10-17 |
| `files` | File storage | Proven on our test cell, not yet with a customer: put, get by a 10-minute link, link refused after 600 s, environments kept apart, new links suspended by the kill switch. No console files screen yet | 2026-10-17 |
| `approvals` | Approvals before production | Proven on our test cell, not yet with a customer, in the console and the CLI. Approval mail is not offered in V1. An org admin may decide their own request, an agent session never decides, and a connection owner who is not an admin cannot decide their own; this rule (2026-10-26) is covered by the control-plane tests only (`test_an_admin_decides_their_own_request_an_owner_not`, `test_a_one_admin_org_approves_what_its_agent_asked_for`, `test_the_owner_may_not_decide_their_own_exceed_request_but_an_admin_may`, `test_check_decider_order`), not yet run on our test cell | 2026-10-26 |
| `data` | Read-only access to company databases; never writes; an admin of your company approves each connection | Proven on our test cell, not yet with a customer, for all ten connection kinds: read served, write refused, an ungranted app refused. On preview the connection's preview access rule may link it with no approval; prod always waits for an admin's approval | 2026-10-22 |
| `egress` | Internet access only to the hosts IT allowed; the fleet example: reaches only the internet hosts your admin allowed (every host on the org-wide allowlist, not only its own) | Proven on our test cell, not yet with a customer: a removed host's open connection cut within 8 s and new ones refused. The allowlist is organisation-wide, not per app | 2026-10-17 |
| `secrets` | Secrets kept out of the code, never shown again to people or agents; the agent never needs the value | Proven on our test cell, not yet with a customer. The console lists names and versions only | 2026-10-17 |
| `owner` | When the owner moves on, IT hands the app to someone else | Proven on our test cell, not yet with a customer, through the API. The console's transfer is not yet proven in a browser | 2026-10-17 |
| `logs` | Logs, including why something was held | Proven on our test cell, not yet with a customer: every line within 30 s from the CLI; secret-shaped strings masked for agents. The console's logs view is not yet checked live | 2026-10-17 |
| `rollback` | Roll back to an earlier release with one command | Proven on our test cell, not yet with a customer | 2026-10-17 |
| `inventory` | A list of every app and its owner | Proven on our test cell, not yet with a customer, in the CLI and the console | 2026-10-17 |
| `kill` | One switch that turns an app off; no time is printed | Proven on our test cell, not yet with a customer: drills of 4.35 to 8.03 s. The console's kill page is not yet checked live. No time on the page | 2026-10-17 |
| `audit` | An audit log of every change | Proven on our test cell, not yet with a customer: the export verifies against the day's anchor. Known gaps: a promotion has no audit row of its own, and the warm setting is recorded as `org.updated` | 2026-10-17 |
| `agents` | Coding agents held to the same rules as people; an agent can ask but never approve, and an org admin approves its share and connection requests | Proven on our test cell, not yet with a customer. On preview a connection's preview access rule may link a connection request at once; prod waits for an admin's approval | 2026-10-22 |
| `isolation` | An app can't reach other apps | Proven between our two test cells: another cell's app and gateway refused by the network, its secret and bucket by IAM; the browser isolation suite passed | 2026-10-17 |
| `leaks` | Every build is checked for leaked keys before it goes live | Proven on our test cell, not yet with a customer. Known gap: a key under a plain name is uploaded before the build refuses it, and the refused bundle stays until clean-up after 7 days | 2026-10-17 |
| `snowflake` | Snowflake named as a company data source in the hero's chat (the agent requests access) and in the example conversation: read-only, waiting for IT's approval | Proven on our test cell, not yet with a customer | 2026-10-17 |
| `https` | Every app address is HTTPS | Proven on our test cell, not yet with a customer | 2026-10-17 |
| `minutes` | The hero: a secure, shareable app "in minutes, not weeks"; step 3: measured, one prompt to a running preview in 3½ minutes with Claude Code and 8 with Codex | Measured on our test cell, to preview, not production; a later run took 7 min 3 s | 2026-10-17 |
| `no-public` | No public apps; every request needs a company login | Decided: apps answer a request with no login with a redirect to sign-in | 2026-10-17 |
| `no-domains` | No custom domains | Decided | 2026-10-17 |
| `no-webhooks` | No inbound webhooks | Decided | 2026-10-17 |
| `retention` | Form details kept 12 months, used only to answer the request, deletion on request | True: a bucket lifecycle rule (`infra/ssc_infra/landing.py`); `privacy@delimitus.com` receives mail | 2026-10-07 |
| `econ-cloud` | Do-it-yourself cloud bill, $129.53 a month plus $9.86 an app; the app line assumes one instance kept warm (minimum instances 1) so the first visit is not slow, a choice the company makes, not ours | Google Cloud list prices, below; unchanged when fetched again | 2026-10-07 |
| `econ-labour` | IT rate $68 an hour; hours are our estimate | BLS figures, below; the hours are labelled "our estimate" | 2026-10-17 |
| `compare` | The comparison table in the cost section: what each way of putting an internal app in front of a team leaves to you; the Delimitus column (login on every request, a cloud account of the company's own, IT-approved read-only data, inventory, logs and kill switch, maintained inside the pilot) | As the rows the Delimitus column reuses (`login`, `account`, `data`, `approvals`, `inventory`, `logs`, `kill`, `pilot`); the table says that column is part of the private pilot offer | 2026-10-17 |
| `src-redaccess` | 2,000 of 5,000 corporate-purpose apps held sensitive data with no authentication, in a scan of more than 380,000 web assets | Public source, below | 2026-10-17 |
| `src-marmelab` | One developer built a self-hosted platform for agent-built internal apps in about two weeks, about 44,000 lines, and estimates one or two more person-months to production grade; MIT, github.com/marmelab/platform. Company SSO for the platform, optional for each app; one database per app; admin dashboard with owner, logs, backups, audit log and a stop switch; reading existing company data is not covered | Public source, below | 2026-10-17 |
| `src-retool` | Retool, State of AI Governance 2026: 307 senior technology and security leaders (43% CISO, 35% CTO, 22% CIO), surveyed May 2026 with Wynter; 93% at least somewhat concerned about vibe-coded internal tools running in production; 4% have governance that covers AI-generated code however it was written; 5% very confident they have full visibility into what runs in production; 55% want security and access controls in a platform underneath the app, 7% inside each app by the builder; quote "It works. It ships. Nobody logs it anywhere." (an enterprise CISO) | Public source, below | 2026-10-17 |

## Public sources

Third-party figures and quotes on the page. Each URL is written as text on the page, never as a
link (nothing on the page points to another origin).

| Source | What the page uses | Published | URL | Fetched |
|---|---|---|---|---|
| Retool, State of AI Governance 2026 (with Wynter, May 2026, 307 leaders) | `src-retool`: 93%, 4%, 5%, 55% against 7%; "It works. It ships. Nobody logs it anywhere." | 2026-06-17 | retool.com/blog/ai-governance-report-2026 | 2026-10-16 |
| TechTarget, "Vibe coding security crisis CIOs can't ignore" (Sean Michael Kerner), reporting Red Access research | `src-redaccess`: 2,000 of 5,000 corporate apps with sensitive data and no authentication, from 380,000+ web assets; Will Bengtson's "Build the paved path." | 2026-06-02 | techtarget.com/searchcio/feature/vibe-coding-security-crisis-CIOs-cant-ignore | 2026-10-16 |
| François Zaninotto, "Building Your Own AI Platform for Vibe-Coded Apps", Marmelab blog | `src-marmelab`: two weeks, about 44,000 lines, one or two more person-months; features of the self-hosted column | 2026-10-15 | marmelab.com/blog/2026/10/15/building-your-own-ai-platform.html | 2026-10-16 |

## Economics sources

Google Cloud list prices, us-central1, USD, 730 hours a month, no free tier, no discounts.

| Line | Monthly | Source | Fetched |
|---|---|---|---|
| External HTTPS load balancer, one forwarding rule, $0.025 an hour | $18.25 | cloud.google.com/vpc/network-pricing#lb | 2026-10-07 |
| Cloud SQL Postgres, smallest high-availability tier with point-in-time recovery | $102.02 | Cloud Billing Catalog API (Cloud SQL) | 2026-10-07 |
| Static outbound IP for Cloud NAT | $4.67 | Cloud Billing Catalog API (Cloud NAT) | 2026-10-07 |
| Cloud Logging, 5 GB | $2.50 | Cloud Billing Catalog API | 2026-10-07 |
| Secret Manager, 20 secrets | $1.23 | Cloud Billing Catalog API | 2026-10-07 |
| Cloud Build, 60 minutes | $0.36 | Cloud Billing Catalog API | 2026-10-07 |
| Artifact Registry, 5 GB | $0.50 | Cloud Billing Catalog API | 2026-10-07 |
| **Fixed** | **$129.53** | | |
| Each app: Cloud Run, 1 vCPU and 512 MiB, one instance kept warm (minimum instances 1) at the idle rate: $6.57 for CPU and $3.29 for memory over 730 hours | $9.86 | cloud.google.com/run/pricing | 2026-10-07 |

Fetched again 2026-10-07 from the Cloud Billing Catalog API (`cloudbilling.googleapis.com/v1/services/<id>/skus`); each unit price matched the earlier figure: forwarding rule minimum $0.025/h (DEE3-C42E-3E4D); Cloud SQL for PostgreSQL regional vCPU $0.0826/h, RAM $0.014/GiB-h, standard storage $0.34/GiB-month (1912-86A6-9950, 9BE2-CB5B-66F8, F5EC-5814-93C3); Cloud NAT gateway uptime $0.0014/h and IP $0.005/h (32E2-4EFC-EF9F, 8515-9425-D2CE); log storage $0.50/GiB (143F-A1B0-E0BE); secret versions $0.06 a month and $0.03 per 10,000 accesses (7756-ADEF-84F4, EBA7-264F-2D2C); Cloud Build e2-standard-2 $0.006/min (A464-9020-6404); Artifact Registry $0.10/GiB-month (8502-299A-ABAF); Cloud Run minimum-instance CPU $0.0000025/vCPU-s and memory $0.0000025/GiB-s (7EBB-8579-2C98, 740D-08F2-7A11). Free tiers are left out, as above.

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
