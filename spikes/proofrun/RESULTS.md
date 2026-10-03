# SSC-086 results

The staging proof run for the architecture of 2026-10-03 (architecture document, section 8). Each row is filled from the final line of its command (`README.md`), and the full output is in `results/t<n>.json`. A failed row is not worked around in the stack: it goes back to the architecture document as a written change with its cost.

- **Cells:** cell 1 `c-<label 1>` (every flag on, kept as the probe cell), cell 2 `c-<label 2>` (every flag off, destroyed in T12).
- **Run:** <dates>
- **Commit:** <commit>
- **Login:** <real login through auth.delimitus.com | sealed cookie, real login not exercised>

## Results

| # | Proof | Pass when | Command | Result |
| --- | --- | --- | --- | --- |
| T1 | Two cells from the amended stack, one full and one empty | `cell_diff` 0 differences outside what the flags name, no policy override; cell 2 under $1 a day | `t1 diff`, `t1 cost` | |
| T2 | Load balancer and wildcard certificate, serverless NEG to the gateway at min 0 | A browser reaches a probe app by its public host; certificate issued within 30 minutes of the DNS record | `t2` | |
| T3 | The 14 runtime probes through the new path, gateway at min 0 | All pass; nightly schedule restored against cell 1; the app's `run.app` URL accepted as the ID-token audience | `t3 nightly`, `t3 public` | |
| T4 | `cannot_reach_peer_cell` from cell 1 against cell 2 | Network refusal before any IAM refusal on the app; the gateway answers only through its load balancer | `t4` | |
| T5 | `db-f1-micro`, ten app databases, cross-connect, restore | Instance under 15 minutes; each database under 1 minute; cross-connect refused; restore written up | `t5 ops`, `t5 cross` | |
| T6 | NAT for the data gateway's position; Envoy CONNECT proxy on the `e2-micro` | Outbound calls leave from the reserved IP; unlisted hosts refused | `t6 nat`, `t6 proxy` | |
| T7 | Cold start, ten samples a series, 26-minute gap: static, API, Streamlit; gateway cold and warm | Medians reported; none worse than the bake-off (4.5 s, 8 s, 22 s) plus the gateway's start; Direct VPC egress delay measured | `t7 run`, `t7 report` | |
| T8 | Kill drill against an open WebSocket | Under 10 s end to end | `t8` | |
| T9 | Streamlit open 24 hours, instance-billed and request-billed; the bill | Each within 20 % of $0.0684 and $0.0909 an hour; the 60-minute drop and the reload documented | `t9 hold`, `t9 report`, `t9 bill` | |
| T10 | Gateway on gen1 at 0.5 vCPU, same probes | Pass or fail recorded; decides the gateway's cost per session hour | `t10` | |
| T11 | Deny probe from a cell 1 app against cell 2's secret and bucket | Refused by IAM, not only by the network | `t11` | |
| T12 | Delete cell 2 and watch the billing slot | Linked count back to 4 the same day; recorded here and in SSC-089 | `t12` | |

## Stand-ins and fallbacks

Nobody may read these rows as proof of the code they stand in for.

- **T6 is not evidence for SSC-050 or SSC-053.** The data gateway is a Cloud Run job (`standins/egress`) running as `ssc-data` with its tag, on the gateway subnet, with Direct VPC egress. The proxy is stock Envoy with a fixed two-host list, started on the `e2-micro` by a cloud-config override (`t6 envoy-config`).
- **T8 is not evidence for SSC-054.** It covers the open WebSocket, the front-door refusal and the instance stop, run with `ssc disable` because the console has no login. There is no query or tunnel leg.
- **T2/T3:** <the real login was / was not exercised>. If it was not, the cookie was sealed by `seal_cookie.py` with the cell's own keyring.
- **T1:** `cell_diff` leaves out what differing flags name. The left-out list is printed with the result.
- **T9 request-billed leg and T10** run on documented overrides (README), not on stack settings.

## Restore drill (T5)

<clone command, time from `t5 ops --restore-instance`, what was checked on the clone, what a customer restore would need>

## Settled here

- **Subnet layout:** <`gcloud compute networks subnets list` output; the decision on isolation grounds>
- **Gateway to `auth.delimitus.com/internal/redeem`:** <path seen in SSC-064 runbook 11d>
- **Cell agent ingress under `run.allowedIngress`:** <result>

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
| Policies refuse the ten listed changes | `infra/README.md`, Done-when checks, "Policies, live (SSC-086 T11)" | |
| Subnet layout | README T1, last line | |
| No-internet floor: rules and DNS unchanged with each flag, `no_direct_egress` passes | `infra/README.md`, No-internet floor, live check (cell 2, before T12) | |
| Deployer: platform job, re-apply, worker settings, stateful deploy into an empty cell | `infra/README.md`, The cell deployer, live steps 1 to 5 | |
| Build images built and set | `infra/README.md`, Builds, live steps | |
| Secret intake: door, version add, read-back refused, condition, deny rule | `infra/README.md`, Secrets, checks 1 to 5 | |
| App databases: roles, Data API, DNS name, `verify-full`, deployer record, 25 connections, superuser, CMEK with CAS | `infra/README.md`, App databases, checks 1 to 9 | |
| Certificate issue time and `entry_probe` | `infra/README.md`, Public entry; kit `t2` | |
| `deny_probe` and `snapshot_rtt` on cell 1 | `infra/README.md`, Done-when checks | |
| Control plane done-when a to f | SSC-064 runbook, 11 | |
