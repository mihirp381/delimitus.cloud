# infra

Pulumi in Python for SSC on Google Cloud (SSC-013, decisions 021, 022 and 025). This is a separate uv project outside the workspace, so the provider SDK never reaches the services.

| Stack | What it holds |
| --- | --- |
| `platform` | Folders `ssc-cells/{prod,staging}` and `ssc-sandbox`, with logs in `us-central1`. The location policy. `ssc-control-staging` and its `ssc-control` identity. The folder rule denying secret reads. Just-in-time staff access. The $250 monthly budget. The public DNS zones `delimitusapps.com.` and `delimitus.com.` in `ssc-platform-0`. |
| `c-<cell label>` | One cell, in two parts. At onboarding: project `ssc-c-<label>` with a $50 budget alert, identities `ssc-gateway`, `ssc-cell-agent`, `ssc-build` and `ssc-data`, KMS, bucket, Artifact Registry, the VPC with its firewall floor and DNS sinkhole, Cloud NAT with the cell's fixed IP, reserved addresses for the proxy, the data gateway and the database range, the gateway (request-billed, minimum 0, 3600 s requests) behind the cell's public entry, the cell agent, and the cell's own deny rule. On first use: whatever the flags below turn on. |

The flags are turned on by the control plane in SSC-087; until then they are set by hand.

State lives in `gs://ssc-platform-0-pulumi`, and secrets are encrypted with the KMS key `ssc-platform/pulumi-secrets`. Every call's quota goes to `ssc-platform-0`. The tools refuse any command that names `ristretto-506621`.

Stack files (`Pulumi.<stack>.yaml`) are not committed: each holds a data key that the secret scanner flags. On a fresh clone, `bootstrap` (and `bootstrap cell <label>`) writes them again with the KMS secrets provider and the stack's config.

## Cell flags

Five settings in a cell stack's config, all off by default. Turning one on adds only the resources named here (`naming.LAZY_RESOURCES`); the firewall rules and addresses they use exist from onboarding.

| Flag | Default | Adds | About a month |
| --- | --- | --- | --- |
| `database` | `false` | Cloud SQL Postgres 18 `ssc-cell`, zonal `db-f1-micro`, private address in the database range, customer-managed key, backups and point-in-time recovery, and the cell agent's IAM login | $13 |
| `egress` | `false` | The proxy machine: one `e2-micro` with no external address at the reserved proxy address, in a group of one that recreates it. Its proxy software is SSC-053 | $7 |
| `connections` | `false` | The data gateway and file broker `ssc-datagw` on Cloud Run, minimum 0, as `ssc-data`, leaving through the cell NAT | usage |
| `gateway_min` | `0` | The gateway's minimum instances | about $10 each |
| `warm` | `false` | Keeps the gateway at one instance or more (SSC-092) | about $10 |

```
pulumi config set --stack c-<label> database true
pulumi up --stack c-<label>
```

Other settings: `stage` (`staging` or `prod`), `probe`, `probe_digest`, `agent_image`, `gateway_max` (20) and `billing_account` (defaults to the one SSC account; set it to link a new cell to another account, SSC-089).

The stack exports `flags`, so `cell_diff` compares two cells with different flags without their flagged resources.

**Subnets.** Two IPv4 `/24`s: `apps` (10.20.0.0/24) holds only apps; `gateway` (10.20.4.0/24) holds everything that may reach the internet: the gateway, the data gateway and the proxy. Only `gateway` is behind the NAT, so an app has no route out even if a firewall rule were wrong. To put everything in one `/24`, set `SUBNETS` in `cell.py` to the `apps` entry alone and `EDGE_SUBNET` to `"apps"`; the reserved addresses move with it.

## Public entry

Each cell has its own door (SSC-088), created at onboarding with no flag:

- A global external Application Load Balancer on the reserved address `ssc-entry`: forwarding rule `ssc-entry-https` (443) to the HTTPS proxy, URL map and backend service `ssc-gateway`, on a serverless NEG `ssc-gateway` that points at the gateway service. A second rule, `ssc-entry-http` (80), on the same address only redirects to HTTPS. The first five forwarding rules in a project are billed as one, so the redirect adds nothing: the entry is about $18.25 a month plus $0.008 per GB.
- A serverless NEG's backend timeout is fixed at 60 minutes and Google refuses `timeoutSec` on it, so the stack leaves it unset; the gateway's own 3600 s request timeout is what ends a WebSocket at the hour.
- Certificate Manager: DNS authorisation `ssc-cell` for `<label>.delimitusapps.com`, the wildcard certificate `ssc-cell-wildcard` for `*.<label>.delimitusapps.com`, map `ssc-entry` and map entry `ssc-wildcard`. No Cloud Armor.
- The same run writes two records into the apps zone: the authorisation CNAME `_acme-challenge.<label>.delimitusapps.com.` and `*.<label>.delimitusapps.com.` A to the entry address.
- The gateway's invoker is `allUsers`; its ingress stays internal and load balancer, which keeps its `run.app` host closed. **Applying this needs the SSC-095 policy exception first**: domain-restricted sharing refuses the `allUsers` grant, so that policy must not reach a folder holding a cell before its exception exists.
- `ssc--agent.<label>.delimitusapps.com` is reserved for the cell agent (SSC-095) and not routed yet; until then it reaches the gateway like any host that is not an app, and the gateway (SSC-018) answers 404. No slug can claim it: slugs never contain `--`.

The cell stack exports `entry_address`, `public_host_suffix` (`<label>.delimitusapps.com`), `certificate_id` and `agent_host`.

**Zones and the registrar step.** The platform stack owns two public zones in `ssc-platform-0`: `delimitusapps` (`delimitusapps.com.`, the cells' records) and `delimitus` (`delimitus.com.`, for `api`, `auth` and `keys`, whose records come later). Both are protected from deletion. A cell stack runs as the operator, who may write records in the apps zone only, through the custom role `sscZoneRecords` granted on that zone. After `pulumi up --stack platform`, the founder sets each domain's name servers at the registrar from `pulumi stack output apps_zone_name_servers` and `platform_zone_name_servers`, and deletes the domain's DS records there. Both domains still carry DS records from zones that no longer exist; with them left in place, validating resolvers fail every lookup and no certificate is issued. The `.com` delegation is cached for up to 48 hours, so do this well before the first cell.

**DNSSEC** is off on both zones for now. Turning it on later is a zone setting plus a new DS record at the registrar.

**Certificate issue time.** Google issues the certificate once the authorisation CNAME resolves publicly, usually within minutes; the done-when allows 30 minutes from the record. Until then the HTTPS rule answers with a TLS failure. `entry_probe` waits up to 30 minutes and prints how long it took.

## First run

You need these org roles:
- `resourcemanager.folderAdmin`
- `iam.denyAdmin`
- `logging.admin`
- `orgpolicy.policyAdmin`
- `privilegedaccessmanager.admin`

You also need Application Default Credentials. Bootstrap is safe to run again; run it once more after an API is added to `PLATFORM_APIS` (Certificate Manager was, for SSC-088), because every call's quota goes to `ssc-platform-0`.

```
cd infra && uv sync
uv run python -m ssc_infra.bootstrap            # folder, project, APIs, bucket, key, platform stack
export PULUMI_BACKEND_URL=gs://ssc-platform-0-pulumi
pulumi up --stack platform
```

## A cell

```
uv run python -m ssc_infra.bootstrap cell testcell01 --probe
pulumi up --stack c-testcell01
```

A staging cell can be destroyed with `pulumi destroy --stack c-<label>`. A prod cell's project, database and services are protected. A deleted project ID stays reserved for 30 days.

## Done-when checks

```
uv run python -m ssc_infra.cell_diff testcell01 testcell02   # same apart from what their flags name
uv run python -m ssc_infra.deny_probe testcell01             # secret read denied (needs --probe)
uv run python -m ssc_infra.snapshot_rtt testcell01           # snapshot round trip under 5 s
uv run python -m ssc_infra.entry_probe testcell01            # public host answers; run.app host refused
```

`cell_diff` reads stored state, so run `pulumi refresh` on both stacks first: a certificate still provisioning in one shows as a difference.

`uv run pytest` runs both programs against Pulumi mocks, with no cloud.
