# Empty-cell monthly cost (SSC-001, assumption A7: under $450)

**Superseded as the cost model on 2026-10-03.** This sheet is the bake-off's measurement and is kept as the record of it. The current model is section 10 of `Cloud_for_small_soft/SSC_Final_Architecture_2026-10-03.md` (decision 025): an empty GCP cell about $19 a month, with a database about $32, full about $43, plus usage. The internal load balancer, the regional HA database, the two always-on gateway instances and the always-on proxy priced below are retired. A7 becomes "under $25 empty, under $50 full" once SSC-086 measures it.

USD per month (730 h) for one idle customer cell. Regions: GCP us-central1, AWS us-east-1, Azure eastus, Fly iad. List prices fetched 2026-09-29 from the machine-readable catalogs: GCP Cloud Billing Catalog API, AWS Price List API, Azure Retail Prices API, and the Fly pricing page source. Free tiers are **not** applied, because most are per billing account and do not repeat per cell. Blank means Unknown. Fly is the control column.

| Line item | GCP | AWS | Azure | Fly (control) | Price-list URL used |
|---|---|---|---|---|---|
| Internal load balancer | 54.75 | 22.27 | 0.00 | n/a | cloudbilling.googleapis.com/v1/services/E505-1604-58F8/skus; AWS `AWSELB`; prices.azure.com (Azure Container Apps) |
| NAT gateway with one fixed IP | 4.67 | 36.50 | 36.50 | 3.65 | E505-1604-58F8; AWS `AmazonEC2`, `AmazonVPC`; prices.azure.com (NAT Gateway, Virtual Network); fly.io/docs/about/pricing |
| Managed Postgres, smallest HA tier, PITR on | 102.02 | 27.96 | 267.24 | 38.28 | 9662-B51E-5089; AWS `AmazonRDS`; prices.azure.com (Azure Database for PostgreSQL); fly.io/docs/mpg |
| Two small gateway instances, always on | 19.71 | 18.02 | 23.65 | 3.94 | 152E-C115-5142; AWS `AmazonECS`; prices.azure.com (Azure Container Apps); fly.io/docs/about/pricing |
| Egress proxy machine, always on | 13.23 | 13.06 | 16.31 | 3.47 | 6F81-5844-456A; AWS `AmazonEC2`; prices.azure.com (Virtual Machines, Storage); fly.io/docs/about/pricing |
| Log store, 5 GB ingested, 30-day retention | 2.50 | 2.65 | 11.50 | | 5490-F7B7-8DF6; AWS `AmazonCloudWatch`; prices.azure.com (Log Analytics) |
| Secret store, 20 secrets, 10k accesses | 1.23 | 8.05 | 0.03 | | EE82-7A5E-871C; AWS `AWSSecretsManager`; prices.azure.com (Key Vault) |
| Build service, 60 build-minutes | 0.36 | 0.30 | 0.72 | n/a | 8B5D-EF7D-EB12; AWS `CodeBuild`; prices.azure.com (Container Registry) |
| Container registry, 5 GB | 0.50 | 0.50 | 5.07 | | 149C-F9EC-3994; AWS `AmazonECR`; prices.azure.com (Container Registry) |
| Other (see notes) | 0.40 | 44.40 | 1.00 | n/a | FA26-5236-B8B5; AWS `AmazonVPC`, `AmazonRoute53`; prices.azure.com (Azure DNS) |
| **Total** | **199.37** | **173.71** | **362.02** | **49.34** (excludes blanks) | |

Measured 24-hour idle bill x 30: not measured. The spike resources ran for less than 24 hours and carried test traffic, so no clean idle day exists. Measure it on the first production cell.

## Configurations and unit prices

1. **Load balancer.** GCP regional internal Application LB bills at least 3 proxy instances at $0.025/h each, with no forwarding-rule charge (SKU 7790-569D-1B96). AWS ALB is $0.0225/h plus one LCU at $0.008/h. Azure Container Apps ingress has no separate charge; the $0.10/h management meter applies only to Dedicated plans. Whether Azure bills a hidden LB for a workload-profiles environment is Unknown.
2. **NAT.** GCP Cloud NAT is $0.0014/h per VM plus $0.005/h per IP. How Cloud Run Direct VPC egress instances are counted is Unknown; if three count as VMs the line is $6.72. AWS NAT Gateway is $0.045/h plus $0.005/h for the public IPv4. Azure NAT Gateway is $0.045/h plus $0.005/h for a Standard static IP. Fly static egress IP is $0.005/h.
3. **Postgres.** GCP Cloud SQL Enterprise, 1 vCPU and 3.75 GB, regional HA, 10 GB SSD. AWS RDS db.t4g.micro Multi-AZ, 20 GB gp3. Azure Flexible Server General Purpose D2ds_v5, zone-redundant HA (Burstable has no HA), 32 GB times two. Fly Managed Postgres Basic plan with 1 GB storage; PITR on that plan is Unknown.
4. **Gateways.** GCP Cloud Run min-instances 1 x2, 1 vCPU 512 MiB, idle rate; instance-based billing would be $99.86. AWS Fargate 0.25 vCPU 0.5 GB x2. Azure Container Apps consumption min-replicas 1 x2, 0.5 vCPU 1 GiB, idle rate; two D4 Dedicated profiles would be $522.62. Fly shared-cpu-1x 256 MB x2.
5. **Egress proxy.** GCP e2-small, AWS t4g.small, Azure B1ms, Fly shared-cpu-1x 256 MB. Each with a 10 GB disk or volume.
6. **Logs.** GCP gives 50 GiB free per project, so with a project per cell this line is $0. AWS includes $0.03/GB-month storage. Azure is $2.30/GB. Fly log search price is Unknown.
7. **Secrets.** GCP $0.06 per active version, AWS $0.40 per secret, Azure Key Vault Standard $0.03 per 10k operations. Fly is not priced.
8. **Build.** Cloud Build e2-standard-2, CodeBuild general1.small, ACR Tasks at 2 vCPU. ACR Tasks is refused on the test subscription until a support request is approved.
9. **Registry.** Artifact Registry and ECR at $0.10/GB. ACR Basic is a fixed $0.1666/day with 10 GB included.
10. **Other.** GCP two Cloud DNS private zones. AWS six interface-endpoint hours (ecr.api, ecr.dkr, logs in two AZs) at $0.01/h plus DNS Firewall queries. Azure two Private DNS zones.
