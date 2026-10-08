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
| T6 | NAT for the data gateway's position; Envoy CONNECT proxy on the `e2-micro` | Outbound calls leave from the reserved IP; unlisted hosts refused | `t6 nat`, `t6 proxy`, `t6 egress` | Proxy **PASS** 2026-10-06 12:46 UTC on the real proxy (`pegress`): the listed host answered 200 and left from 35.222.140.145, an unlisted host got 403, no credential got 407. NAT leg **PASS** 2026-10-07 00:35 and 00:41 UTC (re-test, 3 and 4 targets): every answer came from 35.222.140.145. But every call in about the first 20 to 37 s of the job timed out at connect, whatever the target. The 12:52 FAIL was that gap: it asked one target, first. See below. |
| T7 | Cold start, ten samples a series, 26-minute gap: static, API, Streamlit; gateway cold and warm | Medians reported; none worse than the bake-off (4.5 s, 8 s, 22 s) plus the gateway's start; Direct VPC egress delay measured | `t7 run`, `t7 report` | **PASS** 2026-10-06 on the re-measure after options 1 and 2 (below). Gateway start median 9.85 s (10). Cold, gateway and app both asleep: static 11.60 s against 14.35, API 15.13 s against 17.85, Streamlit 19.01 s against 31.85. Warm gateway, app asleep: static 2.92 s, API 5.34 s, Streamlit 13.59 s. All 60 requests answered 200. Direct VPC egress delay median 0.02 s (20). Run 2026-10-06 13:53 to 22:15 UTC. First run (2026-10-05 20:56 to 2026-10-06 05:45 UTC): **FAIL** on static cold only, 15.40 s against 13.06. |
| T8 | Kill drill against an open WebSocket | Under 10 s end to end | `t8` | **PASS** 2026-10-07 on the new control image (option 1 below): three drills, 4.35 s, 8.03 s and 4.66 s end to end; refused 2.34 to 2.67 s, stream cut 4.19 to 4.35 s; every step `done`, first attempt. First run **FAIL** 2026-10-06 06:05 UTC on `papi`, cell 1 cold: end to end 12.61 s. The front door refused after 2.55 s and the WebSocket was cut after 3.19 s; the overrun is the run's last steps (`gateway_deny` done 5.56 s, `scale_to_zero` done 12.49 s, `pause_timers` 12.61 s); every step `done`, first attempt. A second drill 48 s later, from the same operator login, passed at 9.45 s (`gateway_deny` 2.42 s, `scale_to_zero` 9.32 s). No query or tunnel leg (no data gateway, SSC-054). Undone with `ssc enable papi`. See written changes. |
| T9 | Streamlit open 24 hours, instance-billed and request-billed; the bill | Each within 20 % of $0.0684 and $0.0909 an hour; the 60-minute drop and the reload documented | `t9 hold`, `t9 report`, `t9 bill` | Running. Was blocked 2026-10-06: Streamlit refused its own stream behind the gateway (403, "disallowed Origin or Host header"). Fixed by c13c52d (below), rolled out 2026-10-06 ~22:36 UTC; the stream opens. |
| T10 | Gateway on gen1 at 0.5 vCPU, same probes | Pass or fail recorded; decides the gateway's cost per session hour | `t10` | **PASS** 2026-10-06 06:15 UTC: 14/14 probes (peer cell skipped, as on one cell), SSE 1.99 s, WebSocket 5/5 ticks, gateway gen1 0.5 vCPU at concurrency 1. Busy hour $0.0477 against gen2 1 vCPU $0.0909 (48 % less), but at concurrency 1 each open session holds its own gateway instance. Gateway put back from the stack after. |
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
- **Accepted 2026-10-07 (founder):** the FAIL stands as measured but does not block the pilot: it delays only a new cell's set-up, before any customer is placed. Both options are built (9ec9e71, a741701).

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

#### Re-measure after options 1 and 2 (pass 2)

- **Changed:** apps' startup probe every 1 s (failure threshold 120, timeout 1); the gateway image compiles bytecode and the gateway has startup CPU boost. Gateway revision `ssc-gateway-00008-g9k`; static, API and Streamlit redeployed so they carry the new probe.
- **Run:** cell 1, 2026-10-06 13:53 to 22:15 UTC, 20 samples (cold and warm alternating), 26-minute gap. `results/t7-r2.state.json`.
- **Result: PASS.** Every median is under its limit, and static and API cold are also under the first run's lower limits (13.06 s and 16.56 s).

  | Series | App | First run median | Re-measure median | 9th of 10 | Slowest | Limit |
  |---|---|---|---|---|---|---|
  | Cold | Static | 15.40 s | 11.60 s | 16.3 s | 17.9 s | 14.35 s |
  | Cold | API | 16.28 s | 15.13 s | 19.3 s | 26.9 s | 17.85 s |
  | Cold | Streamlit | 20.47 s | 19.01 s | 23.8 s | 26.5 s | 31.85 s |
  | Warm | Static | 6.24 s | 2.92 s | 3.8 s | 64.2 s | 14.35 s |
  | Warm | API | 6.05 s | 5.34 s | 7.6 s | 75.2 s | 17.85 s |
  | Warm | Streamlit | 11.98 s | 13.59 s | 16.0 s | 21.5 s | 31.85 s |

  - The probe change did what was expected. With the gateway running, static's own start fell from 6.2 s to 2.9 s.
  - The cold tail is shorter: static's slowest went from 25.2 s to 17.9 s, Streamlit's from 52.5 s to 26.5 s.
  - SSC-092's "5 to 20 seconds" now holds for the medians and for most of the tail. The slowest cold API sample (26.9 s) and the one slow warm sample (below) do not.
- **The gateway did not get faster:** its start median is 9.85 s, against 8.56 s in the first run, so the limits rose with it. Cell 1's logs for the 20 gateway starts in this run (from "Starting new instance"):

  | Part | Median | Range |
  |---|---|---|
  | Instance boot, image and Python imports, until uvicorn's first line | 5.8 s | 3.0 to 7.9 s |
  | Authz startup: metadata token, KMS decrypt, snapshot | 0.7 s | 0.5 to 5.6 s |
  | Port 8080 open to the default TCP startup probe passing | 1.6 s | 0.0 to 5.0 s |

  - Boot is split in two. Six starts were ready in 3.0 to 3.6 s, and the other 14 took 5.2 to 7.9 s. The first run's starts were 5.4 to 7.5 s. Bytecode and boost may explain the fast group; the logs do not show what makes a start slow.
  - The gateway has no startup probe of its own, so Cloud Run uses its default TCP probe. It passed up to 5 s after the port opened. An explicit probe every 1 s, as the apps now have, might remove most of that. That is not measured.
  - The first run's recommendation said to do option 3 (gateway start overlap) "only if the gateway still takes over 6 s". It takes 9.85 s from the client, and 7.5 s to its port. T7 passes without it, so it is the founder's choice.
- **One slow warm sample (20:30 UTC, sample 15):** static took 64.2 s and API 75.2 s; Streamlit took 16.0 s.
  - The gateway was not the cause. It started in 12.4 s and answered its own host at 20:29:09.
  - Both apps got a new instance at 20:29:10. Their containers' first output came 63 s (static) and 74 s (API) later, against about 1.5 s in every other sample. Both passed their startup probe within 1 to 6 s of that output, and the gateway's two requests ended when they did.
  - So the time went before the container ran, on Cloud Run's side. The logs show nothing else. It happened once in 60 app requests in this run and never in the first.
  - **What it affects:** a user could wait over a minute for an app that has been idle. The 120 s probe allowance and the gateway's timeouts held, and the request answered 200.

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
- **Three drills on the new control image, 2026-10-07: PASS.** Control image `ssc-control@sha256:c6ad923c…` (bf41e05). Founder's admin login. Drill 1 started after 100 min idle; drills 2 and 3 each after 20 min idle.

  | Drill (UTC) | Refused | Stream cut | `latest.json` | `gateway_deny` | `scale_to_zero` | End to end |
  |---|---|---|---|---|---|---|
  | 03:09 | 2.67 s | 4.35 s | 3.13 s | 1.25 s | 4.24 s | **4.35 s** |
  | 03:30 | 2.34 s | 4.19 s | 2.04 s | 1.18 s | 7.89 s | **8.03 s** |
  | 03:51 | 2.50 s | 4.27 s | 2.97 s | 1.50 s | 4.54 s | **4.66 s** |

  Every step `done` on its first attempt. `gateway_deny` fell from 5.56 s to under 1.5 s, as option 1 expected. `scale_to_zero` (Cloud Run's own update) took 2.2 s, 6.0 s and 2.2 s; it is now the only large part. Each drill undone with `ssc enable papi`.
- **Not counted:** the first try at 00:45 to 01:27 UTC never reached `ssc disable`. The saved cookie was the founder's, and the founder had no grant on papi's preview (decision 019), so papi answered 404 before the drill. The founder shared both probe previews with themself at about 03:05.

## Written changes (pass 2)

### T9: Streamlit refuses its stream behind the gateway

- **Seen.** Every `/_stcore/stream` open got 403 from Streamlit itself, not from the gateway. Its log says "Rejecting WebSocket connection with disallowed Origin or Host header": Origin is the app's public host, Host is its `run.app` host. That is decision 023 working as written, since Cloud Run routes by Host and the public host goes in `X-Forwarded-Host`. Page loads and `/_stcore/health` answer 200 with the same cookie.
- **Reproduced locally** with Streamlit 1.64.0 and the same two headers: refused by default, accepted with `STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS=https://<app host>`, accepted with `STREAMLIT_SERVER_ENABLE_CORS=false`.
- **What it affects:** every Streamlit app on the platform. The page shows, then hangs on "Connecting". T7's Streamlit figures stay valid, since they time the first page load. Other WebSocket apps pass (T10). Any other framework that checks Origin against Host will fail the same way.
- **Options.**
  1. **The agent sets `STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS` to the environment's public host on every service (recommended).** The host is known at deploy, so this is one variable in the agent. Streamlit keeps its own check, and other apps ignore the variable. Cost: $0.
  2. Set `STREAMLIT_SERVER_ENABLE_CORS=false`. Simpler, but it turns the check off for any origin.
  3. The gateway forwards the public host as Host. Not possible: Cloud Run would not route it.
- **Then:** apply 1 on cell 1, open pstream in a browser, run the `instance_count` hold (20 min), then T9.
- **Chosen 2026-10-06 (founder):** option 1. The value goes in the control plane's spec, beside `SSC_APP_ORIGIN` (`identity_env`), and the agent sets it on the service as usual. Added by the agent alone, it would differ from the spec's fingerprint and redeploy on every pass. Every environment gets one new revision when the new control image rolls out, which waits for the T7 re-run to end.

### T6: NAT for the data gateway's position

- **First runs (2026-10-06, FAIL).** Three runs of the stand-in job timed out calling 1.1.1.1:443 after 10 s. With NAT logging on, the flow 10.20.4.18 → 1.1.1.1:443 was given the reserved IP, so it left the cell. The gateway service (10.20.4.16 → the auth host) and the proxy VM work through the same NAT. Settings: NAT on the gateway subnet only, manual IP, firewall `egress-data` open to all for the `ssc-data` tag.
- **Re-test (2026-10-07, PASS).** Kit 876a574 gave `t6 nat` a `--target host@ip[/path]` flag. Image `proofrun-egress:2` (sha256:21c79158…), the same job settings, created 00:31 and deleted 00:41 UTC. The stand-in asks its targets one after another, 10 s timeout each:

  | Run (UTC) | Asked, in order | Result |
  |---|---|---|
  | 00:35 | checkip.amazonaws.com (100.59.170.105), ifconfig.me (34.160.111.145), 1.1.1.1 `/cdn-cgi/trace` | first two timed out at connect; 1.1.1.1 answered 200, seen as 35.222.140.145, connect 0.011 s, about 20 s after start |
  | 00:41 | 1.1.1.1, ifconfig.me `/ip`, checkip.amazonaws.com, ifconfig.me | first three timed out at connect; the last answered 200, seen as 35.222.140.145, connect 7.1 s, about 37 s after start |

  - 1.1.1.1 fails when asked first and answers when asked third, so no target is at fault. Outbound calls from a new job instance don't connect for roughly the first 20 to 37 s; after that they connect and leave from the reserved IP.
  - The first runs asked one target, at once, so they only ever saw the gap.
  - The gateway service and the proxy VM run for long periods, so they don't show it.
- **What it affects:** only the data gateway (SSC-050), which is not built yet. The proxy path that apps use passed.
- **For SSC-050 (not decided here).** The data gateway must not count on outbound calls in its first ~40 s. It can retry its first connect for up to 60 s, or check it can reach a host before it reports ready. Two runs do not give a firm bound.
- **State:** the stand-in job deleted 00:41:20 UTC. NAT, its logging and the firewall not changed.

## Cost reconciliation (SSC-096), first run, part 1

Run 2026-10-06 with `--cells` for the two proof cells and a bill with no lines. The October bill can only be exported after 5 November (runbook step 1), and T9 has not run. So this part checks the model's own sums and nothing else.

- **The model doesn't add up to its own stated figures (section 7):**

  | Line | Stated | Parts give |
  |---|---|---|
  | Empty cell | $23.00 | $24.25 |
  | Cell with a database | $36.00 | $37.25 |
  | Full cell | $43.00 | $44.25 |
  | Ten customers, cells fixed (low) | $367 | $402 |
  | Ten customers, session apps (low / high) | $160 / $320 | $120.38 / $240.77 |
  | Ten customers, rare apps (high) | $42 | $68.93 |
  | Platform prod | $75 | $68.67 |

  - The cell lines are each $1.25 off. The stated sums imply a $17 load balancer, and the model has $18.25.
  - The empty cell is $0.75 under A7's $25 on the model.
- **Not changed.** No stated figure is changed to make section 7 clean (runbook step 5). They change when a bill shows the right figure.
- **Waits for:**
  - The October bill for `ssc-c-proofcell01`, `ssc-c-proofcell02` and `ssc-control-prod`. Re-run with `cells.csv` from the stacks, and add cell 1's `database` and `egress` rows.
  - T1 cost: two whole billing days of cell 2.
  - T9's `t9 bill`, which checks $0.0684 and $0.0909 an hour. T9 is blocked by the Streamlit change above.

## Restore drill (T5)

- **Clone:** `ssc-cell` cloned with `t5 ops --restore-instance`; 28.5 min (17:43 to 18:11 UTC on 2026-10-05).
- **Checked on the clone:** private IP only, all 11 databases present. The clone was deleted the same evening.
- **What a customer restore needs, not built:** a command that clones to a point in time, points the cell's database name at the clone (or copies one app's database back), and rotates the affected apps' passwords. Budget about 30 minutes of downtime per restore at `db-f1-micro`. Belongs to SSC-059's runbooks.

## Live checks, 2026-10-07 (cell 1)

- **Build fixtures (SSC-015), app pfix.** 19 run one at a time. The expected code for every
  must-fail fixture but the three below; the must-succeed ones build and go healthy. Fixed:
  cs-express-hello and cs-vite-app had placeholder lockfiles (de5ef25); listed-native-library
  failed because Railpack mounts each `--env` value as a BuildKit secret and the apt package
  values had none (3bd41c0). The build account holds only `roles/logging.logWriter` and no role
  on the cell bucket.
- **False healthy (SSC-016).** cf-startup-hang, cf-exits-nonzero and cf-no-port-bound went live
  as healthy. Watching the v2 API every 0.5 s on app phang: a new revision of an existing service
  shows `Ready` succeeded for about a second, with no `ContainerHealthy` yet, then goes back to
  reconciling and fails two minutes later. Ready now needs `ContainerHealthy` succeeded and the
  revision not reconciling (ac996e1). Every ready revision on cell 1 has both. Not live until the
  agent rollout after T9.
- **Rate limit.** 60 requests then 1 a second per login. Two `--wait` deploys at once failed on
  a poll while their builds ran on; the wait now sleeps `Retry-After` and polls again (a994ae5).
- **Drivers (SSC-040).** node-pg 8.23, Prisma 7.10 and Django 5.2 connect with `DATABASE_URL` as
  given. `pg_stat_ssl` shows app roles nothing; `sslmode=verify-full` is what proves TLS.
- **Admin connection (SSC-040).** A preview with no requests sleeps about 3 minutes after it
  starts, so ten slow deploys never overlap. All ten hold apps were woken by `ssc database
  rotate` (a redeploy, no build); a further rotation at 06:45:04 took 3 s while every role was at
  its limit of 2 (each new instance refused with `too many connections for role`). The same
  refusal shows why the fix-it pool size is 1: with 2 held, a new instance cannot connect until
  the old one stops.
- **T9 instance hold.** Besides the 60-minute drop, one at 37 minutes (05:59:22): Cloud Run
  replaced pstream's instance on the same revision, with no deploy. The hold reconnected at once.
- **T9 request hold** (ended 2026-10-08 ~04:01 UTC): 8.02 h held over 8.02 h, PASS. 8
  reconnects, longest gap 2.5 s, none refused. 7 of 8 drops at about 60 minutes, one at 7.0
  minutes (stream 3). `t9 report` and `t9 bill` are still to run, the bill after it is exported.
- **Rollback and failed deploy (GA-1.5), app ga1pg01 on cell 2, 2026-10-08, control 69cb999f.**
  R4 (a second deploy) went live in 5 min 8 s, most of it the build. Before 997d5cc, R3 failed
  HEALTH_CHECK_FAILED after 180 s: Cloud Run retires a revision with no traffic unstarted, so it
  never got `ContainerHealthy`. `ssc rollback ga1pg01 R1 --wait` took 17.8 s, PASS (under 30 s).
  R5, made to exit at import, failed HEALTH_CHECK_FAILED in 2 min 40 s with the log tail and the
  fix-it line. The audit log shows traffic moving to R5's revision, Cloud Run refusing it (code 9),
  and the control sending traffic back to R1's revision; `ssc releases` shows R1 still live. The
  audit row was not read: the CLI has no audit command, and the console was not used.
- **First deploys, promote and an agent-built app (GA-1.2, 1.6, 1.7), cell 2, 2026-10-08, control
  6f4d1a94.** ga1lovable (a Lovable export) failed its first deploy, R1, with RUNTIME_ERROR on
  69cb999f: the control moved traffic while Cloud Run was still creating the service, and the
  allowedIngress org policy refused it. On 6f4d1a94 its R2 went live in preview at 14:59. ga1agent,
  written from the `ssc init` pack in an empty folder, ran R1 live at 14:58 and R2 at 15:02, so a
  pinned second deploy still works. `ssc promote ga1pg01 --wait` put R6 in prod at 15:03; the prod
  host answers 302 to sign-in. It has no `[connections]`, so no approval was asked; the gated
  promote is still to run. `uv tool install ssc-cli` from PyPI: 0.0.1 in 6 s. Finding: AGENTS.md
  points apps at `ssc_app.identity` and `@delimitus/ssc-identity`, which are not on PyPI or npm.

## Release, 2026-10-07 (GA-0)

| What | Value |
| --- | --- |
| Live code | a71d35f (mvp-merge before the merge below) |
| Images live | control `sha256:4bb31309…`, agent `sha256:daa5da5e…`, console `sha256:a2bc2739…`, auth `sha256:c6ad923c…` (old; the new auth image waits for T9 to end) |
| Migration head | 0033 |
| Merged, not yet deployed | cbb821f (decision 032), 3e8d07a (approval mail, decision 033), f5b3937, b32c084, f5b53bd, 784ba71, c0aeb18. These are the pilot-blockers fixes |
| Left out of pilot-blockers | c4cdb36 and 7e3bdd9 duplicate routing and console sign-in already on mvp-merge. 04521a0 (revoke `CONNECT` on `postgres` from `PUBLIC`) goes against the SSC-040 acceptance |
| Released 2026-10-08 after T9 | control `sha256:3b426923…` built from 0bf78b3 (f5b3937, f5b53bd, 784ba71) on ssc-api, ssc-auth (new image and env vars, revision 00004) and ssc-worker. Migration head stays 0033, so no migration ran. Worker back to 1 instance and pstream back to instance billing. 3e8d07a's worker mail settings went out in the same apply. b32c084 is CLI only (ssc-cli 0.0.1 on PyPI). cbb821f is a decision. c0aeb18 (data gateway) waits for the cell 2 datagw image (GA-5) |
| Released 2026-10-08, GA-5 platform | control `sha256:69cb999f…` built from 997d5cc on ssc-api (rev 00007), ssc-auth (rev 00005), ssc-worker (1 instance) and the migrate job; console `sha256:695149fc…`. Migration 0034 ran (ssc-control-migrate-zzpq4, exit 0). Carries round-2's GA-5 code (c26feae) and 997d5cc: a deploy moves traffic before its health check, because Cloud Run starts a revision only then. Without it every second deploy failed HEALTH_CHECK_FAILED (ga1pg01 R3, cell 2). Cell 2's apply (agent 067167fe…, datagw 3dea8b28…) follows |
| Released 2026-10-08, first-deploy fix | control `sha256:6f4d1a94…` built from a134158 on ssc-api (rev 00008-tjb) and ssc-worker only (targeted apply, 49 s). ssc-auth and the migrate job stay on 69cb999f until the next full platform apply. No migration. Fixes 997d5cc's regression: a new service's first deploy moved traffic while Cloud Run was still creating it, and the allowedIngress org policy refused the PATCH (ga1lovable R1, RUNTIME_ERROR). Traffic now stays put until go-live and is then pinned |
| Local gates on c0aeb18 | All lint-job gates, `gates/run_gates.py`, root, infra and proofrun pytest, node helpers, console typecheck, test and build pass. Playwright suites not run locally |

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
| Usage: `instance_count` stays `active` during an idle WebSocket | App usage, check 3; kit `t9 hold --hours 0.34`, then `instances` | **PASS** 2026-10-07: one stream held 03:54–04:14 UTC on pstream (0 reconnects); `instance_count` active 20 of 20 minutes, idle 0. Earlier tries: blocked by the Streamlit refusal (fixed in c13c52d), then an expired CLI login |
| Worker pool on `launch_stage="BETA"` applies and runs | `docs/runbooks/ssc-064-control-plane.md` steps 4 and 9, then 11a; `infra/ssc_infra/control.py` `worker_pool` | |
| Gateway reaches the auth host from zero, through the bypass rule and NAT | SSC-064 runbook, 11d | NAT: PASS, the NAT log shows 10.20.4.16 → the auth host on the reserved IP. Bypass rule not read |
| Relay TLS to `run.app` with the image's CA bundle; WebSockets and SSE through the relay | `infra/README.md`, Gateway, Removing access, check 6; kit `t10`'s WebSocket leg | PASS (T10): SSE 1.99 s, WebSocket 5/5. Streamlit's stream refused by Streamlit (T9 change) |
| Removing access: grant, open stream, group, gateway at zero | Gateway, Removing access, checks 1, 3, 4, 5 | |
| Compile time, grant change or command to `latest.json` | Removing access, check 2; Kill switch, check 3; kit `t8` (`latest.json changed`) | Command to `latest.json`: 3.37 s (T8 second drill). Grant change not timed |
| Kill drill: cold gateway, awake gateway, full stop under 60 s, enable | `infra/README.md`, Kill switch, checks 1 to 5; kit `t8`, `instances` | Partly: cold 12.61 s, awake 9.45 s (instances 0 at 9.32 s), `ssc enable` worked. Three drills on the new control image to come |
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

## GA-5 extra cost

The founder accepted on 2026-10-07 that GA-5 work may add to the proof-run bill, recorded here apart from T9. Closed (2026-10-08, 14:00 UTC): **about 15 cents in all; nothing recurring but the DNS zone.** Lines: the sandbox Cloud SQL `ssc-ga5-sandbox` (`db-f1-micro`, 10 GB, `ssc-control-prod`, created 2026-10-08 04:44 UTC, deleted 13:59 UTC), about 12 cents for its nine hours; the GCS bucket `ssc-ga5-sandbox-941314154595` and the BigQuery dataset `ga5_sandbox` in `ssc-control-staging` (one 150-row table, under 4 KB), cents; the `run-app` private DNS zone on cell 2, $0.20 a month; cell 2 request traffic on the data gateway, the agent and the gateway, inside Cloud Run's free tier; one gateway image build and four cell 2 applies, Artifact Registry storage only. No egress proxy (the `egress` flag stays off on cell 2).
