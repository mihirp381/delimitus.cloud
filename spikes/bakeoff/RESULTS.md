# SSC-001 results (run 2026-09-29)

Throwaway accounts: GCP project `delimitus-0926` (us-central1), AWS account in us-east-1, Azure subscription (eastus, runner in westus2), Fly org `personal` (iad). All checks ran from a VM inside each candidate's network, except Fly, which ran from a laptop. All resources are torn down. The raw JSON stays out of git (`results/` is ignored); this file holds the numbers.

## Scorecard

| Check | gcp | aws | azure | fly |
|---|---|---|---|---|
| Direct egress blocked: TCP 443 | blocked | blocked | blocked |  |
| Direct egress blocked: TCP 80 | blocked | blocked | blocked |  |
| Direct egress blocked: UDP 53 | blocked | blocked | blocked |  |
| Direct egress blocked: UDP 443 (QUIC) | blocked | blocked | blocked |  |
| Direct egress blocked: IPv6 TCP 443 | blocked | blocked | blocked |  |
| DNS exfil blocked (system resolver + direct UDP) | pass | pass | pass |  |
| Machine token has no permissions | pass | pass | pass |  |
| Default address returns 403 from internet | 404 | unreachable | unreachable |  |
| Traffic only via internal LB, host intact | True | True | True |  |
| App cannot reach peer app | pass | pass | fail |  |
| Cold start static p50 (<1.5 s) | 4.47 s | 75.05 s | 25.38 s | 2.20 s |
| Cold start static max | 12.75 s | 124.70 s | 32.59 s | 2.30 s |
| Cold start Python API p50 (<3 s) | 8.16 s | 34.15 s | 24.06 s | 5.75 s |
| Cold start Python API max | 18.32 s | 35.70 s | 30.99 s | 9.27 s |
| Cold start Streamlit p50 (<10 s) | 22.29 s | 60.90 s | 39.27 s | 4.32 s |
| Cold start Streamlit max | 36.05 s | 74.80 s | 45.94 s | 4.89 s |
| WebSocket held (target 1800 s) | 1801.5 s | 1800.3 s | 1807.7 s |  |
| SSE held (target 600 s) | 606.5 s | 600.0 s | 628.6 s |  |
| Cut-off time (<10 s) | 1.58 s | 6.12 s | 1.95 s |  |
| Refuses all public ingress | pass | pass | pass |  |
| Authorization header passes through unchanged | pass | pass | pass |  |
| Empty-cell monthly cost (USD) | 199.37 | 173.71 | 362.02 | 49.34 |
| Streamlit WS origin check works behind proxy | pass with XSRF and CORS on (same origin opens, foreign 403) | pass with XSRF and CORS on, Origin must include the ALB port | pass with XSRF and CORS on (same origin opens, foreign 403) | not tested |
| Org-level deny on reading secret values | yes, IAM deny policy on secretmanager versions.access (documented, not tested) https://cloud.google.com/iam/docs/deny-overview | yes, SCP deny on secretsmanager:GetSecretValue (documented, not tested) https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_scps.html | no, deny assignments cannot be authored directly https://learn.microsoft.com/azure/role-based-access-control/deny-assignments | no |
| One isolated account/project per customer | yes, project per customer via Resource Manager API | yes, account per customer via Organizations CreateAccount (default quota 10, raisable) | subscription per customer needs EA or MCA billing; test subscription is pay-as-you-go | org per customer; programmatic creation Unknown |
| MicroVM or equivalent isolation between apps | yes, gen2 sandbox per instance (documented) https://cloud.google.com/run/docs/about-execution-environments | yes, each Fargate task has its own isolation boundary (documented) | Unknown, not documented for apps in one environment | yes, Firecracker |
| Managed Postgres: PITR + customer-managed keys | yes/yes, Cloud SQL (documented, not tested) | yes/yes, RDS (documented, not tested) | yes/yes, Flexible Server (documented, not tested) | PITR Unknown, CMEK Unknown |
| Managed build service | yes, Cloud Build used for the spike images | yes, CodeBuild (not used; images pushed with local buildx) | blocked, ACR Tasks refused (TasksOperationsNotAllowed) until a support request | yes, remote builder used for the spike |
| Managed log store with query API | yes, Cloud Logging entries.list | yes, CloudWatch Logs Insights StartQuery | yes, Log Analytics query API | Unknown |
| Fixed outbound IP | yes, Cloud NAT reserved IP | yes, NAT gateway Elastic IP | yes, NAT gateway static IP | yes, static egress IP $0.005/h |
| Team operating experience (years) | current production cloud for Delimitus | not used for Delimitus | not used for Delimitus | not used for Delimitus |
| Where likely customers' data lives | n/a, no customers yet | n/a, no customers yet | n/a, no customers yet | n/a, no customers yet |

## What the numbers mean

- **Cold start.** Every candidate misses every cold-start target when an app scales to zero. Samples are the first byte through the internal load balancer after an idle gap longer than the platform's scale-down delay.
  - GCP: a 15-minute gap was too short; Cloud Run kept instances idle, so samples used a 26-minute gap plus the first request after deploy. Cloud Run's own `container/startup_latencies` metric shows container start of 0.6 to 0.9 s (static), 2.4 to 7.7 s (API) and 1.9 to 9.6 s (Streamlit). The remaining 1 to 15 s is instance placement and the Direct VPC network attach.
  - AWS: Fargate has no request-driven scale-to-zero. The numbers are ECS desired count 0 to 1, which includes task placement and image pull. A production app on AWS needs one warm task at all times.
  - Azure: 900 s gap; Container Apps scales to zero after 300 s. The first API sample was warm and is dropped.
  - Fly: `fly machine stop`, then the first request through the public proxy. It is the floor for these images: 2.2 s static, 4.3 s Streamlit, 5.8 s API.
- **Cut-off.** Fastest mechanism per cloud; all measured mechanisms:

| Cloud | Mechanism | Cut-off |
|---|---|---|
| GCP | delete service | 1.58 s |
| GCP | 100 % traffic to a tombstone revision | 2.28 s |
| GCP | remove invoker binding | 79.9 s |
| GCP | swap LB route to a black-hole backend | 142 s |
| AWS | stop task | 6.12 s |
| AWS | listener fixed-response 403 | 7.82 s |
| Azure | deactivate revision | 1.95 s |
| Azure | disable ingress | 13.2 s |

- **Peer.** Azure fails: two apps in one Container Apps environment reach each other on the internal FQDN (HTTP 200). Isolation on Azure needs an environment per app.
- **Default address.** GCP's `run.app` address answers 404 from the internet, with or without a token. AWS and Azure have no public address.
- **Host header.** AWS forwards `host:port` as the client sent it. The first run's comparison ignored the port and reported a false failure; the runner now accepts both forms.
- **Metadata.** On GCP the token exists but cannot reach any Google API, because egress is denied. On AWS the ECS credential endpoint is not called by the probe; the task role has no policies. On Azure the probe calls IMDS only, not `IDENTITY_ENDPOINT`; the identity has no role assignments.
- **DNS.** The canary names never appeared in the public zone's query log on any cloud.
- **Cost.** See `COST_SHEET.md`. Keeping one prod instance warm per app adds about $9.86 a month per app on GCP (1 vCPU, 512 MiB, idle rate) and about $9.01 on AWS (0.25 vCPU, 0.5 GB).

## Not tested

- Org-level secret deny, Postgres PITR with customer-managed keys, and account-per-customer creation are read from each cloud's documentation, not exercised.
- AWS CodeBuild was not used; images were pushed with local `docker buildx`.
- The 24-hour idle bill was not measured; see `COST_SHEET.md`.
