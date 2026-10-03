# infra

Pulumi in Python for SSC on Google Cloud (SSC-013, decisions 021, 022 and 025). This is a separate uv project outside the workspace, so the provider SDK never reaches the services.

| Stack | What it holds |
| --- | --- |
| `platform` | Folders `ssc-cells/{prod,staging}` and `ssc-sandbox`, with logs in `us-central1`. The location policy on `ssc-platform`, the cell policy table on `ssc-cells` and the public-invoker tag (SSC-095). `ssc-control-staging` and its `ssc-control` identity. The folder rule denying secret reads. Just-in-time staff access. The $250 monthly budget. The public DNS zones `delimitusapps.com.` and `delimitus.com.` in `ssc-platform-0`. |
| `c-<cell label>` | One cell, in two parts. At onboarding: project `ssc-c-<label>` with a $50 budget alert, identities `ssc-gateway`, `ssc-cell-agent`, `ssc-build` and `ssc-data`, KMS, bucket, Artifact Registry, the VPC with its firewall floor and DNS sinkhole, Cloud NAT with the cell's fixed IP, reserved addresses for the proxy, the data gateway and the database range, the gateway (request-billed, minimum 0, 3600 s requests) and the cell agent behind the cell's public entry, and the cell's own deny rule. On first use: whatever the flags below turn on. |

The lazy flags are turned on by the cell deployer when the control plane asks (SSC-087, below). The operator can still set any flag by hand.

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

Other settings: `stage` (`staging` or `prod`), `probe`, `probe_digest`, `agent_image`, `build_tools_image` and `build_frontend_image` (Builds, below), `gateway_max` (20) and `billing_account` (defaults to the one SSC account; set it to link a new cell to another account, SSC-089).

The stack exports `flags`, so `cell_diff` compares two cells with different flags without their flagged resources.

**Subnets.** Two IPv4 `/24`s: `apps` (10.20.0.0/24) holds only apps; `gateway` (10.20.4.0/24) holds everything that may reach the internet: the gateway, the data gateway and the proxy. Only `gateway` is behind the NAT, so an app has no route out even if a firewall rule were wrong. To put everything in one `/24`, set `SUBNETS` in `cell.py` to the `apps` entry alone and `EDGE_SUBNET` to `"apps"`; the reserved addresses move with it.

## The cell deployer

The control plane turns on a cell's `database`, `egress` or `connections` flag without a person and without new powers of its own (SSC-087, decision 022 amendment pending). NAT and the fixed IP are not lazy.

- **Trigger.** In the control plane, the job `cell:create_resource` (`ssc_control/cell/`), one per cell and resource. It is asked for by the first deploy whose manifest has `[state] postgres = true` (database), the first approved internet host (egress), the first approved data source (connections), or an org admin (`POST /v1/cell/resources/{resource}/enable`, audited). A second request joins the one in flight. Each request, success and failure is an audit row (`cell.resource_requested`, `cell.resource_ready`, `cell.resource_failed`). Nothing turns a flag off: removing a resource is a runbook step.
- **Runner.** The Cloud Run job `ssc-cell-deployer` in `ssc-platform-0`, image `infra/deployer/Dockerfile`, running `python -I -m ssc_infra.deployer <cell label> <database|egress|connections>` as `ssc-cell-deployer@ssc-platform-0.iam.gserviceaccount.com`. It reads the config the stack was last applied with (the stack's `config` output), sets the one flag to `true` and runs `pulumi up` on that one stack. It refuses any third argument, any flag outside the three, a malformed label and any environment variable it does not expect, and gives Pulumi an environment of its own. A run killed halfway is converged by the next one: a state lock older than 3 hours is cancelled, and a create Pulumi never saw finish is imported or dropped.
- **Deployer roles.** On `ssc-cells`: `cloudsql.admin`, `compute.instanceAdmin.v1`, `compute.networkUser`, `iam.serviceAccountUser`, `resourcemanager.tagUser`, `run.admin`. In `ssc-platform-0`: `storage.objectAdmin` on the state bucket, `cloudkms.cryptoKeyEncrypterDecrypter` on the secrets key, `serviceusage.serviceUsageConsumer`. Also the public-invoker tag (above) and the apps zone's record writer. No billing, project-creation, deny-rule or secret-value role.
- **The control plane's part.** `ssc-control` holds `run.jobsExecutorWithOverrides` and `run.viewer` on that one job, nothing more. It starts a run with exactly two arguments, a cell label and a resource, and reads how the run went. Worker settings: `SSC_CELL_DEPLOYER=cloud_run` and `SSC_CELL_DEPLOYER_JOB=projects/ssc-platform-0/locations/us-central1/jobs/ssc-cell-deployer` (unset means none, and a lazy resource fails with `CELL_DEPLOYER_UNAVAILABLE`; `fake` only in `dev` and `test`).
- **The operator's access** is unchanged.

**Why this keeps the control plane without a path to app data.** The control plane gains no role in any cell. It cannot choose what the deployer applies, only which cell and which of three flags, and the deployer's code, image and identity are not the control plane's to change. The flag turns on the same resources the operator would, from the same program, so a compromised control plane can at worst create a database, a proxy or a data gateway that a cell was going to have anyway, and pay for it.

**What it does not protect against.**
- The deployer itself. It can read and decrypt every stack's state, the platform's included, and with `run.admin` and `iam.serviceAccountUser` on the cells folder it can deploy code as any identity in any cell, which reaches app data. Whoever can change its image, its job or its identity's roles holds that power; today that is the operator.
- The image's supply chain: Pulumi, the provider plugin and the Python dependencies inside it run with the deployer's rights.
- Repeated or unwanted runs. The control plane may start the job for any cell and flag as often as it likes; the cost is bounded by the three resources per cell (about $20 a month) and by the budget alert.
- A bug in the cell program. The deployer applies whatever `infra/` says, with only the one flag changed.

**Live steps** (operator, not run by SSC-087):
1. `pulumi up --stack platform`: the identity, its roles and the registry.
2. Build and push `infra/deployer/Dockerfile` (from the repository root) to `us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform/ssc-cell-deployer`, then `pulumi config set --stack platform deployer_image <image@digest>` and `pulumi up --stack platform` again: the job and the control plane's grants.
3. Re-apply each existing cell once (`pulumi up --stack c-<label>`) so its state exports `config`.
4. Set the two worker settings above on the control plane.
5. Deploy a stateful app into an empty staging cell, then run `cell_diff` and `pulumi preview --stack c-<label> --expect-no-changes`.

## Builds

The cell agent runs each build in the cell's own Cloud Build as `ssc-build` (SSC-015, `ssc_agent.cloud_build`). The stack wires it in:

- **Settings.** `build_tools_image` and `build_frontend_image`, both or neither, each `us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform/<image>@sha256:<digest>`. Anything else, or one without the other, fails the stack before apply: a partial set would stop the agent from starting.
- **Agent environment.** With `agent_image` and both images set: `SSC_BUILD_SA` (`ssc-build@ssc-c-<label>.iam.gserviceaccount.com`), `SSC_BUILD_TOOLS_IMAGE` and `SSC_BUILD_FRONTEND_IMAGE`. Without them none of the three is set, and the agent answers builds with `BUILD_NOT_CONFIGURED`.
- **Agent permissions.** `cloudbuild.builds.create` in `sscCellAgentCreate`; `cloudbuild.builds.get` and `cloudbuild.builds.list` in `sscCellAgentRuntime`. Acting as `ssc-build` is the `iam.serviceAccounts.actAs` that `sscCellAgentRuntime` already holds on the project (it covers every account in the cell, as for app identities).
- **`ssc-build`.** `artifactregistry.writer` on the cell's `ssc-apps`; `artifactregistry.reader` on `ssc-platform` in `ssc-platform-0`, which holds the tools image and the Railpack frontend mirror (and the cell deployer's image, which builds can therefore pull); `logging.logWriter`. No storage role of any kind: the bundle's signed URL is all it reads (`tests/test_cell.py`).
- **Policies.** None of the folder policies touches this: the builds run in `us-central1`, on Google's default pool (no VM in the cell), and the registry grant is on a platform project, to an organisation identity.

The cell stack writes the reader grant into `ssc-platform-0`, as it writes the apps zone's records. The operator's run creates it. The cell deployer has no role on that registry, so re-apply each existing cell by hand once before the deployer next runs on it.

**Live steps** (operator, not run by SSC-015):

```
REG=us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform
gcloud auth configure-docker us-central1-docker.pkg.dev
docker buildx build --platform linux/amd64 --provenance=false --metadata-file /tmp/tools.json \
  --tag $REG/ssc-build-tools:railpack-0.40.1 --push infra/build_tools
jq -r '."containerimage.digest"' /tmp/tools.json
docker buildx imagetools create --tag $REG/railpack-frontend:v0.40.1 \
  ghcr.io/railwayapp/railpack-frontend:v0.40.1@sha256:f1973377693af30c9b37a92c97c661c07b277ccdc6be909213c74c771f8d2d6d
docker buildx imagetools inspect $REG/railpack-frontend:v0.40.1
pulumi config set --stack c-<label> build_tools_image $REG/ssc-build-tools@<tools digest>
pulumi config set --stack c-<label> build_frontend_image $REG/railpack-frontend@<mirror digest>
pulumi up --stack c-<label>
```

Railpack tags its frontend `v0.40.1`; there is no `0.40.1` tag.

## Public entry

Each cell has its own door (SSC-088), created at onboarding with no flag:

- A global external Application Load Balancer on the reserved address `ssc-entry`: forwarding rule `ssc-entry-https` (443) to the HTTPS proxy, and URL map `ssc-entry` with two backend services, each on its own serverless NEG: `ssc-gateway` for every host but one, and `ssc-cell-agent` for `ssc--agent.<label>.delimitusapps.com` alone. The gateway's backend never serves the agent host, and the agent's serves nothing else.
- The HTTPS proxy carries the SSL policy `ssc-entry`: TLS 1.2 at least, `MODERN` profile. A second rule, `ssc-entry-http` (80), on the same address only redirects to HTTPS. The first five forwarding rules in a project are billed as one, so the redirect adds nothing: the entry is about $18.25 a month plus $0.008 per GB.
- A serverless NEG's backend timeout is fixed at 60 minutes and Google refuses `timeoutSec` on it, so the stack leaves it unset; the gateway's own 3600 s request timeout is what ends a WebSocket at the hour.
- Certificate Manager: DNS authorisation `ssc-cell` for `<label>.delimitusapps.com`, the wildcard certificate `ssc-cell-wildcard` for `*.<label>.delimitusapps.com`, map `ssc-entry` and map entry `ssc-wildcard`. No Cloud Armor.
- The same run writes two records into the apps zone: the authorisation CNAME `_acme-challenge.<label>.delimitusapps.com.` and `*.<label>.delimitusapps.com.` A to the entry address.
- The gateway's invoker is `allUsers`; its ingress stays internal and load balancer, which keeps its `run.app` host closed. The grant is allowed only because the gateway carries the public-invoker tag (see Organisation policies); the stack binds the tag before it grants.
- The cell agent's ingress is internal and load balancer too, and its invoker is still only the control plane's identity. Its custom audience is `https://ssc--agent.<label>.delimitusapps.com`, so an ID token for that URL is accepted through the load balancer; its `run.app` host is closed. No slug can claim the agent host: slugs never contain `--`.

The cell stack exports `entry_address`, `public_host_suffix` (`<label>.delimitusapps.com`), `certificate_id`, `agent_host` and `agent_url`. `agent_url` is `https://ssc--agent.<label>.delimitusapps.com`, the URL the control plane calls and the audience of its ID token; it was the agent's `run.app` URL before SSC-095. Set `SSC_CELL_AGENT_URL` (worker) and the GitHub variable `SSC_PROBE_AGENT_URL` (nightly) to it.

**Zones and the registrar step.** The platform stack owns two public zones in `ssc-platform-0`: `delimitusapps` (`delimitusapps.com.`, the cells' records) and `delimitus` (`delimitus.com.`, for `api`, `auth` and `keys`, whose records come later). Both are protected from deletion. A cell stack runs as the operator, who may write records in the apps zone only, through the custom role `sscZoneRecords` granted on that zone. After `pulumi up --stack platform`, the founder sets each domain's name servers at the registrar from `pulumi stack output apps_zone_name_servers` and `platform_zone_name_servers`, and deletes the domain's DS records there. Both domains still carry DS records from zones that no longer exist; with them left in place, validating resolvers fail every lookup and no certificate is issued. The `.com` delegation is cached for up to 48 hours, so do this well before the first cell.

**DNSSEC** is off on both zones for now. Turning it on later is a zone setting plus a new DS record at the registrar.

**Certificate issue time.** Google issues the certificate once the authorisation CNAME resolves publicly, usually within minutes; the done-when allows 30 minutes from the record. Until then the HTTPS rule answers with a TLS failure. `entry_probe` waits up to 30 minutes and prints how long it took.

## Organisation policies

The platform stack applies one table (`policies.cell_rules`) to the `ssc-cells` folder, so every cell inherits it. The platform stack exports it as `cell_policies`.

| Constraint | Setting | What it stops in a cell |
| --- | --- | --- |
| `gcp.resourceLocations` | allow `in:us-central1-locations`, `global` | Anything outside `us-central1`. `global` is for the entry's certificate, DNS authorisation and certificate map; global Compute resources are exempt anyway. `ssc-platform` keeps the region alone. |
| `storage.publicAccessPrevention` | enforced | A public object or bucket. Nothing in a cell needs one: public keys are served from `keys.delimitus.com/<label>/jwks.json`, not a cell bucket. |
| `iam.managed.allowedPolicyMembers` | enforced; members: the operator and principals of organisation 878392300952 | A role for anyone outside the organisation, `allUsers` included. **One exception**, below. |
| `iam.disableServiceAccountKeyCreation` | enforced | Service account keys. |
| `compute.restrictVpcPeering` | allow `under:organizations/433637338589` | Peering with anything but Google's service producers, which Cloud SQL's private services access needs. Set `peering_allowed` in the platform stack's config if the first cell's peering is refused. |
| `compute.restrictSharedVpcHostProjects` | deny all | A cell joining another project's network. Becoming a host needs org-level `compute.xpnAdmin`, which nobody holds. |
| `run.allowedIngress` | allow internal, internal and load balancer | A Cloud Run service open on its `run.app` host. |
| `compute.vmExternalIpAccess` | deny all | An external address on a VM. The proxy has none; the NAT and entry addresses are not VM addresses. |
| `sql.restrictPublicIp` | enforced | A public address on Cloud SQL. |

The domain-restricted sharing row uses the managed constraint rather than `iam.allowedPolicyMemberDomains`: the organisation has no Workspace customer, and the legacy constraint cannot name the operator's personal account, so it would refuse the operator's just-in-time and probe grants.

**The one exception.** The gateway's `allUsers` invoker. The platform stack owns the tag `ssc-public-invoker=gateway` on the organisation, and the members rule is off only where that tag is bound. The cell stack binds it to `ssc-gateway` and nothing else. Binding needs `resourcemanager.tagUser` on the value, which the platform stack grants to the operator alone, authoritatively, so a holder added by hand is removed on the next run. The same operator runs both stacks and is organisation and policy admin, so this does not stop the operator. It does stop:
- every runtime identity (cell agent, control plane, apps, nightly), which can never bind the tag and so never make anything public;
- a cell stack mistake that grants `allUsers` or an outside member anywhere but the tagged gateway, which is refused at apply;
- the tag bound anywhere but the gateway (the agent, the project), which `tests/test_policies.py` refuses before apply.

The cell deployer (SSC-087) is the second binder: it applies cell stacks, and the gateway it keeps is tagged. The grant still names nobody else.

**Order.** The policies and the tag come before the first cell: `pulumi up --stack platform` creates the tag, its binder grant and the policies together. Never apply these policies to a folder already holding a cell unless the tag exception is in the same run, or the gateway's `allUsers` grant is refused on its next update. No cell exists yet.

`tests/test_policies.py` checks a full cell's resources against the same table, with one planted violation per policy. `cell_diff` lists the policies in force on each cell.

## First run

You need these org roles:
- `resourcemanager.folderAdmin`
- `resourcemanager.tagAdmin` (the public-invoker tag)
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
uv run python -m ssc_infra.cell_diff testcell01 testcell02   # same apart from what their flags name; policies in force
uv run python -m ssc_infra.deny_probe testcell01             # secret read denied (needs --probe)
uv run python -m ssc_infra.snapshot_rtt testcell01           # snapshot round trip under 5 s
uv run python -m ssc_infra.entry_probe testcell01            # public host answers; run.app host refused
```

`cell_diff` reads stored state, so run `pulumi refresh` on both stacks first: a certificate still provisioning in one shows as a difference. It then prints the policy table each cell inherits, from the platform stack's state, and one read-only `gcloud org-policies list` per cell project; a policy set on the project itself, a policy resource in the cell stack, or a project outside the stage folders is an override and fails the check.

**Policies, live (SSC-086 T11).** On a throwaway staging cell `testcell01` with `database` and `egress` on, and a second cell `testcell02`, each of these must be refused:

```
P=ssc-c-testcell01
gcloud storage buckets add-iam-policy-binding gs://$P-cell --member=allUsers --role=roles/storage.objectViewer
gcloud projects add-iam-policy-binding $P --member=user:someone@example.com --role=roles/viewer
gcloud run services add-iam-policy-binding ssc-cell-agent --project=$P --region=us-central1 --member=allUsers --role=roles/run.invoker
gcloud iam service-accounts keys create /dev/null --iam-account=ssc-build@$P.iam.gserviceaccount.com
gcloud compute networks peerings create t11 --project=$P --network=ssc-cell --peer-project=ssc-c-testcell02 --peer-network=ssc-cell
gcloud compute shared-vpc associated-projects add $P --host-project=ssc-c-testcell02
gcloud run services update ssc-cell-agent --project=$P --region=us-central1 --ingress=all
gcloud compute instances create t11 --project=$P --zone=us-central1-a --subnet=gateway
gcloud sql instances patch ssc-cell --project=$P --assign-ip
gcloud storage buckets create gs://$P-t11 --project=$P --location=europe-west1
```

Then `pulumi destroy --stack c-testcell01`.

`uv run pytest` runs both programs against Pulumi mocks, with no cloud.
