# infra

Pulumi in Python for SSC on Google Cloud (SSC-013, decisions 021, 022 and 025). This is a separate uv project outside the workspace, so the provider SDK never reaches the services.

| Stack | What it holds |
| --- | --- |
| `platform` | Folders `ssc-cells/{prod,staging}` and `ssc-sandbox`, with logs in `us-central1`. The location policy on `ssc-platform`, the cell policy table on `ssc-cells` and the public-invoker tag (SSC-095). The control projects `ssc-control-<stage>`, their four identities and, for each stage in `control_stages`, the control plane (SSC-064: API, worker, auth host, database, secrets; the public hosts in `public_stage`). The folder rule denying secret reads. Just-in-time staff access. The $250 monthly budget. The public DNS zones `delimitusapps.com.` and `delimitus.com.` in `ssc-platform-0`. |
| `c-<cell label>` | One cell, in two parts. At onboarding: project `ssc-c-<label>` with a $50 budget alert, identities `ssc-gateway`, `ssc-cell-agent`, `ssc-build`, `ssc-data` and `ssc-secret-intake`, KMS, bucket, Artifact Registry, the VPC with its firewall floor, DNS sinkhole and the empty database zone `ssc-sql`, Cloud NAT with the cell's fixed IP, reserved addresses for the proxy, the data gateway and the database range, the gateway (request-billed, minimum 0, 3600 s requests), the cell agent (one instance at most) with its two log views and its Monitoring read and the secret intake behind the cell's public entry, and the cell's own deny rule. On first use: whatever the flags below turn on. |

The lazy flags are turned on by the cell deployer when the control plane asks (SSC-087, below). The operator can still set any flag by hand.

State lives in `gs://ssc-platform-0-pulumi`, and secrets are encrypted with the KMS key `ssc-platform/pulumi-secrets`. Every call's quota goes to `ssc-platform-0`. The tools refuse any command that names `ristretto-506621`.

Stack files (`Pulumi.<stack>.yaml`) are not committed: each holds a data key that the secret scanner flags. On a fresh clone, `bootstrap` (and `bootstrap cell <label>`) writes them again with the KMS secrets provider and the stack's config.

## Cell flags

Five settings in a cell stack's config, all off by default. Turning one on adds only the resources named here (`naming.LAZY_RESOURCES`); the firewall rules and addresses they use exist from onboarding.

| Flag | Default | Adds | About a month |
| --- | --- | --- | --- |
| `database` | `false` | Cloud SQL Postgres 18 `ssc-cell`, zonal `db-f1-micro`, `max_connections` 25, private address in the database range, a CA-issued certificate for its DNS name and that name's record in `ssc-sql`, the Data API, customer-managed key, backups and point-in-time recovery, the cell agent's IAM database user in `cloudsqlsuperuser`, and `SSC_SQL_INSTANCE` on the agent (App databases, below) | $13 |
| `egress` | `false` | The proxy machine: one `e2-micro` with no external address at the reserved proxy address, in a group of one that recreates it. Its proxy software is SSC-053 | $7 |
| `connections` | `false` | The data gateway and file broker `ssc-datagw` on Cloud Run, minimum 0, as `ssc-data`, leaving through the cell NAT | usage |
| `gateway_min` | `0` | The gateway's minimum instances | about $10 each |
| `warm` | `false` | Keeps the gateway at one instance or more (SSC-092) | about $10 |

```
pulumi config set --stack c-<label> database true
pulumi up --stack c-<label>
```

Other settings: `stage` (`staging` or `prod`), `probe`, `probe_digest`, `agent_image`, `build_tools_image` and `build_frontend_image` (Builds, below), `gateway_image`, `gateway_keyring`, `gateway_jwks` and `org_id` (Gateway, below), `gateway_max` (20) and `billing_account` (defaults to the one SSC account; set it to link a new cell to another account, SSC-089).

The stack exports `flags`, so `cell_diff` compares two cells with different flags without their flagged resources.

**Subnets.** Two IPv4 `/24`s: `apps` (10.20.0.0/24) holds only apps; `gateway` (10.20.4.0/24) holds everything that may reach the internet: the gateway, the data gateway and the proxy. Only `gateway` is behind the NAT, so an app has no route out even if a firewall rule were wrong. To put everything in one `/24`, set `SUBNETS` in `cell.py` to the `apps` entry alone and `EDGE_SUBNET` to `"apps"`; the reserved addresses move with it.

## No-internet floor

Apps have no internet (SSC-027). The rules are written once at onboarding, against addresses reserved then, and no flag changes them: `tests/test_cell.py` checks that the network, firewall, NAT, private zone and response policy are identical on an empty cell and after `database`, then `egress`, then `connections`.

| Egress rule | Priority | Applies to | Destinations |
| --- | --- | --- | --- |
| `egress-internal` | 1000 | everything | 10.20.4.10/32 (proxy), 10.20.4.11/32 (data gateway), 10.21.0.0/20 (database range) |
| `egress-google-private` | 1000 | everything | 199.36.153.8/30 (`private.googleapis.com`) |
| `egress-gateway`, `egress-proxy`, `egress-data` | 1000 | tags `ssc-gateway`, `ssc-proxy`, `ssc-data` | 0.0.0.0/0 |
| `egress-deny-all` | 65534 | everything | 0.0.0.0/0, denied |

Apps carry no tag, so an app reaches the two reserved addresses (a dead end until the proxy or data gateway takes its address), the database range and Google's private APIs, and nothing else: not another app, not the gateway subnet, not another cell. `ingress-proxy` lets the apps subnet reach the proxy on tcp 3128 only. Until SSC-027, `egress-internal` allowed all of 10.20.0.0/16.

**No IPv6.** The VPC has no internal IPv6 range, both subnets and the proxy's interface are `IPV4_ONLY`, and every firewall range is IPv4, so an IPv6 connection has no route.

**DNS.** The response policy `ssc-cell` answers every name under every top-level domain with an unroutable sinkhole. Two lists bypass it: Google's names (`cell.GOOGLE_DNS_PASSTHRU`) and the platform hosts the gateway calls, `naming.GATEWAY_PLATFORM_HOSTS`: `auth.delimitus.com` (login) and `keys.delimitus.com` (identity note issuer and keys). Each platform rule is the exact name, so `x.auth.delimitus.com` still gets the sinkhole and a query can carry data only in those two fixed names. To add a host, add it to that tuple; it adds one rule. A third bypass, `*.sql.goog.`, hands Cloud SQL's names to the cell's private zone `ssc-sql` (`sql.goog.`), which answers every one of them itself: the database's name with its private address once the flag is on, any other name with NXDOMAIN, so none goes to public DNS (App databases, below).

**Why apps stay blocked.** A response policy belongs to the whole VPC: Cloud DNS cannot answer the apps subnet differently from the gateway subnet, so an app resolves the two platform hosts too. Resolving is not reaching. Their addresses are public, no allow rule names them for an untagged source, so `egress-deny-all` drops the packet; and the apps subnet is not behind the NAT, so there would be no route out even if a rule were wrong. The probe checks it: the probe runner job sets `PROBE_EGRESS_HOSTS` to the two hosts, and `no_direct_egress` dials each on 443 from the app, as well as its five fixed attempts (tcp 443 and 80, udp 53 and 443, IPv6).

Keep `auth` and `keys` as A records. The platform stack writes both, with `api`, to the control plane's entry address (Control plane, below); a CNAME to a name outside the bypass lists may be answered with the sinkhole.

**What SSC-053 must do** (the proxy machine; nothing here blocks it):
- Run the proxy at the reserved address 10.20.4.10, listening on tcp 3128, with its health check on the same port. The ingress rules (`ingress-proxy`, `ingress-proxy-health`) and `PROXY_PORT` already say so; a different port changes `PROXY_PORT` only.
- Resolve allowed hosts itself, through a public resolver over the NAT (the proxy tag may reach 0.0.0.0/0, and the gateway subnet is behind the NAT), not through the cell's resolver, which sinkholes them. Never add approved hosts to the response policy: it is VPC-wide, so apps would resolve them too, and it would change with every approval.
- Refuse any destination that resolves to a private, link-local or Google private address (10.0.0.0/8, 169.254.0.0/16, 199.36.153.8/30, and the like) and any IP literal. The proxy tag may reach everything, so the proxy alone keeps an app from using it to reach the gateway subnet, the database or the metadata server.
- Fetch its software without opening the floor per cell. The template has no service account and the boot image is Container-Optimized OS. Pulling from Artifact Registry needs a service account with `artifactregistry.reader` and `*.pkg.dev` names, which the `*.dev.` rule sinkholes; either add `pkg.dev.` and `*.pkg.dev.` to `GOOGLE_DNS_PASSTHRU` with a private `pkg.dev.` zone pointing at `private.googleapis.com` (one change for every cell, written at onboarding), or resolve through the public resolver as above.
- Leave `egress-internal`, the ingress rules and the NAT alone, and keep the before-and-after test passing.

**Live check** (operator, not run by SSC-027). On a staging probe cell with the nightly's settings (SSC-017: stack settings `probe`, `probe_digest` and `agent_image`; environment `SSC_PROBE_PROJECT`, `SSC_PROBE_AGENT_URL` and `SSC_PROBE_DIGEST`), all flags off:

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

Each `diff` prints nothing, and `no_direct_egress` passes every time. The sinkhole's first creation takes about 78 minutes (SSC-091).

## The cell deployer

The control plane turns on a cell's `database`, `egress` or `connections` flag without a person and without new powers of its own (SSC-087, decision 022 amendment pending). NAT and the fixed IP are not lazy.

- **Trigger.** In the control plane, the job `cell:create_resource` (`ssc_control/cell/`), one per cell and resource. It is asked for by the first deploy whose manifest has `[state] postgres = true` (database), the first approved internet host (egress), the first approved data source (connections), or an org admin (`POST /v1/cell/resources/{resource}/enable`, audited). A second request joins the one in flight. Each request, success and failure is an audit row (`cell.resource_requested`, `cell.resource_ready`, `cell.resource_failed`). Nothing turns a flag off: removing a resource is a runbook step.
- **Runner.** The Cloud Run job `ssc-cell-deployer` in `ssc-platform-0`, image `infra/deployer/Dockerfile`, running `python -I -m ssc_infra.deployer <cell label> <database|egress|connections>` as `ssc-cell-deployer@ssc-platform-0.iam.gserviceaccount.com`. It reads the config the stack was last applied with (the stack's `config` output), sets the one flag to `true` and runs `pulumi up` on that one stack. It refuses any third argument, any flag outside the three, a malformed label and any environment variable it does not expect, and gives Pulumi an environment of its own. A run killed halfway is converged by the next one: a state lock older than 3 hours is cancelled, and a create Pulumi never saw finish is imported or dropped.
- **Deployer roles.** On `ssc-cells`: `cloudsql.admin`, `compute.instanceAdmin.v1`, `compute.networkUser`, `iam.serviceAccountUser`, `resourcemanager.tagUser`, `run.admin`. In `ssc-platform-0`: `storage.objectAdmin` on the state bucket, `cloudkms.cryptoKeyEncrypterDecrypter` on the secrets key, `serviceusage.serviceUsageConsumer`. Also the public-invoker tag (above), the apps zone's record writer, and in each cell the custom role `sscDeployerRecords` (record permissions only) on the zone `ssc-sql` alone, which the cell stack grants at onboarding so the `database` flag can write the database's record. No billing, project-creation, deny-rule, IAM or secret-value role.
- **The control plane's part.** The worker, which runs the cell jobs, holds `run.jobsExecutorWithOverrides` and `run.viewer` on that one job, nothing more: `ssc-control-worker` of each control project (SSC-064; it was `ssc-control` before). It starts a run with exactly two arguments, a cell label and a resource, and reads how the run went. Worker settings: `SSC_CELL_DEPLOYER=cloud_run` and `SSC_CELL_DEPLOYER_JOB=projects/ssc-platform-0/locations/us-central1/jobs/ssc-cell-deployer` (unset means none, and a lazy resource fails with `CELL_DEPLOYER_UNAVAILABLE`; `fake` only in `dev` and `test`).
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
- **Identity.** `ssc-secret-intake` holds `secretmanager.secretVersionAdder` on `ssc-a-*` secrets (IAM condition) and `logging.logWriter`, nothing else, and is named in the cell's deny rule, so it cannot read a version back.
- **Control plane.** Its `secretVersionAdder` in the cell (`control-secret-versions`) is removed; it holds no project role in a cell now.
- **Agent.** `secretmanager.admin` on `ssc-a-*`, unchanged: it creates each secret, sets its policy, adds the database's versions (SSC-040) and deletes the secret of a database it drops (SSC-042). The deny rule refuses it every value.

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
- **Name and certificate.** `serverCaMode: GOOGLE_MANAGED_CAS_CA`, so the server certificate names the instance's DNS name and apps can use `sslmode=verify-full` against `listServerCas`. The name is under `sql.goog.`, which the `*.goog.` rule would sinkhole, so at onboarding the cell gets the private zone `ssc-sql` and the bypass `*.sql.goog.` (both in the floor, the same with every flag), and the flag adds the record `sql-dns`: the instance's `dnsName` as A to its private address, in the database range apps may already reach. Nothing else opens.
- **`cell_diff`.** The instance's `dnsName` and private address are cloud-assigned, so they become `<sql-dns>` and `<sql-address>` wherever they appear, as the entry address does.

**Live checks** (operator, not run by this change), on a staging cell with `database` on:
1. Custom roles may hold `cloudsql.instances.executeSql` and `cloudsql.instances.listServerCas`, and `executeSql` with `autoIamAuthn` needs no more than the permissions above (in particular not `cloudsql.instances.connect`).
2. `operations.get` on the `databases.insert` and `databases.delete` operations needs only `cloudsql.instances.get`, and a database drop succeeds with `cloudsql.databases.delete`.
3. `executeSql` runs on this private-IP-only instance with the Data API allowed.
4. With `GOOGLE_MANAGED_CAS_CA` the instance has a `dnsName` (`gcloud sql instances describe ssc-cell --format='value(dnsName,dnsNames)'`) and its certificate names it; Cloud SQL does not publish that name itself for private services access (if it does, the record here only repeats it).
5. From an app: the name resolves to the instance's private address, another `*.sql.goog` name gets NXDOMAIN, and `psql "sslmode=verify-full"` with `DATABASE_CA` connects.
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

- **Settings.** All four or none; anything else fails the stack before apply.
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

## Audit anchors

Each org's daily audit anchor goes to its cell's bucket, beside the snapshots, under `audit-anchors/<org>/` (SSC-012, decision 012 amendment). The worker is the only account that may change them: it holds `storage.objectUser` on the cell bucket (`bucket-control-worker`), the API's grant there (`bucket-control`) is gone because no API code uses a cell bucket, the gateway and the agent hold `storage.objectViewer`, and no other cell account has a storage role. The cells folder enforces `iam.automaticIamGrantsForDefaultServiceAccounts` (Organisation policies), so no default account of a new cell project gets `roles/editor`. The retention lock is Step 5 and cannot be a bucket lock, because `latest.json` is replaced on every compile. Live checks for the proof run, on a staging cell whose org has events, run as the operator with the worker's settings: `SSC_DATABASE_DSN` through `cloud-sql-proxy` (runbook SSC-064), `SSC_ENV=staging`, `SSC_BLOB_BACKEND=gcs`, `SSC_BLOB_BUCKET`, `SSC_BLOB_SIGNER` and `SSC_CELL_BUCKET_TEMPLATE=ssc-c-{cell}-cell`, with credentials that may use the cell bucket (the just-in-time grant on the cells folder):

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

The cell stack exports `entry_address`, `public_host_suffix` (`<label>.delimitusapps.com`), `certificate_id`, `agent_host`, `agent_url`, `intake_host` and `intake_url`. `agent_url` is `https://ssc--agent.<label>.delimitusapps.com`, the URL the control plane calls and the audience of its ID token; it was the agent's `run.app` URL before SSC-095. The platform stack sets `SSC_CELL_AGENT_URL` (worker and API) to it from `cell_label` (SSC-064); set the GitHub variable `SSC_PROBE_AGENT_URL` (nightly) to it. `intake_url` is `https://ssc--secrets.<label>.delimitusapps.com`, which the platform stack sets as `SSC_SECRET_INTAKE_URL` (API) the same way.

**Zones and the registrar step.** The platform stack owns two public zones in `ssc-platform-0`: `delimitusapps` (`delimitusapps.com.`, the cells' records) and `delimitus` (`delimitus.com.`, for `api`, `auth` and `keys`, whose A records the control plane's entry writes). Both are protected from deletion. A cell stack runs as the operator, who may write records in the apps zone only, through the custom role `sscZoneRecords` granted on that zone. After `pulumi up --stack platform`, the founder sets each domain's name servers at the registrar from `pulumi stack output apps_zone_name_servers` and `platform_zone_name_servers`, and deletes the domain's DS records there. Both domains still carry DS records from zones that no longer exist; with them left in place, validating resolvers fail every lookup and no certificate is issued. The `.com` delegation is cached for up to 48 hours, so do this well before the first cell.

**DNSSEC** is off on both zones for now. Turning it on later is a zone setting plus a new DS record at the registrar.

**Certificate issue time.** Google issues the certificate once the authorisation CNAME resolves publicly, usually within minutes; the done-when allows 30 minutes from the record. Until then the HTTPS rule answers with a TLS failure. `entry_probe` waits up to 30 minutes and prints how long it took.

## Control plane

The platform stack runs the control plane in `ssc-control-<stage>` for each stage named in `control_stages` (SSC-064, `ssc_infra/control.py`). The live runbook is `docs/runbooks/ssc-064-control-plane.md`.

- **Settings** (platform stack config):
  - `control_stages`: a list, `["prod"]`, `["staging"]` or both. Unset or empty: no control plane, only the staging project and its identities, as before. `ssc-control-prod` is made only once `prod` is named.
  - `public_stage`: the stage that holds `api`, `auth` and `keys.delimitus.com`; defaults to `prod` when named, else `staging`. One stage at a time: the three hosts have one A record each.
  - `control_image`, `auth_jwks`, `auth_signing_kid`: the release, all or none. `control_image` is a build of `packages/ssc_control/Dockerfile` pinned by digest in `ssc-platform`; `auth_jwks` is the auth host's public JWKS and must hold `auth_signing_kid`. Until they are set every service runs the placeholder image with no settings, the worker pool runs no instance and there is no migration job.
  - `cell_label`, `cell_jwks`: the one cell until placement, both or neither. `cell_jwks` is that cell stack's `identity_jwks` output. Every per-cell setting derives from the label through `naming`: the agent and intake URLs, the cell bucket and the issuer `https://keys.delimitus.com/<label>`. Only the stage the cells trust gets them, on its API and worker (the public stage; with none, every stage), because a cell grants nothing to another stage's accounts (`ControlConfig.serves_cells`). The cell's `sql_instance` output is not used: the control plane talks to the cell's database only through the agent, and the agent's own `SSC_SQL_INSTANCE` is the cell stack's.
  - `worker_instances`: the worker pool's instance count, 1 by default.
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

  Services read `latest` at start, so a new version needs a new revision. The cell deny rule names all four accounts of every control project.
- **Blobs.** The private bucket `ssc-control-<stage>-blobs`, with signed URLs only. The API and the worker each hold `storage.objectUser` there and sign as themselves (`iam.serviceAccountTokenCreator` on their own account).
- **Entry**, in the public stage only. A global external Application Load Balancer on the address `ssc-control-entry`, with HTTPS on 443 and a redirect on 80. The SSL policy requires TLS 1.2 and the `MODERN` profile. The URL map sends `api` to `ssc-api`, `auth` to `ssc-auth`, and `keys` to the backend bucket `ssc-keys`. One Google-managed certificate names the three hosts. The three A records go in the `delimitus` zone. The two services' invoker is `allUsers`; the `ssc-platform` folder does not carry the cells' members policy.
- **Keys.** `keys.delimitus.com/<label>/jwks.json` is the object `<label>/jwks.json` in the bucket `ssc-control-<stage>-keys`, whose objects are public. It is written from `cell_jwks`, which the stack refuses if it has a private member, and is served with `Cache-Control: public, max-age=300`. The gateway reaches `auth` and `keys` through the cell's two bypass rules and its NAT.
- **Who calls the cells.** Two accounts. The API calls the agent and mints intake grants, so the intake's `SSC_CONTROL_SA` stays `ssc-control`. The worker calls the agent for runtime, builds and databases, uses the cell bucket and starts the deployer. The cell stack reads `control_workers` from the platform stack and grants the worker `run.invoker` on the agent (`agent-invoker-worker`) and `storage.objectUser` on the cell bucket (`bucket-control-worker`); the API has no grant on the cell bucket (SSC-012). So apply the platform stack before the cells. A cell trusts the accounts of `control_public_stage` whatever its own stage, because that control plane serves its gateway's login and keys; with no public stage, its own stage's (`cell.control_for`). Re-apply each cell after the public stage first appears or moves.
- **Outputs.** The stack exports:
  - `control_service_accounts` (the API's), `control_workers` and `control_accounts`, per stage;
  - `control_sql_instances`, the connection names;
  - `control_public_stage` and `control_entry_address`, once a public stage exists.
- **Cost a month.** Prod, with the entry: worker $30, Cloud SQL about $10, entry $18.25, the API's idle minimum instance about $10, secrets $0.42, so about $70 against $75. Staging without the entry is about $40 against $40. With the entry it is about $59, so keep the entry in prod. Set `worker_instances` to 0 to stop a stage's worker.

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
