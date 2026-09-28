# Empty-cell monthly cost (SSC-001, assumption A7: under $450)

One row per line item, one number per candidate, all in USD per month for one idle customer cell in the region the team picks. Fill numbers from the price list the engineer used and put the URL in the last column. Leave a cell blank rather than guessing. Fly is the control column.

| Line item | GCP | AWS | Azure | Fly (control) | Price-list URL used |
|---|---|---|---|---|---|
| Internal load balancer (forwarding rule / ALB hours / App Gateway) | | | | n/a | |
| NAT gateway with one fixed IP (hours + idle IP) | | | | n/a | |
| Managed Postgres, smallest HA tier, PITR on (Cloud SQL / RDS Multi-AZ / Flexible Server ZRHA) | | | | | |
| Two small gateway instances, always on (Cloud Run min-instances 1 x2 / Fargate 0.25 vCPU x2 / Container Apps dedicated x2) | | | | | |
| Egress proxy machine, always on (one small VM) | | | | | |
| Log store, 5 GB ingested, 30-day retention | | | | | |
| Secret store, 20 secrets, 10k accesses | | | | | |
| Build service, 60 build-minutes | | | | n/a | |
| Container registry, 5 GB | | | | | |
| Other (list) | | | | | |
| **Total** | | | | | |

Also record the measured 24-hour idle bill from the throwaway project x 30 as a second total, to catch items this table missed.
