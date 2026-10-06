# SSC-086 results

The staging proof run for the architecture of 2026-10-03 (architecture document, section 8). Each row is filled from the final line of its command (`README.md`), and the full output is in `results/t<n>.json`. A failed row is not worked around in the stack: it goes back to the architecture document as a written change with its cost.

- **Cells:** cell 1 `c-proofcell01` (every flag on, kept as the probe cell), cell 2 `c-proofcell02` (every flag off, destroyed in T12).
- **Run:** 2026-10-05 to <pass 2 date>. Pass 1 written 2026-10-06 after T7; pass 2 adds T6, T8, T9, T10, T1 cost, step 8 and T12.
- **Commit:** branch `round-2`, c1567b9 at pass 1 (kit and stack fixes during the run are named in each row).
- **Login:** real login through `auth.delimitus.com`. T2 and T7 used a cookie from a browser session after the founder signed in; nothing was sealed by `seal_cookie.py`.

## Results

| # | Proof | Pass when | Command | Result |
| --- | --- | --- | --- | --- |
| T1 | Two cells from the amended stack, one full and one empty | `cell_diff` 0 differences outside what the flags name, no policy override; cell 2 under $1 a day | `t1 diff`, `t1 cost` | Drift **PASS** 2026-10-05: 0 differences over 1,604 resources, 0 policy overrides, after 11f62cc (Google-assigned ids, job runs, address users) and re-applying cell 2's agent image, timer keys and health-check logging. The first run showed 15. Cost: pass 2 (two whole billing days of cell 2). |
| T2 | Load balancer and wildcard certificate, serverless NEG to the gateway at min 0 | A browser reaches a probe app by its public host; certificate issued within 30 minutes of the DNS record | `t2` | **FAIL** on the certificate. Entry probe PASS (the gateway's `run.app` host answers 404), and the probe app answers 200 on its public host in 0.35 to 0.97 s warm (8.16 s on its first request) with a browser cookie. Cell 1's certificate was active 87.9 min after its record; cell 2's, in the same zone and written 9 s later, 21.7 min. Change: below. |
| T3 | The 14 runtime probes through the new path, gateway at min 0 | All pass; nightly schedule restored against cell 1; the app's `run.app` URL accepted as the ID-token audience | `t3 nightly`, `t3 public` | **PASS** 2026-10-05: nightly on cell 2 14/14 (snapshot drift 23 s), on cell 1 14/14 (22 s), public path 14/14, gateway minimum 0. Fixes a40f6c3, e97ef49 (the peer-cell probe waits 240 s); the schedule is back on against cell 1 (2874dab). |
| T4 | `cannot_reach_peer_cell` from cell 1 against cell 2 | Network refusal before any IAM refusal on the app; the gateway answers only through its load balancer | `t4` | **PASS** 2026-10-05: app and gateway each refused twice by the network and once by ingress (404 on the Google VIP); 4 TCP attempts to cell 2's addresses blocked. Name lookups of `run.app` hosts inside a cell take about 22 s to fail. |
| T5 | `db-f1-micro`, ten app databases, cross-connect, restore | Instance under 15 minutes; each database under 1 minute; cross-connect refused; restore written up | `t5 ops`, `t5 cross` | **PASS** 2026-10-05: instance 8.0 min; ten databases, slowest 0.5 s; cross-connect refused 9/9 (`42501 permission denied for database`). First cross run failed 0/9 on the certificate name, not on isolation (fixed d89ea6b). Finding: every app role can connect to the `postgres` maintenance database (Postgres's default CONNECT for PUBLIC); to be revoked in SSC-040. |
| T6 | NAT for the data gateway's position; Envoy CONNECT proxy on the `e2-micro` | Outbound calls leave from the reserved IP; unlisted hosts refused | `t6 nat`, `t6 proxy` || Pass 2. |
| T7 | Cold start, ten samples a series, 26-minute gap: static, API, Streamlit; gateway cold and warm | Medians reported; none worse than the bake-off (4.5 s, 8 s, 22 s) plus the gateway's start; Direct VPC egress delay measured | `t7 run`, `t7 report` | **FAIL** 2026-10-06 on static cold only. Gateway start median 8.56 s (10). Cold, gateway and app both asleep: static 15.40 s against 13.06, API 16.28 s against 16.56, Streamlit 20.47 s against 30.56. Warm gateway, app asleep: static 6.24 s, API 6.05 s, Streamlit 11.98 s, all under their limits. All 60 requests answered 200. Direct VPC egress delay median 0.02 s (20). Run 2026-10-05 20:56 to 2026-10-06 05:45 UTC. Change: below. |
| T8 | Kill drill against an open WebSocket | Under 10 s end to end | `t8` | **FAIL** 2026-10-06 06:05 UTC on `papi`, cell 1 cold: end to end 12.61 s. The front door refused after 2.55 s and the WebSocket was cut after 3.19 s; the overrun is the run's last steps (`gateway_deny` done 5.56 s, `scale_to_zero` done 12.49 s, `pause_timers` 12.61 s); every step `done`, first attempt. A second drill 48 s later, from the same operator login, passed at 9.45 s (`gateway_deny` 2.42 s, `scale_to_zero` 9.32 s). No query or tunnel leg (no data gateway, SSC-054). Undone with `ssc enable papi`. See written changes. |
| T9 | Streamlit open 24 hours, instance-billed and request-billed; the bill | Each within 20 % of $0.0684 and $0.0909 an hour; the 60-minute drop and the reload documented | `t9 hold`, `t9 report`, `t9 bill` || Pass 2. |
| T10 | Gateway on gen1 at 0.5 vCPU, same probes | Pass or fail recorded; decides the gateway's cost per session hour | `t10` || Pass 2. |
| T11 | Deny probe from a cell 1 app against cell 2's secret and bucket | Refused by IAM, not only by the network | `t11` | **PASS** 2026-10-05: the secret read refused (403 `secretmanager.versions.access`), the bucket listing refused (403 `storage.objects.list`). Operator half: the deny probe refused and all ten policy commands refused for the founder. |
| T12 | Delete cell 2 and watch the billing slot | Linked count back to 4 the same day; recorded here and in SSC-089 | `t12` || Pass 2. |

## Stand-ins and fallbacks

Nobody may read these rows as proof of the code they stand in for.

- **T6 is not evidence for SSC-050 or SSC-053.** The data gateway is a Cloud Run job (`standins/egress`) running as `ssc-data` with its tag, on the gateway subnet, with Direct VPC egress. The proxy is stock Envoy with a fixed two-host list, started on the `e2-micro` by a cloud-config override (`t6 envoy-config`).
- **T8 is not evidence for SSC-054.** It covers the open WebSocket, the front-door refusal and the instance stop, run with `ssc disable` because the console has no login. There is no query or tunnel leg.
- **T2, T3 and T7:** the real login was exercised (a browser cookie after the founder signed in through `auth.delimitus.com`); `seal_cookie.py` was not used.
- **T1:** `cell_diff` leaves out what differing flags name. The left-out list is printed with the result.
- **T9 request-billed leg and T10** run on documented overrides (README), not on stack settings.

## Written changes (pass 1)

A failed row goes back to the architecture as a change with its cost. The founder decides each one; SSC-006 records it (decision by decision, D1).

### T2: certificate issue time

- **Seen:** 87.9 min (cell 1) and 21.7 min (cell 2) from the DNS authorisation record to an active certificate, against 30. The two were written 9 s apart in one zone, so the zone's name-server switch alone does not explain cell 1. To read before SSC-006 amends decision 001: cell 1's certificate and DNS-authorisation history.
- **What it affects:** onboarding only. Once active, a cell's certificate renews on its own, and nothing a customer does waits on it.
- **Options:**
  1. **Change the pass line, not the stack (recommended).** The certificate is reported beside onboarding, not inside it (SSC-091, D6), and a cell is not handed over until it is active. The onboarding command polls it for up to 120 minutes. Cost: $0.
  2. Start the certificate first in a new cell's apply, so it runs alongside the 78-minute DNS sinkhole. Cost: $0, a reorder in `cell.py` for new cells only. Worth doing with option 1 until SSC-091 shortens the sinkhole.
  3. A certificate shared across cells: refused by rule 1.
- **Third figure:** the next new cell (SSC-091's measurement).
- **Chosen 2026-10-06 (founder):** options 1 and 2. The certificate is reported beside onboarding, a cell is handed over only once it is active (up to 120 minutes), and a new cell's apply starts the certificate first (SSC-091).

### T7: cold start through the gateway

- **Seen:** a cold first load is the gateway's start followed by the app's start, one after the other.
  - The gateway takes 8.56 s (median of the `www` first byte, 10 samples).
  - The app's own start, behind a running gateway, is about 6 s for static and API and 12 s for Streamlit.
  - So no cold sample came in under 14.3 s.
  - Static is over its limit for two reasons. The gateway adds 8.6 s, and the static app itself starts in 6.2 s against the bake-off's 4.5 s.
- **The tail matters more to a user than the median:**

  | Series | App | Median | 9th of 10 | Slowest |
  |---|---|---|---|---|
  | Cold | Static | 15.4 s | 22.6 s | 25.2 s |
  | Cold | API | 16.3 s | 20.7 s | 24.3 s |
  | Cold | Streamlit | 20.5 s | 32.2 s | 52.5 s |

  - SSC-092 tells buyers "5 to 20 seconds". The medians hold to that, roughly. The slow tenth does not.
  - One warm API sample (0.4 s) found the app still running; it does not move the median.
- **What it affects:** the first person to open an app after about 15 minutes of no traffic to the cell. Once an app is running, it is not affected.
- **Where the time goes (cell 1's logs, read 2026-10-06 after the run):**
  - **Gateway, from "Starting new instance" to the first request answered (8 starts):**

    | Part | Time |
    |---|---|
    | Instance boot, image and Python imports, until uvicorn's first line | 5.4 to 7.5 s |
    | Authz startup: metadata token, KMS decrypt, snapshot | 0.8 s |
    | Envoy and the port-8080 probe passing | 0.2 to 2.5 s |

    - The gateway has no startup CPU boost (`cell.py`, `_service`).
    - Its image does not compile Python bytecode (`UV_COMPILE_BYTECODE` is unset), so each new instance compiles what it imports. On a laptop the gateway's imports take 0.4 s with bytecode and 1.4 to 1.6 s without; on 1 vCPU the difference will be larger.
  - **Static app, behind a running gateway (6.2 s):**
    - It printed its first output 1.5 s after its instance started.
    - Its HTTP startup probe passed on the second attempt, 4.4 s later. The agent sets the apps' startup probe to every 5 s (`STARTUP_PERIOD_SECONDS = 5`, `ssc_agent/cloud_run.py`), so a ready app can wait up to 5 s to be marked ready.
    - The apps already have startup CPU boost.
  - **The gateway's ID tokens for apps:** a few milliseconds each. They are not a factor.
- **Options, cost a month a cell:**
  1. **App startup probe every 1 s instead of 5**, still two minutes in all (failure threshold 120). One constant in the agent. It should take the static app from about 6.2 s to about 2 to 3 s, and every other app by up to 4 s. Cost: $0.
  2. **Gateway image compiles bytecode and the gateway gets startup CPU boost.** One line in the Dockerfile and one in `cell.py`'s service resources. Cost: the boosted CPU during each start, about $0.0005 a start, under $1 a month a cell at pilot use (estimate).
  3. **Gateway start overlap in code.** Start Envoy while the authz process starts, and open port 8080 only once both answer. It saves up to about 1.5 s. Cost: $0, about 1 day.
  4. **Gateway warm in business hours.** Minimum 1 from 08:00 to 19:00 on weekdays in the customer's time zone, by a scheduled change. About $3.57 a month plus a scheduler job. It takes an empty cell from $23 to about $26.57, over A7's $25.
  5. **Gateway always warm.** About $9.86 a month ($6.57 idle CPU and $3.29 memory). It takes an empty cell from $23 to $33, over A7. This is already the paid warm option (SSC-092); it should not be the default.
  6. **Change the pass line.** Use "cold median within the 20 s told to buyers" instead of "bake-off plus the gateway's start". Cost: $0, but it fixes nothing a user sees.
- **Recommended:**
  - Do 1 and 2: three lines, under $1 a month.
  - Measure again with one cold series on cell 1: 10 samples, about 4.5 hours, almost no cost. The arithmetic says static cold would come to about 11 s against its 13.06 s limit, and the limit itself falls as the gateway's start falls. That is an estimate until measured.
  - If it passes, decision 001 records the probe period, the bytecode and the boost.
  - 3 only if the gateway still takes over 6 s.
  - 4 and 6 only if the new figures still fail.
  - 5 stays the customer's paid choice.
- **Chosen 2026-10-06 (founder):** options 1 and 2, then one cold series on cell 1. The result goes in pass 2.

### T8: kill drill

- **Seen.** The two drills differ by about 3 s, all of it in `gateway_deny`: 5.56 s against 2.42 s since the command. `scale_to_zero` took 6.0 s and 6.2 s. Refusal (2.55 s, 2.64 s) and stream cut (3.19 s, 3.15 s) are the same in both.
- **Where the time goes.**
  - `gateway_deny` is done once the cell's `latest.json` names the new snapshot version. When the first check finds the old version, the job re-defers itself for one second later (`kill_switch._confirm`). The worker wakes on NOTIFY only for a job due now. A job scheduled for later waits for the worker's next poll, every 5 s (`WorkerSettings.polling_seconds`). So the confirmation lands up to 5 s after the cell already had the version. The front door had refused at 2.55 s, 3 s before the step was done.
  - `scale_to_zero` is Cloud Run's own update: manual scaling to 0, then the agent polls every second until `observedGeneration` catches up. About 6 s, the same in both runs.
- **Options.**
  1. Confirm inside the job. `_confirm` checks every 0.25 s within the same job up to `confirm_by` (10 s at most), instead of re-deferring. It holds one of the worker's 4 slots for up to 10 s during a drill. The deny is then done when the cell has it, about 2.5 s. No cost.
  2. Poll the queue every 1 s instead of 5 s. That cuts every scheduled job's lag, at 5 times the idle queries on the control database. Almost no cost, but it touches every job.
  3. Start `scale_to_zero` alongside the deny instead of after it. That saves about 3 s more, but it changes SSC-025's fixed order.
  4. Count the drill to the refusal and the stream cut (3.2 s), and report `scale_to_zero` beside it. Changes the pass line.
- **Recommended:** 1, then three drills on the new control image. The arithmetic gives about 2.5 + 0.7 + 6.1 + 0.1 = 9.4 s, which is the second drill's figure, so the margin stays under 1 s. If a drill still goes over, 3 or 4 go to the founder with the figures.
- **Chosen 2026-10-06 (lead, under the founder's approval of all actions the same day):** option 1, then three drills. The result goes in pass 2.

## Restore drill (T5)

- **Clone:** `ssc-cell` cloned with `t5 ops --restore-instance`; 28.5 min (17:43 to 18:11 UTC on 2026-10-05).
- **Checked on the clone:** private IP only, all 11 databases present. The clone was deleted the same evening.
- **What a customer restore needs, not built:** a command that clones to a point in time, points the cell's database name at the clone (or copies one app's database back), and rotates the affected apps' passwords. Budget about 30 minutes of downtime per restore at `db-f1-micro`. Belongs to SSC-059's runbooks.

## Settled here

- **Subnet layout:** pass 2 (README T1, last line, not yet run).
- **Gateway to `auth.delimitus.com/internal/redeem`:** pass 2 (SSC-064 runbook 11d). The real login worked on cell 1 through the gateway, so the path exists; which path it took is not yet read.
- **Cell agent ingress under `run.allowedIngress`:** `run.allowedIngress` is in force on both cells as `is:internal, is:internal-and-cloud-load-balancing` (T1 policy listing). The agent's own reachability under it: pass 2.

## Deferred live checks

These are the checks earlier tickets left to this run, one line each with where the command is. Mark each pass or fail and add the number seen.

| Check | Where the command is | Result |
| --- | --- | --- |
| Logs: `_Default` in `us-central1` (not `global`), both views present | `infra/README.md`, App logs, check 1 | |
| Logs: a printed line followed within 5 s; ingestion delay | App logs, check 2 | |
| Logs: `viewAccessor` on the two views is enough, the bucket refused | App logs, check 3 | |
| Logs: one `OR` view accepted or not | App logs, check 4 | |
| Logs: build source and health without waking the app | App logs, check 5 | |
| Agent: max 1 instance, concurrency 200, timeout 300 s | App logs, check 6 | |
| Usage: `sscCellAgentUsage` alone is enough, no `roles/monitoring.*` | `infra/README.md`, App usage, check 1 | |
| Usage: data within 15 minutes; delay seen | App usage, check 2; kit `instances --metric billable_instance_time` | |
| Usage: `instance_count` stays `active` during an idle WebSocket | App usage, check 3; kit `t9 hold --hours 0.34`, then `instances` | |
| Worker pool on `launch_stage="BETA"` applies and runs | `docs/runbooks/ssc-064-control-plane.md` steps 4 and 9, then 11a; `infra/ssc_infra/control.py` `worker_pool` | |
| Gateway reaches the auth host from zero, through the bypass rule and NAT | SSC-064 runbook, 11d | |
| Relay TLS to `run.app` with the image's CA bundle; WebSockets and SSE through the relay | `infra/README.md`, Gateway, Removing access, check 6; kit `t10`'s WebSocket leg | |
| Removing access: grant, open stream, group, gateway at zero | Gateway, Removing access, checks 1, 3, 4, 5 | |
| Compile time, grant change or command to `latest.json` | Removing access, check 2; Kill switch, check 3; kit `t8` (`latest.json changed`) | |
| Kill drill: cold gateway, awake gateway, full stop under 60 s, enable | `infra/README.md`, Kill switch, checks 1 to 5; kit `t8`, `instances` | |
| Anchors written to the cell bucket, verified, reanchored | `infra/README.md`, Audit anchors, checks 1, 2, 4 | |
| Only the worker writes anchors; no default account holds `roles/editor` | Audit anchors, check 3 | |
| Cell agent ingress under `run.allowedIngress` | `infra/README.md`, Organisation policies, and "Policies, live (SSC-086 T11)"; `t3 nightly` reaching the agent through its host | |
| Policies refuse the ten listed changes | `infra/README.md`, Done-when checks, "Policies, live (SSC-086 T11)" | PASS, 10/10 refused for the founder |
| Subnet layout | README T1, last line | |
| No-internet floor: rules and DNS unchanged with each flag, `no_direct_egress` passes | `infra/README.md`, No-internet floor, live check (cell 2, before T12) | |
| Deployer: platform job, re-apply, worker settings, stateful deploy into an empty cell | `infra/README.md`, The cell deployer, live steps 1 to 5 | |
| Build images built and set | `infra/README.md`, Builds, live steps | |
| Secret intake: door, version add, read-back refused, condition, deny rule | `infra/README.md`, Secrets, checks 1 to 5 | |
| App databases: roles, Data API, DNS name, `verify-full`, deployer record, 25 connections, superuser, CMEK with CAS | `infra/README.md`, App databases, checks 1 to 9 | Partly: per-app databases and roles, cross-connect refused, `verify-full` after d89ea6b (T5). The rest in pass 2 |
| Certificate issue time and `entry_probe` | `infra/README.md`, Public entry; kit `t2` | Certificate FAIL (87.9 and 21.7 min); `entry_probe` PASS |
| `deny_probe` and `snapshot_rtt` on cell 1 | `infra/README.md`, Done-when checks | `deny_probe` PASS (T11). `snapshot_rtt` measured on cell 2: worst 3.71 s; cell 1 in pass 2 |
| Control plane done-when a to f | SSC-064 runbook, 11 | Done (SSC-064 step 11, 2026-10-05) |
