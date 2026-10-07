# infra

Pulumi in Python for SSC on Google Cloud (SSC-013, decisions 021, 022 and 025). This is a separate uv project outside the workspace, so the provider SDK never reaches the services.

| Stack | What it holds |
| --- | --- |
| `platform` | Folders `ssc-cells/{prod,staging}` and `ssc-sandbox`, with logs in `us-central1`. The location policy on `ssc-platform`, the cell policy table on `ssc-cells` and the public-invoker tag (SSC-095). The control projects `ssc-control-<stage>`, their four identities and, for each stage in `control_stages`, the control plane (SSC-064: API, worker, auth host, database, secrets; the public hosts in `public_stage`). The folder rule denying secret reads. Just-in-time staff access. The $250 monthly budget. The public DNS zones `delimitusapps.com.` and `delimitus.com.` in `ssc-platform-0`. |
| `c-<cell label>` | One cell, in two parts. At onboarding: project `ssc-c-<label>` with a $50 budget alert, identities `ssc-gateway`, `ssc-cell-agent`, `ssc-build`, `ssc-data`, `ssc-proxy` and `ssc-secret-intake`, KMS, bucket, Artifact Registry, the VPC with its firewall floor, DNS sinkhole and the empty database zone `ssc-sql`, Cloud NAT with the cell's fixed IP, reserved addresses for the proxy, the data gateway and the database range, the gateway (request-billed, minimum 0, 3600 s requests), the cell agent (one instance at most) with its two log views and its Monitoring read and the secret intake behind the cell's public entry, the project's connection tag `ssc-secret-kind=connection`, and the cell's own deny rule. On first use: whatever the flags below turn on. |

The lazy flags are turned on by the cell deployer when the control plane asks (SSC-087, below), and the deployer sets `warm` both ways when an org admin changes the warm option (SSC-092). The operator can still set any flag by hand.

State lives in `gs://ssc-platform-0-pulumi`, and secrets are encrypted with the KMS key `ssc-platform/pulumi-secrets`. Every call's quota goes to `ssc-platform-0`. The tools refuse any command that names `ristretto-506621`.

Stack files (`Pulumi.<stack>.yaml`) are not committed: each holds a data key that the secret scanner flags. On a fresh clone, `bootstrap` (and `bootstrap cell <label>`) writes them again with the KMS secrets provider and the stack's config.

## Cell flags

Five settings in a cell stack's config, all off by default. Turning one on adds only the resources named here (`naming.LAZY_RESOURCES`); the firewall rules and addresses they use exist from onboarding.

| Flag | Default | Adds | About a month |
| --- | --- | --- | --- |
| `database` | `false` | Cloud SQL Postgres 18 `ssc-cell`, zonal `db-f1-micro`, `max_connections` 25, private address in the database range, a CA-issued certificate for its DNS name and that name's record in `ssc-sql`, the Data API, customer-managed key, backups and point-in-time recovery, the cell agent's IAM database user in `cloudsqlsuperuser`, and `SSC_SQL_INSTANCE` on the agent (App databases, below) | $13 |
| `egress` | `false` | The proxy machine and its health check: one `e2-micro` with no external address at the reserved proxy address, in a group of one that recreates it when the proxy port stops answering (Egress proxy, below) | $7 |
| `connections` | `false` | The data gateway and file broker `ssc-datagw` on Cloud Run, minimum 0, as `ssc-data`, leaving through the cell NAT (Data gateway, below) | usage |
| `gateway_min` | `0` | The gateway's minimum instances | about $10 each |
| `warm` | `false` | Keeps the gateway at one instance or more: the gateway's part of the warm option, which an org admin sets in the console and the cell deployer applies (SSC-092) | about $10 |

```
pulumi config set --stack c-<label> database true
pulumi up --stack c-<label>
```

Other settings: `stage` (`staging` or `prod`), `probe`, `probe_digest`, `agent_image`, `build_tools_image` and `build_frontend_image` (Builds, below), `gateway_image`, `gateway_keyring`, `gateway_jwks` and `org_id` (Gateway, below; `org_id` may also be set alone, and `agent_image` needs it: the agent serves that org only, as `SSC_ORG_ID`, decision 029), `timer_jwks` (Timer calls, under Gateway), `datagw_image` and `datagw_connections` (Data gateway, below), `proxy_image` and `proxy_ha` (Egress proxy, below), `oncall_email` (Alerts and on call, below), `gateway_max` (20) and `billing_account` (defaults to the one SSC account; set it to link a new cell to another account, SSC-089).

The stack exports `flags`, so `cell_diff` compares two cells with different flags without their flagged resources.

**Subnets.** Two IPv4 `/24`s: `apps` (10.20.0.0/24) holds only apps; `gateway` (10.20.4.0/24) holds everything that may reach the internet: the gateway, the data gateway and the proxy. Only `gateway` is behind the NAT, so an app has no route out even if a firewall rule were wrong. To put everything in one `/24`, set `SUBNETS` in `cell.py` to the `apps` entry alone and `EDGE_SUBNET` to `"apps"`; the reserved addresses move with it.

## No-internet floor

Apps have no internet (SSC-027). The rules are written once at onboarding, against addresses reserved then, and no flag changes them: `tests/test_cell.py` checks that the network, firewall, NAT, private zone and response policy are identical on an empty cell and after `database`, then `egress`, then `connections`.

| Egress rule | Priority | Applies to | Destinations |
| --- | --- | --- | --- |
| `egress-internal` | 1000 | everything | 10.20.4.10/32 (proxy), 10.20.4.11/32 (data gateway), 10.21.0.0/20 (database range) |
| `egress-google-private` | 1000 | everything | 199.36.153.8/30 (`private.googleapis.com`) |
| `egress-gateway`, `egress-proxy`, `egress-data` | 1000 | tags `ssc-gateway`, `ssc-proxy`, `ssc-data` | 0.0.0.0/0 |
| `egress-proxy-private` | 900 | tag `ssc-proxy` | 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, denied (SSC-053) |
| `egress-deny-all` | 65534 | everything | 0.0.0.0/0, denied |

Apps carry no tag, so an app reaches the two reserved addresses (a dead end until the proxy or data gateway takes its address), the database range and Google's private APIs, and nothing else: not another app, not the gateway subnet, not another cell. `ingress-proxy` lets the apps subnet reach the proxy on tcp 3128 only. Until SSC-027, `egress-internal` allowed all of 10.20.0.0/16.

**No IPv6.** The VPC has no internal IPv6 range, both subnets and the proxy's interface are `IPV4_ONLY`, and every firewall range is IPv4, so an IPv6 connection has no route.

**DNS.** The response policy `ssc-cell` answers every name under every top-level domain with an unroutable sinkhole. Two lists bypass it: Google's names (`cell.GOOGLE_DNS_PASSTHRU`) and the platform hosts the gateway calls, `naming.GATEWAY_PLATFORM_HOSTS`: `auth.delimitus.com` (login) and `keys.delimitus.com` (identity note issuer and keys). Each platform rule is the exact name, so `x.auth.delimitus.com` still gets the sinkhole and a query can carry data only in those two fixed names. To add a host, add it to that tuple; it adds one rule. A third bypass, `*.sql-psa.goog.`, hands Cloud SQL's names to the cell's private zone `ssc-sql` (`sql-psa.goog.`), which answers every one of them itself: the database's name with its private address once the flag is on, any other name with NXDOMAIN, so none goes to public DNS (App databases, below).

**Why apps stay blocked.** A response policy belongs to the whole VPC: Cloud DNS cannot answer the apps subnet differently from the gateway subnet, so an app resolves the two platform hosts too. Resolving is not reaching. Their addresses are public, no allow rule names them for an untagged source, so `egress-deny-all` drops the packet; and the apps subnet is not behind the NAT, so there would be no route out even if a rule were wrong. The probe checks it: the probe runner job sets `PROBE_EGRESS_HOSTS` to the two hosts, and `no_direct_egress` dials each on 443 from the app, as well as its five fixed attempts (tcp 443 and 80, udp 53 and 443, IPv6).

Keep `auth` and `keys` as A records. The platform stack writes both, with `api`, to the control plane's entry address (Control plane, below); a CNAME to a name outside the bypass lists may be answered with the sinkhole.

**The proxy's floor (SSC-053).** The proxy may reach only public addresses: `egress-proxy-private` denies it every private range, so an allowed host that resolves into the cell (the gateway subnet, the database range) gets nowhere. It still reaches `private.googleapis.com`, which it reads its snapshot through, and Google's names, `pkg.dev.` and `*.pkg.dev.` included, bypass the sinkhole so it can pull its image from Artifact Registry over the NAT. The firewall never sees traffic to the metadata server or the machine's own loopback, so the machine refuses tunnel traffic (tcp 443) to 169.254.0.0/16 and 127.0.0.0/8 itself (`cell.PROXY_HOST_RANGES`). An app resolves `pkg.dev` too; resolving is not reaching.

**Live check** (operator, not run by SSC-027). On a staging probe cell with the nightly's settings (SSC-017: stack settings `probe`, `probe_digest` and `agent_image`; environment `SSC_PROBE_PROJECT`, `SSC_PROBE_AGENT_URL` and `SSC_PROBE_DIGEST`; the nightly workflow gets the first two from `SSC_NIGHT_CELLS`), all flags off:

```
P=ssc-c-testcell01
pulumi up --stack c-testcell01
gcloud compute firewall-rules list --project=$P --format=json > /tmp/fw-empty.json
gcloud dns response-policies rules list ssc-cell --project=$P --format=json > /tmp/dns-empty.json
uv run python -m ssc_conformance.nightly            # no_direct_egress passed, 7 connections refused
for flag in database egress connections; do
  pulumi config set --stack c-testcell01 $flag true
  pulumi up --stack c-testcell01
  gcloud compute firewall-rules list --project=$P --format=json | diff /tmp/fw-empty.json -
  gcloud dns response-policies rules list ssc-cell --project=$P --format=json | diff /tmp/dns-empty.json -
  uv run python -m ssc_conformance.nightly
done
```

Each `diff` prints nothing, and `no_direct_egress` passes every time. The sinkhole's first creation took about 78 minutes with its state in the bucket; onboarding now keeps the state in a local folder until its last step (SSC-091, below).

## The cell deployer

The control plane turns on a cell's `database`, `egress` or `connections` flag without a person and without new powers of its own (SSC-087, decision 022 amendment pending). NAT and the fixed IP are not lazy.

- **Trigger.** In the control plane, the job `cell:create_resource` (`ssc_control/cell/`), one per cell and resource. It is asked for by the first deploy whose manifest has `[state] postgres = true` (database), the first approved internet host (egress), the first approved data source (connections), or an org admin (`POST /v1/cell/resources/{resource}/enable`, audited). A second request joins the one in flight. Each request, success and failure is an audit row (`cell.resource_requested`, `cell.resource_ready`, `cell.resource_failed`). Nothing turns a flag off: removing a resource is a runbook step.
- **Runner.** The Cloud Run job `ssc-cell-deployer` in `ssc-platform-0`, image `infra/deployer/Dockerfile`, running `python -I -m ssc_infra.deployer <cell label> <database|egress|connections|warm=true|warm=false>` as `ssc-cell-deployer@ssc-platform-0.iam.gserviceaccount.com`. It reads the config the stack was last applied with (the stack's `config` output), sets the one flag to `true` (or, for `warm=true` and `warm=false`, the `warm` flag to that value; `naming.WARM_ARGS`) and runs `pulumi up` on that one stack. It refuses any third argument, any flag outside those five, a malformed label and any environment variable it does not expect, and gives Pulumi an environment of its own. A run killed halfway is converged by the next one: a state lock older than 3 hours is cancelled, and a create Pulumi never saw finish is imported or dropped.
- **Deployer roles.** On `ssc-cells`: `cloudsql.admin`, `compute.instanceAdmin.v1`, `compute.networkUser`, `iam.serviceAccountUser`, `resourcemanager.tagUser`, `run.admin`. In `ssc-platform-0`: `storage.objectAdmin` on the state bucket, `cloudkms.cryptoKeyEncrypterDecrypter` on the secrets key, `serviceusage.serviceUsageConsumer`. Also the public-invoker tag (above), the apps zone's record writer, and in each cell the custom role `sscDeployerRecords` (record permissions only) on the zone `ssc-sql` alone, which the cell stack grants at onboarding so the `database` flag can write the database's record. No billing, project-creation, deny-rule, IAM or secret-value role.
- **The control plane's part.** The worker, which runs the cell jobs, holds `run.jobsExecutorWithOverrides` and `run.viewer` on that one job, nothing more: `ssc-control-worker` of each control project (SSC-064; it was `ssc-control` before). It starts a run with exactly two arguments, a cell label and a resource or warm setting, and reads how the run went. Worker settings: `SSC_CELL_DEPLOYER=cloud_run` and `SSC_CELL_DEPLOYER_JOB=projects/ssc-platform-0/locations/us-central1/jobs/ssc-cell-deployer` (unset means none, and a lazy resource fails with `CELL_DEPLOYER_UNAVAILABLE`; `fake` only in `dev` and `test`).
- **The operator's access** is unchanged.

**Why this keeps the control plane without a path to app data.** The control plane gains no role in any cell. It cannot choose what the deployer applies, only which cell and which of three flags, or whether the gateway is warm, and the deployer's code, image and identity are not the control plane's to change. The flag turns on the same resources the operator would, from the same program, so a compromised control plane can at worst create a database, a proxy or a data gateway that a cell was going to have anyway, or keep a gateway at one instance (about $10 a month), and pay for it.

**What it does not protect against.**
- The deployer itself. It can read and decrypt every stack's state, the platform's included, and with `run.admin` and `iam.serviceAccountUser` on the cells folder it can deploy code as any identity in any cell, which reaches app data. Whoever can change its image, its job or its identity's roles holds that power; today that is the operator.
- The image's supply chain: Pulumi, the provider plugin and the Python dependencies inside it run with the deployer's rights.
- Repeated or unwanted runs. The control plane may start the job for any cell and flag as often as it likes; the cost is bounded by the three resources per cell (about $20 a month) and by the budget alert.
- A bug in the cell program. The deployer applies whatever `infra/` says, with only the one flag changed.

**Live steps** (operator, not run by SSC-087):
1. `pulumi up --stack platform`: the identity, its roles and the registry.
2. Build and push `infra/deployer/Dockerfile` (from the repository root) to `us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform/ssc-cell-deployer`, then `pulumi config set --stack platform deployer_image <image@digest>` and `pulumi up --stack platform` again: the job and the control plane's grants.
3. Re-apply each existing cell once (`pulumi up --stack c-<label>`) so its state exports `config`.
4. The platform stack sets the two worker settings above on the control plane's worker once `deployer_image` is set (SSC-064).
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

## Secrets

App secret values enter a cell through one service, the secret intake (SSC-026, decision 022 amendment pending), and never pass the control plane:

- **Service.** `ssc-secret-intake` on Cloud Run, minimum 0, at most 3, request-billed, as `ssc-secret-intake`. With `agent_image` set it runs that image with the command `python -m ssc_agent.intake` and `SSC_CELL_PROJECT`, `SSC_INTAKE_ORIGIN` (`https://ssc--secrets.<label>.delimitusapps.com`) and `SSC_CONTROL_SA` (the stage's control plane account); without it, the placeholder with no environment.
- **Door.** Its own serverless NEG and backend `ssc-secret-intake` on the cell's load balancer, for its reserved host alone. Ingress internal and load balancer; invoker `allUsers` under the public-invoker tag, because the CLI calls it with the control plane's write grant, which the intake checks itself. No custom audience: no ID token is checked by Cloud Run.
- **Network.** No VPC egress, like the agent: it reaches `www.googleapis.com` (Google's certificates) and Secret Manager on Google's own path, not through the apps' network, so the no-internet floor neither applies to it nor changes.
- **Identity.** `ssc-secret-intake` holds `secretmanager.secretVersionAdder` on `ssc-a-*` and `ssc-conn-*` secrets (IAM condition; `ssc-conn-*` holds connection credentials, Data gateway below) and `logging.logWriter`, nothing else, and is named in the cell's deny rule, so it cannot read a version back.
- **Control plane.** Its `secretVersionAdder` in the cell (`control-secret-versions`) is removed; it holds no project role in a cell now.
- **Agent.** `secretmanager.admin` on `ssc-a-*` and `ssc-conn-*`: it creates each secret, sets its policy, adds the database's versions (SSC-040) and deletes the secret of a database it drops (SSC-042). For a connection secret it binds the connection tag in the create call and sets `ssc-data` as the only reader (Data gateway, below). The deny rule refuses it every value.

**Live checks** (operator, not run by this change), on a staging cell with `agent_image` set:
1. `curl https://ssc--secrets.<label>.delimitusapps.com/healthz` answers `ok`; the service's `run.app` host refuses. The folder policy accepts the `allUsers` grant on the intake with the existing tag value.
2. `ssc secret set` adds a version; the intake's account reading it back (`gcloud secrets versions access` impersonating `ssc-secret-intake`) is refused, and so is a create.
3. The control plane's account adding a version in the cell is refused.
4. The condition `resource.name.extract("/secrets/{name}")` matches the resource name `:addVersion` is checked against, as it does for the agent's `secretmanager.admin`.
5. `deny_probe` does not list identities, so check the intake and the control plane in the deny rule with `gcloud iam policies get ssc-deny-secret-read --attachment-point=cloudresourcemanager.googleapis.com/projects/<project> --kind=denypolicies`.

## App databases

With the `database` flag the agent makes one database per app on the cell's instance (SSC-040, `ssc_agent.cloud_sql`, `ssc_agent.app_database`):

- **Agent environment.** `SSC_SQL_INSTANCE=ssc-cell`, the instance's name (the agent builds `projects/<project>/instances/<name>` itself), only with the flag on and `agent_image` set; unset, the agent refuses databases. `cell_diff` hides only that variable on the agent when the stacks' `database` flags differ.
- **Agent permissions.** Granted at onboarding, since the deployer holds no IAM role: the custom role `sscCellAgentDatabase`, one permission per call `ssc_agent.cloud_sql` makes: `cloudsql.instances.executeSql` (`executeSql`), `cloudsql.instances.login` (its `autoIamAuthn`), `cloudsql.instances.get` (the instance, and the `operations` it polls after `databases.insert` and `databases.delete`), `cloudsql.instances.listServerCas`, `cloudsql.databases.create` (`databases.insert`, moved here from `sscCellAgentCreate`) and `cloudsql.databases.delete` (`databases.delete`, SSC-042). They replace `cloudsql.admin`, `cloudsql.client` and `cloudsql.instanceUser`. The agent has no network path to the instance and needs none.
- **Database user.** `ssc-cell-agent@ssc-c-<label>.iam`, type `CLOUD_IAM_SERVICE_ACCOUNT`, in `cloudsqlsuperuser`; the instance has `cloudsql.iam_authentication=on`. Statements run through the Data API (`dataApiAccess: ALLOW_DATA_API`) with `autoIamAuthn`.
- **Connections.** `max_connections=25` on `db-f1-micro` (`SQL_TIER`), which the agent reads for its ceiling.
- **Name and certificate.** `serverCaMode: GOOGLE_MANAGED_CAS_CA`, so the server certificate names the instance's DNS name and apps can use `sslmode=verify-full` against `listServerCas`. The name is under `sql-psa.goog.`, which the `*.goog.` rule would sinkhole, so at onboarding the cell gets the private zone `ssc-sql` and the bypass `*.sql-psa.goog.` (both in the floor, the same with every flag), and the flag adds the record `sql-dns`: the instance's private services access name (in `dnsNames`; Cloud SQL leaves `dnsName` empty for such an instance, found live 2026-10-04) as A to its private address, in the database range apps may already reach. Nothing else opens.
- **`cell_diff`.** The instance's DNS name and private address are cloud-assigned, so they become `<sql-dns>` and `<sql-address>` wherever they appear, as the entry address does.

**Live checks** (operator, not run by this change), on a staging cell with `database` on:
1. Custom roles may hold `cloudsql.instances.executeSql` and `cloudsql.instances.listServerCas`, and `executeSql` with `autoIamAuthn` needs no more than the permissions above (in particular not `cloudsql.instances.connect`).
2. `operations.get` on the `databases.insert` and `databases.delete` operations needs only `cloudsql.instances.get`, and a database drop succeeds with `cloudsql.databases.delete`.
3. `executeSql` runs on this private-IP-only instance with the Data API allowed.
4. With `GOOGLE_MANAGED_CAS_CA` the instance has a private services access name in `dnsNames` (`gcloud sql instances describe ssc-cell --format='value(dnsName,dnsNames)'`) and its certificate names it; Cloud SQL does not publish that name itself for private services access (if it does, the record here only repeats it).
5. From an app: the name resolves to the instance's private address, another `*.sql-psa.goog` name gets NXDOMAIN, and `psql "sslmode=verify-full"` with `DATABASE_CA` connects.
6. The deployer's flag run writes `sql-dns` through its zone-level grant.
7. Cloud SQL accepts `max_connections=25` on `db-f1-micro`, and `superuser_reserved_connections + reserved_connections` is 3, which gives the agent's ceiling of 10 app databases.
8. The agent's IAM user is in `cloudsqlsuperuser` (`gcloud sql users list --instance=ssc-cell`) and `SET ROLE cloudsqlsuperuser` works through the Data API.
9. CMEK and the CAS server CA mode together are accepted on a new instance.

## App logs

The cell agent reads app, build and health logs from the cell's own Cloud Logging (SSC-024, `ssc_agent.cloud_logging`), through two log views and nothing else:

- **Views.** `ssc-run` (`resource.type = "cloud_run_revision"`) and `ssc-build` (`resource.type = "build"`) on the project's `_Default` bucket, `projects/ssc-c-<label>/locations/us-central1/buckets/_Default/views/<view>`. Two views, because a view's filter is documented as an `AND` of comparisons only. The agent's `SSC_LOG_VIEW` holds both full names, comma-separated.
- **Bucket location.** `_Default` is made with the project, in the default log location of its folder. The platform stack sets that to `us-central1` on `ssc-cells` and on each stage folder (`FolderSettings`), and the location policy allows it, so a cell's `_Default` is in `us-central1`. A bucket's location never changes: a cell project made before that setting would have a `global` `_Default`, and its views would need `locations/global` in `cell.log_bucket`, or a new project; live check 1 tells which.
- **Identity.** `ssc-cell-agent` holds `logging.viewAccessor` on the project with the condition `resource.name == "<ssc-run>" || resource.name == "<ssc-build>"`, beside its `logging.logWriter`; no `logging.viewer`, no `privateLogViewer`, nothing on the bucket (`tests/test_cell.py`).
- **One instance.** The agent keeps under Cloud Logging's 60 `entries.list` calls a minute per project only within one instance, so the agent service runs at most 1 instance (it was 3). Concurrency 200, for the 40 follows it allows at once (each held up to 20 s) beside deploys, which hold a call for up to 240 s; request timeout 300 s. The secret intake keeps its own 3.
- **Network.** The agent has no VPC egress, so it reaches `logging.googleapis.com` on Google's own path, as it reaches Cloud Run and Secret Manager; the Logging API is already on in the cell. No DNS or Private Google Access change.
- **Cost.** None for an empty cell: views and IAM are free, Logging does not bill reads, and one instance at most can only cost less.

**Live checks** (operator, not run by this change), on a staging cell with `agent_image` set and one app deployed:
1. `gcloud logging buckets describe _Default --location=us-central1 --project=ssc-c-<label>` answers (and `--location=global` does not); `gcloud logging views list --bucket=_Default --location=us-central1` shows both views with their filters.
2. Following the app (`GET /v1/apps/<app>/environments/<env>/logs?source=app` with `after=<cursor>&wait=20`, until `ssc logs` lands in SSC-022) shows a line the app prints within 5 s of printing it, on a real cell; note the ingestion delay seen (the agent reads 10 s back, `LAG`, and polls every 2 s).
3. `viewAccessor` on the two views alone is enough for `entries.list` with both views in `resourceNames`: no `403`, and the same call naming the bucket itself (`projects/<p>/locations/us-central1/buckets/_Default`) is refused.
4. Whether one view with `resource.type = "cloud_run_revision" OR resource.type = "build"` is accepted: `gcloud logging views create ssc-try --bucket=_Default --location=us-central1 --log-filter=...`, then delete it. If it is, two views stay anyway (no change needed).
5. `source=build` shows a build's lines through `ssc-build`, and `GET .../health` shows `running` or `asleep` without waking the app.
6. The agent service shows max instances 1, concurrency 200 and timeout 300 s (`gcloud run services describe ssc-cell-agent`).

## App usage

The cell agent reads Cloud Run's own metrics for every `ssc-a-` service from the cell's Cloud Monitoring (SSC-028, `ssc_agent.cloud_monitoring`): three `timeSeries.list` calls an hour, for `container/billable_instance_time`, `container/instance_count` (`state = active`) and `container/startup_latencies`.

- **API.** `monitoring.googleapis.com` is on in every cell, with the other APIs at onboarding.
- **Identity.** The custom role `sscCellAgentUsage` holds `monitoring.timeSeries.list` alone, granted to `ssc-cell-agent` on the project; no `monitoring.viewer` or any other Monitoring role (`tests/test_cell.py`).
- **Agent environment.** `SSC_USAGE_SOURCE=monitoring` with `agent_image` set; unset, the agent refuses usage reads and the control plane records no usage events.
- **Network.** The agent has no VPC egress, so it reaches `monitoring.googleapis.com` on Google's own path, as it reaches Logging, Cloud Run and Cloud SQL Admin. The cell's DNS policy and Private Google Access apply only to traffic in the VPC, so neither changes.
- **Cost.** Cloud Run's metrics are free, and about 2,200 read calls a month stay inside Monitoring's free read allowance.

**Live checks** (operator, not run by this change), on a staging cell with an `agent_image` built from SSC-028 and one Streamlit app deployed:
1. The custom role is enough: the agent's usage read answers with no `403`, and `gcloud projects get-iam-policy ssc-c-<label>` shows `ssc-cell-agent` with `sscCellAgentUsage` and no `roles/monitoring.*`.
2. Data within 15 minutes: open the app, then read the agent's usage for the current hour; `billable_instance_time` and `startup_latencies` show the service within 15 minutes of the first request. Note the delay seen.
3. `instance_count` stays `active` while a WebSocket is open: keep the Streamlit page open with no clicks for 20 minutes, then read `instance_count` with `state = active` for that window (Metrics Explorer, or the agent's busy minutes). Expected: every minute of the window counts as active. If the idle minutes show as `idle`, session hours undercount open pages, and SSC-028's busy-minute rule needs another source.

## Gateway

The cell's gateway (SSC-018, decision 023) runs a build of `packages/ssc_edge/Dockerfile` once its four settings are set; until then it is the placeholder image with no environment.

- **Settings.** All four or none (`org_id` alone is allowed too); anything else fails the stack before apply.
  - `gateway_image`: `us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform/<image>@sha256:<digest>`.
  - `org_id`: the cell's customer, `org_` and 20 lowercase letters or digits.
  - `gateway_keyring`: the keyring sealed with the cell's `gateway` key, in base64. It is ciphertext, so it is an ordinary setting; the stack refuses a value that decodes to JSON, which would be the plain keyring.
  - `gateway_jwks`: the keyring's public JWKS. The stack refuses a key with a private member.
- **Key.** `gateway` in the ring `ssc-cell`, rotated every 90 days like the others. `cryptoKeyDecrypter` for `ssc-gateway` alone, and the gateway waits for that grant; `cryptoKeyEncrypter` for the operator, to seal. The stack exports its name as `gateway_kms_key`.
- **Image.** The cell's Cloud Run service agent gets `artifactregistry.reader` on `ssc-platform` in `ssc-platform-0`. Like the build grant, the operator's run writes it.
- **Environment.** `SSC_CELL_LABEL`, `SSC_ORG_ID`, `SSC_PROJECT_NUMBER`, `SSC_REGION`, `SSC_CELL_BUCKET`, `SSC_GATEWAY_KEYRING`, `SSC_GATEWAY_KMS_KEY`, `SSC_IDENTITY_JWKS`, `SSC_APPS_DOMAIN`, `SSC_AUTH_URL` (`https://auth.delimitus.com`) and `SSC_IDENTITY_ISSUER` (`https://keys.delimitus.com/<label>`).
- **Start.** The gateway decrypts its keyring, refuses to start if it does not match `SSC_IDENTITY_JWKS`, and reads the snapshot before it takes a request; with no readable snapshot it answers `503` to everything.
- **Identity keys.** With the settings, the stack exports `identity_jwks`. Apps have no internet, so each app gets it inline as `SSC_IDENTITY_KEYS_URL=data:application/json;base64,<JWKS>` (`docs/contracts/identity-note.md`).

**Live steps** (operator, not run by SSC-018). The plain keyring stays on the operator's machine for these lines only:

```
REG=us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform
docker buildx build --platform linux/amd64 --provenance=false --metadata-file /tmp/gateway.json \
  -f packages/ssc_edge/Dockerfile --tag $REG/ssc-gateway:<commit> --push .
jq -r '."containerimage.digest"' /tmp/gateway.json
pulumi up --stack c-<label>
KEY=$(pulumi stack output --stack c-<label> gateway_kms_key)
umask 077
uv run python -m ssc_edge.keys new > keyring.json
uv run python -m ssc_edge.keys jwks < keyring.json > jwks.json
gcloud kms encrypt --key "$KEY" --plaintext-file keyring.json --ciphertext-file - \
  | base64 | tr -d '\n' > keyring.sealed
rm keyring.json
pulumi config set --stack c-<label> gateway_image $REG/ssc-gateway@<digest>
pulumi config set --stack c-<label> org_id <org id>
pulumi config set --stack c-<label> gateway_keyring "$(cat keyring.sealed)"
pulumi config set --stack c-<label> gateway_jwks "$(cat jwks.json)"
pulumi up --stack c-<label>
```

The first `pulumi up` creates the key on a cell made before it. Still live-only: Cloud Run accepting the gateway's ID token for an app's `run.app` URL (T3), the cold start (T7), the first drill against a cold gateway (T8, below), the browser suite (SSC-029), and the `auth` and `keys` records in the `delimitus` zone, which exist once the control plane's public stage is applied (Control plane, below).

**Removing access (SSC-021, decision 023 amendment).** An allowed WebSocket or event stream goes through the gateway's stream relay (loopback port 9002, `SSC_STREAM_PORT`). While any stream is open, the relay re-reads the snapshot every second and closes each stream its person may no longer open. Nothing in the stack changes. Live checks for the proof run, on a staging cell with a Streamlit app shared with one test person through a group, then by a direct grant:

1. Grant removal, awake gateway. Keep a page polling the app once a second, then remove the grant (`PUT .../grants`). Note when `snapshots/<org>/latest.json` names the new version (`gcloud storage objects describe`, `update_time`) and when the first `404` arrives. Expected: the `404` arrives at most 2 s plus the bucket read after the pointer moves, under 5 s in total.
2. Time from the grant change to the pointer. Measure from the `PUT` returning to `latest.json` moving: this is the worker's compile, which is not measured anywhere yet. Expected: a few seconds. If it is longer, the 5 s done-when does not hold from the person's side.
3. Open stream. With the Streamlit page open, remove the grant. Expected: the websocket closes (browser devtools, Network, WS) about 3 s after the pointer moves, and Streamlit shows its "connection lost" state. The cell's logs show `stream watch closed 1 stream(s)`.
4. Group removal. Remove the person from the group in the directory. Expected: after the next sync tick (60 s) and the compile, the page gets `404` and the open websocket closes, as in 1 and 3.
5. Gateway at zero. Let the gateway scale to zero, remove a grant, then open the app. Expected: the first answer is `404`.
6. The relay's path. Open a websocket through the load balancer and confirm it reaches the app with `X-Serverless-Authorization` accepted. This checks TLS from the relay to `run.app` with the image's CA bundle (`/etc/ssl/certs/ca-certificates.crt`), the ID token, and WebSockets through Envoy to the relay behind the serverless NEG. Also confirm that server-sent events still arrive unbuffered.

**Kill switch (SSC-025, decision 014 amendment).** The kill goes through the snapshot, so nothing in the stack changes. The saga's `gateway_deny` step is `done` once `latest.json` names its version, and no cell process reports back. Each `kill_switch.step` audit row has `since_command_ms`; the last row's figure is the drill's time. Live checks for the proof run (T8), on a staging cell with a Streamlit app open in a browser:

1. First drill, gateway cold. Let the gateway and the app scale to zero, run `ssc disable <app>` and note when the command returns. Expected: `gateway_deny` is `done`, not `unconfirmed`, with `since_command_ms` under 10,000. Then open the app: the first answer is `404`, and the app's service logs no request and no new instance.
2. Awake gateway. Keep a page polling the app once a second with the Streamlit page open, then disable it. Expected: the first `404` arrives at most 3.3 s after `latest.json` moves (when the gateway last read it, plus its 0.3 s wait), and under 10 s after the command. The websocket closes about 3 s after the pointer moves.
3. Time from the command to the pointer. Compare the `disable` response with the `update_time` of `latest.json`: this is the worker's compile, the term no local test measures.
4. Full stop. Expected: `scale_to_zero` is `done` and the last step's `since_command_ms` is under 60,000. Cloud Run's settle time for manual scaling to 0 sets this bound (the agent waits up to 180 s), and it is known only from this run. `gcloud run services describe` shows the manual instance count at 0.
5. Enable. Run `ssc enable <app>`. Expected: the next request after the following compile is admitted, and the app starts from zero.

**Kill switch drill (SSC-054).** The published numbers come from `python -m ssc_conformance.kill_drill` (`docs/kill-switch-drill.md`, which has the setup and the settings), not from a stack change. Live steps (operator, not run by SSC-054), on a staging cell with `connections` and `egress` on, the drill app (`conformance/kill_drill_app`) deployed with its connection `drill-db` and `api.github.com` allowed:

1. One run each. `SSC_DRILL_RUNS=1`: the awake run shows the query and the tunnel ended, with `running` true in their log lines, and the asleep run prints "nothing started" with the gateway's own request line found. Expected: the proxy's log lists the environment's credential in the awake run (the asleep proof relies on it), and a Cloud Logging read of the drill app's `jsonPayload.drill` lines works for the caller.
2. Ten runs in each state, then paste the table into `docs/kill-switch-drill.md` with the date, commit and cell. Expected: front door under 10 s, everything under 60 s, both states. A fail goes back to the architecture document; it is not worked around.
3. Names. Confirm the bucket is `<project>-cell` and the data gateway and gateway are `ssc-datagw` and `ssc-gateway` (the drill's log filters use these names), and that the request-log name `run.googleapis.com/requests` returns lines for the app.
4. Workflow. Trust `kill-drill.yml` in the WIF binding (done: the GitHub provider trusts `nightly.yml` and `kill-drill.yml` on main, and the nightly account reads `snapshots/` and logs in probe cells, so use a probe cell and `ssc-nightly` as `SSC_DRILL_SERVICE_ACCOUNT`), put the cell and its `drill` object in `SSC_NIGHT_CELLS` and set the variables and secrets named at the top of the workflow, run it once from the Actions tab. `nightly.yml` calls it each night with one run per state (SSC-056); the workflow's own schedule stays commented out. For the nightly's least-privilege and organisation-policy checks (SSC-056) the platform stack binds `ssc-nightly` on the `ssc-cells` folder to `roles/iam.securityReviewer`, `roles/iam.viewer` (each deny policy's rules; `roles/iam.denyReviewer` is organisation-only) and `roles/orgpolicy.policyViewer` (`cells-nightly-*`), and each probe cell's probe job to `roles/run.jobsExecutorWithOverrides`, which the probe runner's overrides need. Re-apply the platform stack and the probe cell's stack before the first night.

**Timer calls (SSC-041, decision 023 amendment).** A timer run reaches its app through the cell's load balancer and gateway, as a browser does, with a schedule token signed by the control-plane worker. The cell's `timer_jwks` setting is the worker key's public JWKS, one or two named public P-256 keys, the same for every cell; the stack refuses anything else and passes it to the gateway as `SSC_TIMER_JWKS`. Unset, the gateway refuses every timer call with its `404`. Live steps (operator, not run by SSC-041):

```
umask 077
uv run python -m ssc_control.timers.https new --out timer-key.pem --kid timer-<yyyymm> > timer-jwks.json
pulumi config set --stack c-<label> timer_jwks "$(cat timer-jwks.json)"
pulumi up --stack c-<label>
```

The PEM goes into the worker's `SSC_TIMER_SIGNING_KEY` secret, then is removed locally; the platform stack's `timer_key_id` makes that secret and gives the worker `SSC_TIMER_KEY_ID` and `SSC_TIMER_DISPATCHER=https` (Control plane, below). `docs/runbooks/ssc-064-control-plane.md` (step 7) does this without writing the PEM to a file. To rotate, set `timer_jwks` to both keys on every cell, move the worker to the new key, then drop the old one. Live check for the proof run: with the gateway and an app with a one-minute schedule at zero, wait for a run. Expected: `GET .../schedules/{id}/runs` shows it `succeeded` with `start_ms` the cold start and `duration_ms` the call alone, and the app's logs show a `GET` of its `health_path` and then the call, both with a note whose `role` is `schedule`.

## Egress proxy

The `egress` flag's machine (SSC-053) runs `packages/ssc_egress/Dockerfile` once `proxy_image` is set: Envoy as an explicit `CONNECT` proxy on tcp 3128 at 10.20.4.10, refusing every host the org's allowlist does not name, and leaving through the cell NAT, so a partner sees the cell's fixed IP. There is no load balancer in front of it. The control plane turns the flag on with the org's first allowed host (The cell deployer, above), and the NAT and fixed IP exist from onboarding, so the first host needs no person. The contract is `docs/contracts/access-snapshot.md` (`egress`).

- **Setting.** `proxy_image`: `us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform/<image>@sha256:<digest>`, and only with the four gateway settings, whose `org_id` it reads; anything else fails the stack before apply. Without it the machine boots with no proxy and its health check fails.
- **Machine.** Container-Optimized OS (`cos-stable`, automatic updates off), Shielded VM, OS Login, no project SSH keys and no external address. Its `user-data` writes the systemd unit `ssc-egress.service`, which configures Docker for the platform registry with the machine's identity and runs the image with `--network host --read-only --cap-drop ALL --security-opt no-new-privileges`, `SSC_ORG_ID` and `SSC_CELL_BUCKET`. The unit restarts the container 2 s after any exit and never gives up; when Envoy exits, the container exits. The machine is patched by replacing it: the template takes a new name (`ssc-proxy-<suffix>`) on every change, so a new image or boot image recreates the machine.
- **Identity.** `ssc-proxy`, made at onboarding because the cell deployer holds no IAM role: `storage.objectViewer` on the cell bucket under `snapshots/` only (`bucket-proxy`), reader on the platform repository (`registry-proxy-image`), `logging.logWriter`, and named in the cell's deny rule. It reads the org's snapshot every few seconds; nothing else.
- **Healing.** The health check `ssc-proxy` opens tcp 3128 every 10 s; three failures in a row and the group recreates the machine, after a 300 s grace for a new one to boot.
- **High availability.** `proxy_ha` (`false`): two machines in two zones (`us-central1-a`, `-b`), each replaced only once its successor exists, behind an internal passthrough load balancer (`ssc-proxy`, tcp 3128) that holds 10.20.4.10. It replaces the single machine; `cell_diff` treats the five resources it changes (`naming.PROXY_HA_RESOURCES`) as the flag's.
- **Agent.** The cell agent gets `SSC_PROXY_ADDRESS` (10.20.4.10) and `SSC_OUTBOUND_IP` (the NAT's fixed address) on every cell, so it can write `HTTPS_PROXY` for an app environment and the console can show the fixed IP.

**Live checks** (operator, not run by SSC-053), on a staging cell with the gateway settings, `proxy_image` built from this commit, and an app whose `ssc.toml` lists `api.github.com` under `[egress] hosts`, the org's allowlist holding `*.github.com`:

1. First host. On a cell with `egress` off, add the first host as an org admin. Expected: `cell.resource_requested` then `cell.resource_ready` for `egress` with no person, the machine `ssc-proxy-*` running, and `GET /v1/egress` showing `outbound_ip` equal to the stack's `nat_ip`.
2. T6 (SSC-086) outbound. From the app, `curl https://api.ipify.org` with the host listed: the answer is `nat_ip`. Unlisted: `curl https://example.com` fails with `403` and a body that names `example.com:443` and says how to request it.
3. Raw IP. `curl https://1.1.1.1` and `curl --resolve x.github.com:443:1.1.1.1 https://1.1.1.1` both fail with `403`; `curl -6`, a UDP 443 send and plain `http://` (`405`) fail too.
4. Drain. Open a long tunnel (`openssl s_client -proxy 10.20.4.10:3128 -connect api.github.com:443` held open), remove `*.github.com` from the allowlist, and time how long the tunnel stays up after the next snapshot: under 5 s plus one poll.
5. Kill. `sudo pkill envoy` on the machine (OS Login through IAP): the unit restarts the container and the port answers again; record the seconds. Then stop the machine's Docker daemon: the health check fails and the group recreates the machine; record the minutes to a healthy instance.
6. Image pull. `journalctl -u ssc-egress` shows the pull from `us-central1-docker.pkg.dev` succeeding through the NAT, and no tunnel to `169.254.169.254:443` succeeds.

## Data gateway

The `connections` flag's `ssc-datagw` (SSC-050, C19) runs a build of `packages/ssc_datagw/Dockerfile` once `datagw_image` is set; until then it is the placeholder image with no environment. The contract is `docs/contracts/data-gateway.md`.

- **Setting.** `datagw_image`: `us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform/<image>@sha256:<digest>`, and only with the four gateway settings, whose `org_id` and `gateway_jwks` it reads too; anything else fails the stack before apply.
- **Callers.** Ingress is internal only, so only the cell's VPC reaches it. Cloud Run's invoker check is off on this service alone (`invoker_iam_disabled`): an app sends its own Google ID token (audience `SSC_DATAGW_AUDIENCE`) in `Authorization`, and the data gateway admits only Google-signed tokens for that audience from an `ssc-a-<env>` account of this cell's project. No app holds `run.invoker`, and there is no `allUsers` grant and no public-invoker tag (`tests/test_cell.py`).
- **Snapshot.** `ssc-data` holds `storage.objectViewer` on the cell bucket under an IAM condition, objects under `snapshots/` only (`bucket-data`, written at onboarding so the flag still adds the service alone). It reads the snapshot before its first request and again on demand; nothing runs between requests.
- **Environment.** `SSC_ORG_ID`, `SSC_CELL_LABEL`, `SSC_PROJECT_ID`, `SSC_CELL_BUCKET`, `SSC_DATAGW_AUDIENCE` (`https://ssc-datagw-<project number>.us-central1.run.app`), `SSC_IDENTITY_JWKS`, `SSC_APPS_DOMAIN` and `SSC_IDENTITY_ISSUER`.
- **Connections (SSC-051).** The Postgres connector reads each connection from `SSC_CONNECTION_CON_<20>` (JSON `{host, port, database, user, password, ca}`; the contract's "The Postgres connector"). The cell holds the credentials; they reach the service only as pinned secret variables:
  - **Secret.** `ssc-conn-<20>` (the connection id's 20 characters), its value written through the secret intake like an app secret (Secrets, above). The agent creates it with the project's tag `ssc-secret-kind=connection` bound in the same call and sets its policy to `secretAccessor` for `ssc-data` alone. The agent reads `SSC_DATA_SA` and `SSC_CONNECTION_TAG` (`tagKeys/<n>=tagValues/<n>`) for this; without them it refuses connection secrets. The tag key and value are the cell project's own, so no organisation tag permission is involved, and only the agent is granted `resourcemanager.tagUser` on the value in the stack.
  - **Deny rule.** `ssc-deny-secret-read` has two rules. The first refuses `secretmanager.versions.access` to `ssc-gateway`, `ssc-cell-agent`, `ssc-build`, `ssc-proxy`, `ssc-secret-intake` and the deny probe, with no condition. The second refuses it to `ssc-data` alone, with the denial condition `!resource.matchTagId('tagKeys/<n>', 'tagValues/<n>')`: every secret without the connection tag stays denied to `ssc-data` whatever is granted, so no grant can give it an app secret. `ssc-data` holds no Secret Manager role on the project (`tests/test_cell.py`). IAM deny conditions accept only the resource tag functions, `!` included (Google's "Deny access" and "Deny policies" pages), and a tag bound to a secret is inherited by its versions ("Tags overview"); Google does not document `matchTagId` on `secretmanager.versions.access` itself, so live checks 9 and 10 settle it. Tags are inherited, so the tag bound to the project would exempt every secret for `ssc-data`: the agent's `tagUser` is on the tag value only and it holds no tag-binding permission on the project; the cell deployer's folder `tagUser` would allow it, but the deployer holds no Secret Manager role, so `ssc-data` would still need a grant on the app secret from the agent.
  - **Setting.** `datagw_connections`: `con_<20>:<version>`, separated by commas, each version a number (never `latest`), and only with `datagw_image`. Each becomes `SSC_CONNECTION_CON_<20>` from `ssc-conn-<20>` at that version (Cloud Run secret variable), read by Cloud Run as `ssc-data` when an instance starts. The stack exports it with the other settings, so the cell deployer restores it (SSC-087); a new version or connection is a new setting and a new revision. `cell_diff` treats these variables as the customer's own. A connection with no variable answers `503 CONNECTION_UNAVAILABLE`.

**Live checks** (operator, not run by SSC-050 or SSC-051), on a staging cell with `connections` on, an image built from this commit and an app given a connection (2 to 4 hold with no connection variable, every granted connection answering `503 CONNECTION_UNAVAILABLE`; 6 to 8 need one, set as in 9):

1. T6 (SSC-086): the customer's database sees the cell NAT's address and no other.
2. Google's keys. The first query after a cold start verifies its workload token, so the data gateway reached `www.googleapis.com/oauth2/v3/certs` through Direct VPC egress (the cell NAT or Private Google Access). A `503 UNAVAILABLE` at stage `workload` means it did not.
3. Invoker. An app's query reaches the container with no `403` from Cloud Run, and a request with no token answers the data gateway's own `401 UNAUTHENTICATED`.
4. Kill at zero. Let the data gateway scale to zero, run `ssc disable <app>` (or suspend the connection), then have the app query: the first answer is `403 APP_NOT_ACTIVE` (or `CONNECTION_SUSPENDED`), under 5 s after `latest.json` moves.
5. Cold start. After 15 idle minutes the first query is served; its `datagw query` log line has `cold: true`, `instance_started_at` and `ready_ms`. Note `ready_ms` and the query's `elapsed_ms`.
6. A real database. Run `postgres_setup.sql` on a staging Postgres (a Cloud SQL replica if there is one) with its server CA pasted as `ca`, set the connection variable, and query from the app: rows come back, `SELECT * FROM <a schema not named>` is `QUERY_FAILED` 42501, and `NOTIFY x` is `QUERY_REFUSED`. In `pg_stat_activity` the session's `application_name` is the query's tag while it runs and the client address is the cell NAT's.
7. Pooler. Point the same connection at a PgBouncer in transaction mode in front of that database: every query is `503 CONNECTION_UNAVAILABLE`, with `a pooler is between` as the logged reason.
8. Kill. Start `SELECT pg_sleep(25)` with `timeout_ms` 30000, then suspend the connection: the answer is `CONNECTION_SUSPENDED` within 3 s plus a bucket read, and the backend is gone from `pg_stat_activity` within 5 s more.
9. Connection secret. Have the agent ensure `ssc-conn-<20>` (`POST /v1/secrets/ensure`) and add a version through the intake. Expected: the create succeeds with the tag bound (`gcloud resource-manager tags bindings list --parent=//secretmanager.googleapis.com/projects/<project>/secrets/ssc-conn-<20>` shows `ssc-secret-kind=connection`), its policy names `ssc-data` alone, and `gcloud secrets versions access` impersonating `ssc-data` reads it. Then set `datagw_connections` to that version and apply: the revision starts and the connection answers rows.
10. App secret refused. Grant `ssc-data` `secretAccessor` directly on an `ssc-a-*` secret (the probe secret will do), and read a version impersonating `ssc-data`: refused with the deny policy named, while the same read of the connection secret still succeeds. Remove the grant. Also confirm the agent impersonated cannot read the connection secret, and `ssc-data` cannot read a `ssc-conn-*` secret made without the tag.

## File storage

App files (SSC-046) sit in the cell bucket under `files/<env_id>/`, and the data gateway's file broker hands out 10-minute signed links to them (`docs/contracts/data-gateway.md`, "Files"). Every grant below is in the cell's base stack, written at onboarding (SSC-087), so the first app that asks for files gets a working broker as soon as the `connections` flag creates `ssc-datagw`, with no person acting (`packages/ssc_control/tests/test_cell_resources.py`).

- **Customer's key.** The bucket's default key is the cell's own `bucket` key (`key-bucket`, the cell key ring, rotated like the others). Cloud Storage's agent for the project, `service-<project number>@gs-project-accounts.iam.gserviceaccount.com`, read through `gcp.storage.get_project_service_account`, holds `cloudkms.cryptoKeyEncrypterDecrypter` on it (`storage-agent-key`), and the bucket waits for that grant. A default key applies to objects written after it is set: on an existing cell, snapshots and anchors already in the bucket keep Google's key until they are rewritten.
- **Signing.** `ssc-data` holds `iam.serviceAccountTokenCreator` on itself (`data-signs-as-itself`) and signs each link through IAM `signBlob` (`iamcredentials.googleapis.com`, enabled with the cell's APIs). No account key exists, and `iam.disableServiceAccountKeyCreation` forbids one (SSC-095).
- **Data gateway.** `ssc-data` holds `storage.objectUser` on the bucket under the condition `only app files` (`bucket-data-files`): objects whose name starts `files/`, or a listing whose prefix does. It counts an environment's bytes with that listing and deletes a file through the API; the app's own `PUT` and `GET` go straight to Cloud Storage with the link.
- **Agent.** The custom role `sscCellAgentFiles` (`storage.objects.delete`) is bound on the bucket under the same condition (`bucket-agent-files`), not on the project. With its bucket-wide `storage.objectViewer` the agent lists an environment's files and deletes them through `POST /v1/files/drop` (it reads `SSC_CELL_BUCKET`), refused while the environment's service runs. The flow that calls it once the database's grace period ends is not built yet.
- **Versions.** The bucket keeps replaced and deleted objects as noncurrent versions; a lifecycle rule deletes noncurrent versions under `files/` seven days after they become noncurrent. Cloud Storage's soft delete may keep a deleted object for its retention period after that.
- **Egress.** `storage.googleapis.com` and the other storage hosts are refused on the egress allowlist (`ssc_contracts.egress`); apps reach Cloud Storage only through Private Google Access, with `.googleapis.com` in `NO_PROXY`.

**Live checks** (operator, not run by SSC-046), on a staging cell with `connections` on, `datagw_image` built from this commit and an app with `[files]` in its manifest:

1. Key. `gcloud storage buckets describe gs://ssc-c-<label>-cell --format='value(default_kms_key)'` names `.../cryptoKeys/bucket`, and an object the app uploads shows the same `kms_key` in `gcloud storage objects describe`.
2. First use. On a cell that never had `ssc-datagw`, turn on `connections` and deploy the app: its first `put` (after the helper's one retry while the gateway starts) succeeds with no one granting anything.
3. Signing. The `put` link's `X-Goog-Credential` names `ssc-data@ssc-c-<label>.iam.gserviceaccount.com`, and `gcloud iam service-accounts keys list --iam-account=ssc-data@ssc-c-<label>.iam.gserviceaccount.com --managed-by=user` lists nothing.
4. Prefix. Edit a valid `get` link's path from `files/env_<A>/` to `files/env_<B>/`: Cloud Storage answers `403 SignatureDoesNotMatch`.
5. Size. A `put` of 26 MB with a 25 MB link is refused by Cloud Storage (`EntityTooLarge` or `400`), and the object does not exist.
6. Attachment. A stored `index.html` opened from its `get` link in a browser downloads instead of rendering; `curl -I` shows `Content-Disposition: attachment`.
7. Conditions. Impersonating `ssc-data`, `gcloud storage ls gs://ssc-c-<label>-cell/files/` succeeds, `gcloud storage ls gs://ssc-c-<label>-cell/` and a copy to `snapshots/x` are refused; impersonating the agent, `gcloud storage rm` of an object under `snapshots/` is refused and one under `files/` succeeds. This settles the `objectListPrefix` attribute in the condition, which Google documents for `storage.objects.list`.
8. Kill. `ssc disable <app>`, then `put`: `403 APP_NOT_ACTIVE` once the snapshot moves; a link handed out before keeps working until it expires.
9. Private access. From the app, a transfer to `storage.googleapis.com` goes out by Private Google Access with no proxy, and the egress proxy's log has no line for it.

## Audit anchors

Each org's daily audit anchor goes to its cell's bucket, beside the snapshots, under `audit-anchors/<org>/` (SSC-012, decision 012 amendment). The worker is the only account that may change them: it holds `storage.objectUser` on the cell bucket (`bucket-control-worker`), the API's grant there (`bucket-control`) is gone because no API code uses a cell bucket, the gateway and the agent hold `storage.objectViewer`, `ssc-data` and `ssc-proxy` hold it under `snapshots/` only, `ssc-data` and the agent may change app files under `files/` alone (File storage, above), and no other cell account has a storage role. The cells folder enforces `iam.automaticIamGrantsForDefaultServiceAccounts` (Organisation policies), so no default account of a new cell project gets `roles/editor`. The retention lock is Step 5 and cannot be a bucket lock, because `latest.json` is replaced on every compile. Live checks for the proof run, on a staging cell whose org has events, run as the operator with the worker's settings: `SSC_DATABASE_DSN` through `cloud-sql-proxy` (runbook SSC-064), `SSC_ENV=staging`, `SSC_BLOB_BACKEND=gcs`, `SSC_BLOB_BUCKET`, `SSC_BLOB_SIGNER` and `SSC_CELL_BUCKET_TEMPLATE=ssc-c-{cell}-cell`, with credentials that may use the cell bucket (the just-in-time grant on the cells folder):

1. The job. After the first 00:05 UTC tick, `gcloud storage ls gs://ssc-c-<label>-cell/audit-anchors/<org>/` lists `<YYYY-MM-DD>.json`, and `gs://ssc-control-staging-blobs/audit-anchors/` lists nothing.
2. Verify. `uv run python -m ssc_control.audit verify --org <org> --anchors` exits 0 and prints `anchors: 1 checked`. With `SSC_CELL_BUCKET_TEMPLATE` unset it prints `anchor missing` for that key and exits 1, which shows it read the other store.
3. Only the worker writes. Impersonating `ssc-gateway@ssc-c-<label>.iam.gserviceaccount.com`, then `ssc-cell-agent@…`, `gcloud storage cp` of a file to `gs://ssc-c-<label>-cell/audit-anchors/<org>/x.json` and `gcloud storage rm` of the day's anchor are both refused with 403, and so are they impersonating `ssc-control@ssc-control-<stage>.iam.gserviceaccount.com` (the API). Also read the cell project's IAM policy (`gcloud projects get-iam-policy ssc-c-<label>`): no account besides the stack's own (`ssc-*`), Google's service agents and the operator holds a role there. In particular the Compute Engine default account holds no `roles/editor`, because the agent may run a service or a build as any account in the project, so one with `roles/editor` could change anchors. The folder policy stops new grants only, so a project made before it may still hold one: if it does, remove it.
4. Restore drill, on staging only, when decision 012's restore procedure is rehearsed: after restoring the staging control database to a time T, run `uv run python -m ssc_control.audit reanchor --org <org> --restored-to <T> --ref <ticket>` for every org. It prints `reanchored: seq N at audit-anchors/<org>/restore-…`, the object is in the cell bucket, and `verify --anchors` exits 0.

## Public entry

Each cell has its own door (SSC-088), created at onboarding with no flag:

- A global external Application Load Balancer on the reserved address `ssc-entry`: forwarding rule `ssc-entry-https` (443) to the HTTPS proxy, and URL map `ssc-entry` with three backend services, each on its own serverless NEG: `ssc-gateway` for every host but two, `ssc-cell-agent` for `ssc--agent.<label>.delimitusapps.com` alone, and `ssc-secret-intake` for `ssc--secrets.<label>.delimitusapps.com` alone. The gateway's backend never serves a reserved host, and the other two serve nothing else.
- The HTTPS proxy carries the SSL policy `ssc-entry`: TLS 1.2 at least, `MODERN` profile. A second rule, `ssc-entry-http` (80), on the same address only redirects to HTTPS. The first five forwarding rules in a project are billed as one, so the redirect adds nothing: the entry is about $18.25 a month plus $0.008 per GB.
- A serverless NEG's backend timeout is fixed at 60 minutes and Google refuses `timeoutSec` on it, so the stack leaves it unset; the gateway's own 3600 s request timeout is what ends a WebSocket at the hour.
- Certificate Manager: DNS authorisation `ssc-cell` for `<label>.delimitusapps.com`, the wildcard certificate `ssc-cell-wildcard` for `*.<label>.delimitusapps.com`, map `ssc-entry` and map entry `ssc-wildcard`. No Cloud Armor.
- The same run writes two records into the apps zone: the authorisation CNAME `_acme-challenge.<label>.delimitusapps.com.` and `*.<label>.delimitusapps.com.` A to the entry address.
- The gateway's invoker is `allUsers`; its ingress stays internal and load balancer, which keeps its `run.app` host closed. The grant is allowed only because the gateway carries the public-invoker tag (see Organisation policies); the stack binds the tag before it grants.
- The cell agent's ingress is internal and load balancer too, and its invoker is still only the control plane's identity. Its custom audience is `https://ssc--agent.<label>.delimitusapps.com`, so an ID token for that URL is accepted through the load balancer; its `run.app` host is closed. No slug can claim the agent host: slugs never contain `--`.
- The secret intake's ingress is internal and load balancer, and its invoker is `allUsers` under the same tag as the gateway (Secrets, below).

The cell stack exports `entry_address`, `public_host_suffix` (`<label>.delimitusapps.com`), `certificate_id`, `agent_host`, `agent_url`, `intake_host` and `intake_url`. `agent_url` is `https://ssc--agent.<label>.delimitusapps.com`, the URL the control plane calls and the audience of its ID token; it was the agent's `run.app` URL before SSC-095. The control plane derives it from the label in `SSC_CELLS` (decision 029); put it in the cell's `agent_url` in the GitHub variable `SSC_NIGHT_CELLS` (nightly). `intake_url` is `https://ssc--secrets.<label>.delimitusapps.com`, which the API derives the same way.

**Zones and the registrar step.** The platform stack owns two public zones in `ssc-platform-0`: `delimitusapps` (`delimitusapps.com.`, the cells' records) and `delimitus` (`delimitus.com.`, for `api`, `auth` and `keys`, whose A records the control plane's entry writes). Both are protected from deletion. A cell stack runs as the operator, who may write records in the apps zone only, through the custom role `sscZoneRecords` granted on that zone. After `pulumi up --stack platform`, the founder sets each domain's name servers at the registrar from `pulumi stack output apps_zone_name_servers` and `platform_zone_name_servers`, and deletes the domain's DS records there. Both domains still carry DS records from zones that no longer exist; with them left in place, validating resolvers fail every lookup and no certificate is issued. The `.com` delegation is cached for up to 48 hours, so do this well before the first cell.

**DNSSEC** is off on both zones for now. Turning it on later is a zone setting plus a new DS record at the registrar.

**Certificate issue time.** Google issues the certificate once the authorisation CNAME resolves publicly, usually within minutes; the done-when allows 30 minutes from the record. Until then the HTTPS rule answers with a TLS failure. `entry_probe` waits up to 30 minutes and prints how long it took.

## Control plane

The platform stack runs the control plane in `ssc-control-<stage>` for each stage named in `control_stages` (SSC-064, `ssc_infra/control.py`). The live runbook is `docs/runbooks/ssc-064-control-plane.md`.

- **Settings** (platform stack config):
  - `control_stages`: a list, `["prod"]`, `["staging"]` or both. Unset or empty: no control plane, only the staging project and its identities, as before. `ssc-control-prod` is made only once `prod` is named.
  - `control_staging_billing`: `false` unlinks `ssc-control-staging` from billing while `staging` is not in `control_stages`. It then holds only accounts, IAM and free APIs, and its slot goes to prod (one billing account holds five projects; set 2026-10-04 for the proof run).
  - `public_stage`: the stage that holds `api`, `auth` and `keys.delimitus.com`; defaults to `prod` when named, else `staging`. One stage at a time: the three hosts have one A record each.
  - `control_image`, `auth_jwks`, `auth_signing_kid`: the release, all or none. `control_image` is a build of `packages/ssc_control/Dockerfile` pinned by digest in `ssc-platform`; `auth_jwks` is the auth host's public JWKS and must hold `auth_signing_kid`. Until they are set every service runs the placeholder image with no settings, the worker pool runs no instance and there is no migration job.
  - `cells`: the cells this control plane serves, a JSON list of `{"label": ..., "jwks": ...}`, each `jwks` that cell stack's `identity_jwks` output (a string), each label once (decision 029). Each org is served by the cell its `cell_label` names. The API and worker get `SSC_CELLS` (compact, sorted keys), and the worker `SSC_RUNTIME_DRIVER` and `SSC_BUILD_DRIVER` `cell_agent`. Every per-cell setting derives from the label through `naming` and `ssc_shared.hosts`: the agent and intake URLs, the cell bucket and the issuer `https://keys.delimitus.com/<label>`. Unset, the older pair `cell_label` and `cell_jwks` (both or neither) is read as a one-cell list; setting both forms is refused. Only the stage the cells trust gets them, on its API and worker (the public stage; with none, every stage), because a cell grants nothing to another stage's accounts (`ControlConfig.serves_cells`). The cell's `sql_instance` output is not used: the control plane talks to the cell's database only through the agent, and the agent's own `SSC_SQL_INSTANCE` is the cell stack's.
  - `worker_instances`: the worker pool's instance count, 1 by default.
  - `timer_key_id`: the worker's timer key (SSC-041). Unset, there is no timer key secret and the worker keeps its current dispatcher. Set, the stack makes the `SSC_TIMER_SIGNING_KEY` secret, readable by the worker alone, and gives the worker `SSC_TIMER_DISPATCHER=https`, `SSC_TIMER_KEY_ID` and `SSC_APPS_DOMAIN`. Add the secret's version before the worker next starts (`docs/runbooks/ssc-064-control-plane.md`, step 7); every cell's `timer_jwks` is that key's public JWKS.
  - `landing`: `false` by default. True builds delimitus.com's account `ssc-landing`, its request bucket `ssc-control-<stage>-pilot-requests` (object create only), the notification channel and two alerts in the public stage's control project (SSC-065, decision 028). Nothing is public yet. It needs a control stage.
  - `landing_image`: the landing page's image, a digest in the platform registry, and only with `landing`. Set, the Cloud Run service goes behind `ssc-control-entry` as two more host rules (`delimitus.com`, `www.delimitus.com`) with a second certificate `ssc-control-entry-landing`, and their A records go in the `delimitus` zone.
  - `landing_notify_email`: where the two alerts go (a pilot request stored, a pilot request not stored); the operator by default.
- **Processes**, one account each, all from one image:

  | Process | Cloud Run | Account | Command |
  | --- | --- | --- | --- |
  | API | service `ssc-api`, request-billed, min 0 in staging and 1 in prod, max 3 | `ssc-control` | `python -m ssc_control.api` |
  | Auth host | service `ssc-auth`, request-billed, min 0, max 2 | `ssc-auth` | `python -m ssc_control.identity serve` |
  | Worker | worker pool `ssc-worker`, manual scaling, `worker_instances` | `ssc-control-worker` | `python -m ssc_control.worker` |
  | Migrations | job `ssc-control-migrate`, run by the operator | `ssc-control-migrate` | `ssc_control.db.migrate.upgrade` |

  The services' ingress is internal and load balancer. Each has 1 vCPU and 512 MiB.
- **Worker.** Procrastinate polls the database, so the worker cannot scale to zero on requests; it runs always-on in a worker pool. One instance of 1 vCPU and 512 MiB at the worker-pool rates ($0.0000108 per vCPU-second, $0.0000012 per GiB-second) is about $30 a month.
- **Database.** Cloud SQL `ssc-control`, Postgres 18 on `db-f1-micro`, `max_connections=50`, database `ssc`, protected from deletion, point-in-time recovery in prod. It has a public address with no authorised network, and both `connector_enforcement=REQUIRED` and client certificates are required, so only the Cloud SQL connector reaches it. Every process mounts it at `/cloudsql`, and each account holds `cloudsql.client`. The roles `ssc_app` and `ssc_migrate` are made by the operator, so no password is in state.
- **Secrets.** Each is an empty container in `us-central1`, and the operator adds every version. Each is readable only by the accounts that take it, granted on the secret:

  | Secret | Read by |
  | --- | --- |
  | `SSC_DATABASE_DSN` | API, worker, auth host |
  | `SSC_MIGRATE_DSN` | migrations |
  | `SSC_METRICS_KEY` | API, worker |
  | `SSC_WORKOS_API_KEY`, `SSC_WORKOS_CLIENT_ID` | worker (directory sync), auth host |
  | `SSC_AUTH_SIGNING_KEY`, `SSC_AUTH_STATE_KEY` | auth host |
  | `SSC_TIMER_SIGNING_KEY`, only with `timer_key_id` | worker |

  Services read `latest` at start, so a new version needs a new revision. The cell deny rule names all four accounts of every control project.
- **Blobs.** The private bucket `ssc-control-<stage>-blobs`, with signed URLs only. The API and the worker each hold `storage.objectUser` there and sign as themselves (`iam.serviceAccountTokenCreator` on their own account).
- **Entry**, in the public stage only. A global external Application Load Balancer on the address `ssc-control-entry`, with HTTPS on 443 and a redirect on 80. The SSL policy requires TLS 1.2 and the `MODERN` profile. The URL map sends `api` to `ssc-api`, `auth` to `ssc-auth`, and `keys` to the backend bucket `ssc-keys`. One Google-managed certificate names the three hosts. The three A records go in the `delimitus` zone. The two services' invoker is `allUsers`; the `ssc-platform` folder does not carry the cells' members policy.
- **Keys.** `keys.delimitus.com/<label>/jwks.json` is the object `<label>/jwks.json` in the bucket `ssc-control-<stage>-keys`, whose objects are public. One is written for each cell in `cells`, from its `jwks`, which the stack refuses if it has a private member, and is served with `Cache-Control: public, max-age=300`. The gateway reaches `auth` and `keys` through the cell's two bypass rules and its NAT.
- **Who calls the cells.** Two accounts. The API calls the agent and mints intake grants, so the intake's `SSC_CONTROL_SA` stays `ssc-control`. The worker calls the agent for runtime, builds and databases, uses the cell bucket and starts the deployer. The cell stack reads `control_workers` from the platform stack and grants the worker `run.invoker` on the agent (`agent-invoker-worker`) and `storage.objectUser` on the cell bucket (`bucket-control-worker`); the API has no grant on the cell bucket (SSC-012). So apply the platform stack before the cells. A cell trusts the accounts of `control_public_stage` whatever its own stage, because that control plane serves its gateway's login and keys; with no public stage, its own stage's (`cell.control_for`). Re-apply each cell after the public stage first appears or moves.
- **Outputs.** The stack exports:
  - `control_service_accounts` (the API's), `control_workers` and `control_accounts`, per stage;
  - `control_sql_instances`, the connection names;
  - `control_public_stage` and `control_entry_address`, once a public stage exists.
- **Cost a month.** Prod, with the entry: worker $30, Cloud SQL about $10, entry $18.25, the API's idle minimum instance about $10, secrets $0.42, so about $70 against $75. Staging without the entry is about $40 against $40. With the entry it is about $59, so keep the entry in prod. Set `worker_instances` to 0 to stop a stage's worker.

## Alerts and on call (SSC-062)

`alerts.py` builds every alert from one table (`CELL_ALERTS`, `PLATFORM_ALERTS`); `docs/runbooks/ssc-062-support-and-on-call.md` has one heading per alert name, and `docs/support/how-to-get-help.md` is the builders' page. A test checks both ways that every alert has its heading and every heading its alert.

- **Setting.** `oncall_email`, in a cell stack and in the platform stack. Set, each stack makes one email channel `oncall` in its project (a cell's, and each control project's) and its alerts. Unset, no alert resource exists and the cell budget has no channel. It is not a secret.
- **Sleeps are healthy.** A gateway or data gateway with no instances is normal. No alert uses an instance count, an uptime check, a heartbeat or missing data (`evaluation_missing_data` is inactive on all of them), so a cell with nothing running raises nothing overnight.
- **Cell alerts** (cell stack): `ssc-gateway-authoriser-errors` (over 5 `authz check failed` lines in 5 minutes), `ssc-gateway-snapshot-stale` (any `gateway snapshot stale` line, which the gateway writes when a served request finds the snapshot over 60 s old, at most once a minute per instance), `ssc-gateway-lb-error-rate` (over 5% of the gateway's requests at the load balancer are 5xx for 5 minutes), `ssc-datagw-refusals` (over 20 refusals in 5 minutes, `APP_NOT_ACTIVE` and `served` left out) and `ssc-proxy-unhealthy` (the proxy's health check went from healthy to unhealthy). Counters are log-based metrics of the same names in the cell project.
- **Control-plane alerts** (platform stack, in each control project): `ssc-snapshot-late` (a compile over 60 s or failed for good, or a sweep that found a stale snapshot, any in 1 minute) and `ssc-build-failures` (3 `build failed` lines in 15 minutes). A late write to a cell bucket is a late compile. The sweep still runs every 5 minutes; no migration was needed.
- **Budget.** `Cell.budget` (`cell-monthly`, 50, 90 and 100 percent of spend and 100 percent of forecast) notifies the channel too. No second budget exists.
- **Certificates.** No Certificate Manager expiry or renewal metric is documented in the provider schema or in this repository, so none is used. The nightly run takes `alpha.<base>` of each cell in `SSC_NIGHT_CELLS` (a host under the cell's apps domain; the old variable `SSC_PROBE_TLS_HOST` is no longer read): it opens a verified TLS connection and fails when the wildcard certificate does not verify or has under 21 days left. A failed renewal keeps the old certificate serving, so the check catches it 14 or more days before it expires.
- **Proxy health.** The `proxy-health` check now writes its logs (`log_config.enable`), on every cell, alerts or not.
- **Gateway log lines.** The authoriser logs `gateway snapshot age snapshot_age_ms=N` at INFO at most once a minute per instance, and `gateway snapshot stale snapshot_age_ms=N` at WARNING when N is over 60000. An idle instance logs neither.

**Live checks** (operator, not run by SSC-062), on a staging cell and the staging control project with `oncall_email` set to an address you read:

1. Apply. `pulumi preview` shows only the channel, the metrics, the policies and the budget's notification; the first apply may need the Monitoring API to finish enabling.
2. Health-check log filter. Read the filter on `ssc_proxy_unhealthy` (`log_id("compute.googleapis.com/healthchecks")` with `jsonPayload.healthCheckProbeResult.healthState="UNHEALTHY"` and `previousHealthState="HEALTHY"`) against a real entry in Logs Explorer: the field names are written from the documented format and must match. With `egress` on, stop the proxy machine: an email arrives and the group brings it back.
3. Stall. Make a snapshot compile slow or fail on staging (for example block the worker's write to the cell bucket): the `ssc-snapshot-late` email arrives within 5 minutes. Restore it.
4. Stale gateway. Block the gateway's read of the cell bucket for over a minute while sending requests: a `gateway snapshot stale` line, then the `ssc-gateway-snapshot-stale` email.
5. Quiet night. Leave a staging cell with nothing running overnight: no alert email, and the budget unchanged.
6. Load balancer. Check `ssc-gateway-lb-error-rate`'s filter (`https_lb_rule`, `backend_target_name`, `response_code_class`) in Metrics Explorer shows the gateway's requests.
7. Datagw. Call the data gateway with a refused request 21 times in 5 minutes: the email arrives; a stopped app's `APP_NOT_ACTIVE` calls raise none.
8. Budget. The budget's notification channel shows `oncall` in the Billing console; the deployer needs permission to attach a monitoring channel to it.
9. Certificate. Run the nightly workflow with the cell in `SSC_NIGHT_CELLS`: it passes. Point it at a host whose certificate is under 21 days from expiry, or a name not on the wildcard: it fails.

## Organisation policies

The platform stack applies one table (`policies.cell_rules`) to the `ssc-cells` folder, so every cell inherits it. The platform stack exports it as `cell_policies`.

| Constraint | Setting | What it stops in a cell |
| --- | --- | --- |
| `gcp.resourceLocations` | allow `in:us-central1-locations`, `global` | Anything outside `us-central1`. `global` is for the entry's certificate, DNS authorisation and certificate map; global Compute resources are exempt anyway. `ssc-platform` keeps the region alone. |
| `storage.publicAccessPrevention` | enforced | A public object or bucket. Nothing in a cell needs one: public keys are served from `keys.delimitus.com/<label>/jwks.json`, not a cell bucket. |
| `iam.managed.allowedPolicyMembers` | enforced; members: the operator and principals of organisation 878392300952 | A role for anyone outside the organisation, `allUsers` included. **One exception**, below. |
| `iam.disableServiceAccountKeyCreation` | enforced | Service account keys. |
| `iam.automaticIamGrantsForDefaultServiceAccounts` | enforced | Google granting `roles/editor` to a new project's Compute Engine and App Engine default accounts. The agent may run a service or a build as any account in its project, so an Editor default account would let it change the cell bucket, audit anchors included (SSC-012). It stops new grants only; an existing project keeps one until it is removed. |
| `compute.restrictVpcPeering` | allow `under:organizations/433637338589` | Peering with anything but Google's service producers, which Cloud SQL's private services access needs. Set `peering_allowed` in the platform stack's config if the first cell's peering is refused. |
| `compute.restrictSharedVpcHostProjects` | deny all | A cell joining another project's network. Becoming a host needs org-level `compute.xpnAdmin`, which nobody holds. |
| `run.allowedIngress` | allow internal, internal and load balancer | A Cloud Run service open on its `run.app` host. |
| `compute.vmExternalIpAccess` | deny all | An external address on a VM. The proxy has none; the NAT and entry addresses are not VM addresses. |
| `sql.restrictPublicIp` | enforced | A public address on Cloud SQL. |

The domain-restricted sharing row uses the managed constraint rather than `iam.allowedPolicyMemberDomains`: the organisation has no Workspace customer, and the legacy constraint cannot name the operator's personal account, so it would refuse the operator's just-in-time and probe grants.

**The one exception.** The `allUsers` invoker of the gateway and of the secret intake (`policies.PUBLIC_SERVICES`). The platform stack owns the tag `ssc-public-invoker=gateway` on the organisation, and the members rule is off only where that tag is bound. The cell stack binds it to `ssc-gateway` and `ssc-secret-intake` and nothing else. The intake reuses the tag value rather than a second one: the same binders and the same exception, so the platform stack is unchanged. Binding needs `resourcemanager.tagUser` on the value, which the platform stack grants to the operator alone, authoritatively, so a holder added by hand is removed on the next run. The same operator runs both stacks and is organisation and policy admin, so this does not stop the operator. It does stop:
- every runtime identity (cell agent, control plane, apps, nightly), which can never bind the tag and so never make anything public;
- a cell stack mistake that grants `allUsers` or an outside member anywhere but the two tagged services, which is refused at apply;
- the tag bound anywhere else (the agent, the data gateway, the project), which `tests/test_policies.py` refuses before apply.

The cell deployer (SSC-087) is the second binder: it applies cell stacks, and the two services it keeps are tagged. The grant still names nobody else.

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

### Onboarding in one command (SSC-091)

```
uv run python -m ssc_infra.onboard <label> --settings settings.json \
    --probe-image us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform/<probe>@sha256:<digest>
uv run python -m ssc_infra.onboard <label> ... --dry-run      # the plan and the exact commands; runs nothing
uv run python -m ssc_infra.onboard <label> ... --resume       # continue a stack that exists
uv run python -m ssc_infra.onboard <label> ... --from-step 5  # start at step 5 (implies --resume)
```

`settings.json` is a JSON object of stack settings (or give single ones with `--set key=value`). `gateway_image` and `org_id` are required. `stage` and `probe` are set for you, and the sealed keyring, its JWKS and `probe_digest` are made by the command: they are refused in the settings. Run it from the repository root with `gcloud`, `pulumi` and `docker` signed in (the last to the cell's Artifact Registry: `gcloud auth configure-docker us-central1-docker.pkg.dev`).

The steps print as `[n/9]` with each step's time and the running total, and a line every 30 seconds while `pulumi up` runs (resources done, resources in flight).

1. Stack and settings: the stack, in the local state folder (below), with the KMS secrets provider and the settings the first apply can take. An existing stack is refused unless `--resume` is given. A stack that already exists in the state bucket is refused before this step.
2. DNS sinkhole rules: `not needed: the rules go in the first apply on local state (SSC-091)`. The step stays so the numbers do not move.
3. `pulumi up` (with `--parallel 32`): project, network, registry and the gateway's key, and the sinkhole's rules.
4. The gateway keyring: made and sealed with the stack's key in memory (never written to disk or printed), then set with the gateway's settings and the images that need the gateway. `org_id` goes in the first apply, since the agent needs it.
5. The probe image copied into the cell's `ssc-apps` registry by digest (`docker buildx imagetools create`, as the proof run does), and `probe_digest` set.
6. `pulumi up` (with `--parallel 32`): gateway, agent and probe-runner.
7. The certificate: waits for `ssc-cell-wildcard` to be `ACTIVE`, 120 minutes at most.
8. The floor probes: `ssc_conformance.nightly` with the cell's project, agent URL and probe digest.
9. Move the state to the bucket (below). It counts in the total and the 15 minutes, and prints its own line, `state move: mm:ss`.

**Where the state lives while it runs.** Pulumi rewrites the whole checkpoint after every resource, and in the bucket with a 7 MB state that made 8.8 sinkhole rules a minute (274 on a local file; `spikes/sinkhole/pulumi_probe/README.md`). So steps 1 to 8 run every `pulumi` call, both applies included, with `PULUMI_BACKEND_URL=file://~/.ssc/onboard/<label>`, a folder made with mode 0700 and never under a temporary folder. The secrets provider is still the KMS key, and `Pulumi.<stack>.yaml` stays in `infra/`. Step 9 then:

1. exports the local stack (`pulumi stack export`, secrets stay sealed) and saves a copy of `Pulumi.<stack>.yaml` as `Pulumi.<stack>.yaml.before-init` in the folder;
2. creates the stack in `gs://ssc-platform-0-pulumi` with the same secrets provider, and stops if `secretsprovider` or `encryptedkey` in the yaml changed (it names the line, never the key, and does not restore the copy);
3. imports the export, and exports the bucket's stack to check it holds the same resources;
4. runs `pulumi preview --expect-no-changes --parallel 32` against the bucket;
5. renames the folder to `~/.ssc/onboard/<label>.moved-<UTC>`. It is kept, with `export.json` in it, and never deleted by the command: remove it by hand when the cell is settled.

**If a run crashes.** The state is in the folder, so run the same command with `--resume` (or `--from-step N`). If pulumi says the stack is locked, run `PULUMI_BACKEND_URL=file://$HOME/.ssc/onboard/<label> pulumi cancel --stack <stack>` in `infra/` first. If step 9 stops halfway (the stack is created in the bucket but not imported, or imported but not previewed), a plain run is refused, with the folder and the bucket's stack named; run it with `--from-step 9`. That carries on from what it finds in the bucket: an empty stack is imported into, a stack holding the same resources is only previewed, and a stack holding different resources is never overwritten (the command stops and says so). If it stops on `encryptedkey changed`, put `Pulumi.<stack>.yaml.before-init` back as `infra/Pulumi.<stack>.yaml` by hand before running it again. A stack onboarded before this change lives only in the bucket and is refused: this command does not resume it. A run whose state was moved is refused as already onboarded.

The certificate comes before the floor probes because the cell agent is reachable only through the cell's load balancer, which needs it. Its wait is not part of the 15 minutes (founder decision D6); the probe image and probe runner are. The last line is the verdict:

```
onboarding 12:41 to floor probes (budget 15:00, PASS); certificate 38:07
```

The certificate figure runs from the end of step 3 (when its DNS record exists) to `ACTIVE`. A resumed run counts only its own time and says so. A failed step prints its number, its time and `resume with: ...`, then exits 1. Each step is idempotent, and with `--resume` a step whose work is visibly done (an output, a setting, the image in the registry) is skipped. The labels `proofcell01` and `proofcell02` are always refused.

**Not yet timed on this path.** Step 3 creates the sinkhole's rules on local state, where the probe made 274 rules a minute with a 7 MB state; the first full run on this path has not been timed yet, and its verdict is the first figure that counts. The rules' names and inputs are pinned by a test (`test_the_sinkhole_s_rules_keep_the_names_and_inputs_the_live_cells_were_built_with`), so a change cannot make the next `pulumi up` on a live cell replace them.

**Open item (SSC-091).** The cell deployer is unchanged: its flag runs (`pulumi up` on a live cell) still use the bucket as their backend, so they are as slow as the sinkhole creation was whenever a cell's state is that large. Moving them to a faster path is not part of this change.

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
