# SSC-086 proof-run kit

One command per proof from SSC-086 (architecture document, section 8). Each command ends in one line, `T<n> <number> PASS|FAIL|INCOMPLETE`, appends the full result to `results/t<n>.json` and exits 0, 1 or 2. Copy the final lines into `RESULTS.md`.

Steps marked **[real]** create, change or delete a real resource, as in the SSC-064 runbook. Run them in order, each only after the founder has agreed. Every other step only reads. The kit itself never creates anything: its commands read with `gcloud ... describe/list`, `ssc status`, HTTP requests to the cell's public hosts, and Cloud Monitoring. There are five exceptions, and each is called out where it happens: `t6 nat` and the stand-in `t6 proxy` execute an existing Cloud Run job (`t6 egress` only reads), `t8` runs `ssc disable`, `t3 nightly`/`t4` run the existing nightly, which deploys the probe apps, and `timers --disable` and `files --disable` run `ssc disable` and `ssc enable`.

## Setup

```sh
cd spikes/proofrun && uv sync        # the kit; its tests: uv run pytest
cd ../.. && uv sync --all-packages   # the repository, for `uv run ssc` and `ssc_infra`
```

Run the kit from `spikes/proofrun` as `uv run python -m proofrun <command> ...`. `--help` on any command lists its arguments. Nothing is hardcoded: labels, projects, hosts and slugs are arguments, and a few settings come from the environment:

| Setting | Meaning |
| --- | --- |
| `PROOFRUN_SSC` | how to start the CLI (default `uv run ssc`, at the repository root) |
| `PROOFRUN_COOKIES` | the cookie jar (default `~/.ssc-proofrun/cookies.json`, mode 600) |
| `PROOFRUN_RESULTS` | the results folder (default `spikes/proofrun/results`, git-ignored) |
| `PROOFRUN_BILLING_ACCOUNT` | T12's billing account, if not passed as `--billing-account` |

Every argument and every `SSC_*`, `PROOFRUN_*` and `CLOUDSDK_*` setting is checked against a fence before anything runs. The fence holds only a digest of the one project id the kit must never touch.

The examples below use these shell variables:

```sh
L1=<label 1>; L2=<label 2>          # 8 to 16 lower-case letters and digits, letter first; new labels
P1=ssc-c-$L1; P2=ssc-c-$L2          # project ids
N1=$(cd infra && pulumi stack output --stack c-$L1 project_number); N2=<the same for c-$L2>
NAT1=$(cd infra && pulumi stack output --stack c-$L1 nat_ip)
```

**Session cookie.** The public hosts need a session cookie. After a browser login (SSC-064), copy the `__Host-ssc-session` value from the browser's devtools into the jar with `uv run python -m proofrun cookie set <host>`. The value is read without echo and is never printed again. Without a real login (T2/T3 fallback), seal one with the cell's own keyring. Do this at the repository root:

```sh
gcloud kms decrypt --key "$(cd infra && pulumi stack output --stack c-$L1 gateway_kms_key)" \
  --ciphertext-file=<(base64 -d keyring.sealed) --plaintext-file=- \
  | uv run python spikes/proofrun/seal_cookie.py --host <slug>.$L1.delimitusapps.com --org <org_id> --sub <usr_...> --keyring -
```

Results taken with a sealed cookie say the real login was not exercised. One cookie is kept per host, valid for at most 12 hours, so refresh it during T9. The 12 hours run from the browser's login at `auth.delimitus.com`, not from when the cookie was copied: a cookie taken later in the same browser session ends with that session. So sign in again in a fresh private window before each refresh.

## Probe apps

Everything under `apps/` deploys through `ssc deploy`. Create each app once with `uv run ssc apps create <slug>`, then run `uv run ssc deploy --app <slug> <folder> --wait` at the repository root **[real]**. An app's owner gets no grant (decision 019), so give the signed-in person preview access to each app with `uv run ssc share <slug> <usr_…> --env preview`, or the gateway answers 404 `not_granted`. The kit always reads the `preview` environment. `ssc deploy` places apps in cell 1 only, because the control plane serves one cell (`cell_label`).

| Slug (example) | Folder | Used by |
| --- | --- | --- |
| `proba`, `probb` | `uv run python -m proofrun stage-probe /tmp/probe` (the conformance probe app with the kit's manifest) | T2, T3 public, T10 |
| `pstatic` | `apps/static` | T7 |
| `papi` | `apps/api` (`/vpc`, `/ws`, `/deny`) | T7, T8, T10, T11 |
| `pstream` | `apps/streamlit` (the bake-off's workload) | T7, T9 |
| `pg01` to `pg10` | `apps/pg` (`[state] postgres = true`) | T5 |
| `pegress` | `apps/egress` (`/egress?host=&credentials=`, through the app's own `HTTPS_PROXY`) | T6 |

The kit bypasses `ssc deploy` in two places:

- **Cell 2's probe apps.** They come from the nightly (`t3 nightly` against cell 2), because the control plane does not serve cell 2.
- **T6's NAT job.** It needs a chosen service account, subnet and network tag, and no SSC app can have those. The proxy leg needs no job: it runs from the `pegress` app.

## Order, cost and teardown

Costs are rough list prices. The two cells cost about $40 for the week together (ticket). On its own, the empty cell is about $23 a month, $0.75 a day, with NAT from onboarding. The full cell is about $43 a month, $1.40 a day. The table lists only what each step adds on top.

| # | Step | Adds | Teardown |
| --- | --- | --- | --- |
| 1 | T1 create both cells **[real]** | the cells themselves | cell 2 in T12; cell 1 stays as the probe cell |
| 2 | SSC-064 runbook **[real]** (`docs/runbooks/ssc-064-control-plane.md`) | its own budget (about $70 a month for prod) | none: it stays |
| 3 | Probe apps in cell 1 **[real]** | cents (minimum 0) | `ssc apps` delete when done; cell 1 keeps them as probes |
| 4 | T2 | none | none |
| 5 | T3 nightly against cell 2, then cell 1; T3 public | cents | restore the nightly schedule (part of T3's pass) |
| 6 | T4, then T11 (after SSC-095's policies) | cents | none |
| 7 | T1 cost (needs 2 whole days of cell 2 on the bill) | none | none |
| 8 | No-internet floor live check on cell 2 **[real]** (optional, it turns on cell 2's flags) | under $1 | destroyed with cell 2 |
| 9 | T12 destroy cell 2 **[real]** | saves about $0.75 a day | none |
| 10 | T5 ops, cross, restore clone **[real]** | clone about $0.02 an hour | `gcloud sql instances delete ssc-cell-restore --project=$P1` **[real]** |
| 11 | T6 NAT job and the org allowlist entry **[real]** | under $0.10 | below |
| 12 | T7 alone, about 9 hours, nothing else calling cell 1 | about $0.40 (the Streamlit instance lives up to 15 min a sample) | none |
| 13 | T8 kill drill **[real]** | cents | `uv run ssc enable papi` **[real]** |
| 14 | T10 gateway override **[real]** | cents | `pulumi up --stack c-$L1` **[real]** |
| 15 | instance_count check (20 minutes) | cents | none |
| 16 | T9 instance-billed 24 h, then request-billed 24 h **[real]** | about $1.64 + $2.18 for the app, plus about $4.40 for the gateway, which bills while it relays the stream (48 h at $0.0909) | below |

Run T9 last, because its request-billed leg pauses the worker.

## The proofs

### T1: two cells from the amended stack **[real]**

1. In `infra/`, bootstrap both cells: `uv run python -m ssc_infra.bootstrap cell $L1 --probe`, then the same for `$L2`. This writes `Pulumi.c-<label>.yaml`.
2. Set each stack's config from `configs/Pulumi.c-CELL1.example.yaml` (every flag on) and `configs/Pulumi.c-CELL2.example.yaml` (every flag off). Do it key by key with `pulumi config set --stack c-<label> <key> <value>`.
3. Run `pulumi up` on each stack. This first run creates the gateway's key.
4. Seal each cell's keyring as in `infra/README.md` (Gateway, live steps), set the four gateway settings, and run `pulumi up` again. Build the build images first if needed (`infra/README.md`, Builds).
5. Run `pulumi refresh --stack c-$L1` and the same for `c-$L2` (read-only against the cloud, it updates state).

```sh
uv run python -m proofrun t1 diff $L1 $L2
```

Pass: at least one resource compared, 0 differences and no policy override. `cell_diff` leaves out what differing flags name and says which. The output also shows the policy table each cell inherits.

Two days later, read cell 2's cost before credits for whole days. Use Billing, Reports, filtered to the project, grouped by project, with credits unticked. Then run:

```sh
uv run python -m proofrun t1 cost --usd <cost> --days <whole days>
```

Pass: under $1 a day. Run it for cell 1 too and read its "implies $X a month" line against decision 006's pending A7: full cell under $50.

Record the subnet layout: `gcloud compute networks subnets list --network=ssc-cell --project=$P1 --format='table(name,ipCidrRange,purpose)'`.

### T2: load balancer, certificate, public host

```sh
uv run python -m proofrun t2 --label $L1 --project $P1 --project-number $N1 --zone-project ssc-platform-0 --app proba
```

The command checks three things:

- the certificate is `ACTIVE`, and how many minutes it took from the Cloud DNS change that added `_acme-challenge.$L1`;
- `entry_probe` passes;
- the probe app answers 200 on its public host with the jar's cookie.

Pass: all three, with the certificate within 30 minutes. With a sealed cookie the result says so.

### T3: the 14 probes, gateway at minimum 0

```sh
uv run python -m proofrun t3 nightly --project $P2 --agent-url https://ssc--agent.$L2.delimitusapps.com --digest sha256:<probe digest>
uv run python -m proofrun t3 nightly --project $P1 --agent-url https://ssc--agent.$L1.delimitusapps.com --digest sha256:<probe digest>
uv run python -m proofrun t3 public --project $P1 --project-number $N1 --app proba --peer-app probb
```

Before the first `nightly`, each cell needs the probe image and the probe-runner job. The image is `conformance/runtime/probe_app/Dockerfile`, which holds the runner; proba's build leaves the runner out, so its image fails with "can't open file '/app/runner.py'". Build it with `docker buildx build --platform linux/amd64 --provenance=false --sbom=false -t <cell 1 repo>:nightly-probe --push conformance/runtime/probe_app`, where a cell's repo is `us-central1-docker.pkg.dev/<project>/ssc-apps/apps`, then copy it to cell 2 with `docker buildx imagetools create --tag <cell 2 repo>:nightly-probe <cell 1 repo>@<digest>`, which keeps the digest. Then set `probe_digest` to it on both cell stacks and apply the five resources the preview adds: `probe-runner`, `probe-runner-nightly`, `nightly-logs`, `nightly-run-viewer` and `bucket-nightly-snapshots`. Without the job, the nightly stops with 404 on `ssc-probe-runner:run`.

`nightly` runs the existing nightly (`ssc_conformance.nightly`, which deploys the probe apps) without peer settings. `public` runs the same runner's probes through the public host and the gateway. An answer from the app shows that Cloud Run accepted the gateway's ID token for the app's `run.app` URL, the check SSC-018 left open. `cannot_reach_peer_cell` is T4's.

Pass: 14 of 14, and the gateway's minimum is 0 on both the template and the service.

Then restore the schedule. Uncomment `schedule` in `.github/workflows/nightly.yml`, and set the repository variables `SSC_PROBE_PROJECT`, `SSC_PROBE_AGENT_URL` and `SSC_PROBE_DIGEST` to cell 1 **[real]**.

### T4: `cannot_reach_peer_cell`

```sh
uv run python -m proofrun t4 --project $P1 --agent-url https://ssc--agent.$L1.delimitusapps.com --digest sha256:<probe digest> --peer-project-number $N2
```

The nightly against cell 1 dials three things in cell 2:

- probe app `a`'s `run.app` host, which `t3 nightly` made there;
- the gateway's `run.app` host;
- the apps range.

Each attempt is sorted as one of these:

- `network`: no connection;
- `ingress`: Cloud Run's 404, before IAM;
- `iam`: 401 or 403, which means the network let the call through;
- `peer`: the peer answered itself;
- `answered`: any other answer.

Pass: every app and gateway attempt is `network` or `ingress`. The range leg is not applicable, because every cell has the same address plan. The probe says so.

### T5: the database

```sh
uv run python -m proofrun t5 ops --project $P1
uv run python -m proofrun t5 cross --app pg01 --other pg02 --other pg03 ... --other pg10
```

`ops` reads Cloud SQL's operation log. Pass: the instance in under 15 minutes, and at least ten databases at under 1 minute each.

`cross` asks `pg01` to connect to each other app's database. Pass: its own database connects, and every other one is refused with a privilege or login error (42501, 28000, 28P01). The `postgres` maintenance database is reported but not counted.

Restore drill **[real]**:

```sh
gcloud sql instances clone ssc-cell ssc-cell-restore --project=$P1
```

Then time it:

```sh
uv run python -m proofrun t5 ops --project $P1 --restore-instance ssc-cell-restore
```

Write the restore up in RESULTS.md, then delete the clone.

### T6: NAT and the proxy

The NAT leg uses a stand-in job in the data gateway's place, because the job needs a service account, subnet and network tag that no SSC app can have. It is no evidence for SSC-050. The proxy leg runs against the real `ssc-egress` proxy, from the `pegress` app, so an app's own `HTTPS_PROXY` credential is what is tested. Set up **[real]**. Run the `docker` and `gcloud` lines at the repository root and the kit's lines in `spikes/proofrun`:

```sh
REG=us-central1-docker.pkg.dev/$P1/ssc-apps
docker buildx build --platform linux/amd64 --provenance=false --push --tag $REG/proofrun-egress:1 spikes/proofrun/standins/egress
gcloud run jobs create proofrun-egress-nat --project=$P1 --region=us-central1 --image=$REG/proofrun-egress:1 \
  --service-account=ssc-data@$P1.iam.gserviceaccount.com --network=ssc-cell --subnet=gateway \
  --network-tags=ssc-data --vpc-egress=all-traffic --max-retries=0 --task-timeout=120s
```

Create and deploy `pegress` as under Probe apps (`uv run ssc apps create pegress`, `uv run ssc deploy --app pegress spikes/proofrun/apps/egress --wait`, then `uv run ssc share`). The deployment issues the app's `HTTPS_PROXY`; in preview it needs no approval. Its preview capability diff shows `egress_host_missing`: the proxy only tunnels to hosts on the org's allowlist, and the manifest's `[egress] hosts` do not feed that list.

Allowlist step **[real]**: the founder, as an org admin, adds the allowed host through the API before the run, and `example.com` must not be on the list.

```sh
PUT /v1/egress/hosts/www.cloudflare.com
```

Read the cell's NAT log for the allowed host's connections (NAT logging is off in the stack, so turning it on for the run is a known difference from the stack until it is turned off again):

```sh
gcloud logging read 'resource.type="nat_gateway"' --project=$P1 --freshness=30m --format=json
```

```sh
uv run python -m proofrun t6 nat --project $P1 --nat-ip $NAT1      # executes the job: the data gateway's place
uv run python -m proofrun t6 egress --base https://pegress--preview.$L1.delimitusapps.com --nat-ip $NAT1   # reads only
```

`--base` is the preview host that `ssc status` shows for `pegress`; the cookie is taken from the jar by that host. `egress` calls the app three times: `www.cloudflare.com` with the credential, `example.com` with the credential, and `www.cloudflare.com` with none. Add `--allow` or `--unlisted` to use other hosts. A 403 for the allowed host usually means the allowlist step was not done.

Pass: `nat` leaves from the reserved NAT address. In `egress`, the allowed host tunnels (proxy 200, host 200) and reports the NAT address, the unlisted host is refused (403 expected; the number is recorded), and the call without the credential gets 407.

**Stand-in proxy, for a cell without `proxy_image` only.** Use `t6 proxy` and the stand-in Envoy instead of `egress`:

```sh
gcloud run jobs create proofrun-egress-app --project=$P1 --region=us-central1 --image=$REG/proofrun-egress:1 \
  --service-account=ssc-deny-probe@$P1.iam.gserviceaccount.com --network=ssc-cell --subnet=apps \
  --vpc-egress=all-traffic --max-retries=0 --task-timeout=120s
uv run python -m proofrun t6 envoy-config --envoy-image envoyproxy/envoy:<tag>@sha256:<digest> --out results/envoy-user-data.yaml   # in spikes/proofrun
INST=$(gcloud compute instance-groups managed list-instances ssc-proxy --zone=us-central1-a --project=$P1 --format='value(name)')
gcloud compute instances add-metadata $INST --zone=us-central1-a --project=$P1 --metadata-from-file=user-data=spikes/proofrun/results/envoy-user-data.yaml
gcloud compute instances reset $INST --zone=us-central1-a --project=$P1
uv run python -m proofrun t6 proxy --project $P1 --nat-ip $NAT1    # executes the job: an app's place
```

Never run `add-metadata` or `reset` on a cell with the real proxy: they replace the machine's user-data, which the real proxy needs. The stand-in's cloud-config points the machine's resolver at 8.8.8.8 over the NAT and starts stock Envoy with a fixed two-host CONNECT list on 10.20.4.10:3128. If the group recreates the machine before Envoy answers its health check, its name changes; repeat the last two `gcloud` lines. Pass for `proxy`: both allowed hosts tunnel and report the NAT address, and both unlisted hosts are refused.

Teardown **[real]**:

```sh
DELETE /v1/egress/hosts/www.cloudflare.com
gcloud run jobs delete proofrun-egress-nat --project=$P1 --region=us-central1
gcloud artifacts docker images delete $REG/proofrun-egress --delete-tags --project=$P1
```

For the stand-in proxy also delete `proofrun-egress-app` and run `gcloud compute instance-groups managed recreate-instances ssc-proxy --instances=$INST --zone=us-central1-a --project=$P1`. Delete `pegress` with `ssc apps` when done.

### T7: cold starts

```sh
uv run python -m proofrun t7 run --static pstatic --api papi --streamlit pstream
uv run python -m proofrun t7 report
```

The run takes 20 samples, 26 minutes apart, in about 8.7 hours. They alternate a `cold` series (gateway and app cold) and a `warm` series. In the warm series the cell's `www` host is asked first, and the gateway answers it itself, so that time is the gateway's own start. Each sample also reads the API app's Direct VPC egress delay from `/vpc`.

The run can be stopped and resumed with the same command. A sample cut off half way is thrown away, and the next one waits a full gap. A new run waits a full gap before its first sample; pass `--start-now` if the cell has already been idle that long.

Pass: every sample answered 200, and each median is within the bake-off's figure (4.5 s, 8 s, 22 s) plus the median gateway start.

Compare with Cloud Run's own figure, which is decision 025's reversal condition: `uv run python -m proofrun instances --project $P1 --service <ssc-a-...> --metric startup_latencies --minutes 600`.

### T8: kill drill **[real]**

Let the gateway and the app scale to zero first (20 minutes idle). This covers `infra/README.md`, Kill switch, check 1.

```sh
uv run python -m proofrun t8 --app papi --label $L1
```

The drill runs these steps:

1. Opens `wss://<host>/ws`, which ticks once a second.
2. Asks `/health` every 0.25 s.
3. Runs `ssc disable papi --json`.
4. Times, from the command's start:
   - the first refused `/health`;
   - the stream's end;
   - each step's `since_command_ms` from the `kill_switch.step` audit rows;
   - the change of `snapshots/<org>/latest.json` (the compile).

Pass: all of them within 10 s, with every step `done`. There is no query or tunnel leg, because there is no data gateway (SSC-054). Undo with `uv run ssc enable papi` **[real]**.

### T9: Streamlit held open 24 hours **[real]**

Instance-billed is the session app's normal setting:

```sh
uv run python -m proofrun t9 hold --state results/t9-instance.state.json --app pstream --project $P1 --mode instance
```

Request-billed needs an override. Pause the worker so nothing re-applies the service while it holds:

```sh
cd infra && pulumi config set --stack platform worker_instances 0 && pulumi up --stack platform
gcloud run services update <ssc-a-...> --project=$P1 --region=us-central1 --cpu-throttling
uv run python -m proofrun t9 hold --state results/t9-request.state.json --app pstream --project $P1 --mode request
gcloud run services update <ssc-a-...> --project=$P1 --region=us-central1 --no-cpu-throttling
cd infra && pulumi config set --stack platform worker_instances 1 && pulumi up --stack platform
```

`hold` refuses to start if the service is billed the other way. Each hold keeps `/_stcore/stream` open as a browser tab does. When the stream drops, the hold records why and reconnects at once, reading the cookie again. A refused reconnect, such as an expired cookie, is retried every 30 s. Refresh the cookie, after a fresh login, before the login's 12 hours are up. A stopped hold resumes with the same command.

The next day, read the service's usage amounts for the hold's day. Use Billing, Reports, filtered to the project and Cloud Run, grouped by SKU, with credits unticked. Then run:

```sh
uv run python -m proofrun t9 report --state results/t9-instance.state.json
uv run python -m proofrun t9 bill --state results/t9-instance.state.json --vcpu-seconds <n> --gib-seconds <n>
```

Pass: within 20 % of the hours held times $0.0684 (instance) or $0.0909 (request). Use usage amounts, not dollars, because the free tier hides the first vCPU-seconds of the month. `report` lists every drop and the length of the stream before it; the 60-minute drop is Cloud Run's request timeout.

### T10: gateway on gen1 at 0.5 vCPU **[real]**

```sh
gcloud run services update ssc-gateway --project=$P1 --region=us-central1 --execution-environment=gen1 --cpu=0.5 --concurrency=1
uv run python -m proofrun t10 --project $P1 --project-number $N1 --app proba --peer-app probb --ws-app papi
cd infra && pulumi up --stack c-$L1    # puts the gateway back
```

The stack has no setting for the gateway's generation or CPU, so T10 switches it with this override rather than a code change. Cloud Run requires concurrency 1 below 1 vCPU. That means one gateway instance per open session, so the cost per session hour is the gen1 figure times the open sessions; the command prints both figures. Apps stay on gen2.

Pass: the gateway is gen1 at 0.5, 14 of 14 probes pass, and the WebSocket carries 5 ticks.

### T11: IAM refusal across cells

Run it after SSC-095's policies are in force, and before T12.

```sh
uv run python -m proofrun t11 --app papi --peer-label $L2
```

The API app asks, with its own identity, for cell 2's secret `ssc-a-probe` and for one object of cell 2's bucket. Each leg is sorted:

- `iam`: 401 or 403;
- `network`: no answer;
- `not_found`: 404, which proves nothing;
- `allowed`: a breach.

Pass: both legs are `iam`. The operator's side of the same check is `uv run python -m ssc_infra.deny_probe $L1`. Also run the "Policies, live (SSC-086 T11)" list in `infra/README.md` **[real]**: each command there must be refused.

### T12: delete cell 2 and watch the slot **[real]**

```sh
export PROOFRUN_BILLING_ACCOUNT=<billing account id>
uv run python -m proofrun t12 --project $P2 --once           # before: the starting count is on record
cd infra && pulumi destroy --stack c-$L2
uv run python -m proofrun t12 --project $P2                  # every 10 minutes, up to 12 hours; resumable
```

Pass: once cell 2 is `DELETE_REQUESTED`, the linked count is back to 4 on the same UTC day. Record the times here and in SSC-089.

If the count is not back that day, decision 026's rule stands: unlink before deleting. The fallback, all **[real]**:

1. `gcloud projects undelete $P2`
2. `gcloud billing projects unlink $P2`
3. `gcloud projects delete $P2`

The account is printed masked.

### Timers (GA-4.1)

The app is `apps/timer`: `/health` answers at once, and `/tick` verifies the identity note, prints `TICK role=<role> at=<iso> method=<method>` (or `TICK refused=<code>`, with 401) and records it. Its `ssc.toml` declares one schedule, `minute` (`* * * * *`, UTC, `POST /tick`, 60 s timeout). Deploy it to prod, with everything at zero first: `ssc deploy`, then `ssc promote`. Preview schedules are stored paused, so a preview deploy fails the first phase. Then, from `spikes/proofrun`:

```sh
uv run python -m proofrun timers --app <slug> [--env prod] [--minutes 4] [--disable]
```

It lists the schedules (it stops at once unless `minute` is `active`), then reads the schedule's runs from the control API every 10 s until two scheduled runs have succeeded or `--minutes` are up, and prints them newest first. Runs scheduled before the command started are left out of the table and every check; one line says how many. The checks:

| # | Check |
| --- | --- |
| 1 | Two scheduled runs succeeded with HTTP 200. |
| 2 | The two latest of them are exactly 60 s apart (`scheduled_for`). |
| 3 | No two runs overlap (started to finished), and none has error `overlap`. |
| 4 | Each of the two started at most 15 s after its `scheduled_for`; the numbers are printed either way. |
| 5 | Both have `start_ms` and `duration_ms`: the history shows the start apart from the call. |
| 6 | `ssc logs --source app` shows at least two `TICK role=schedule` lines and no `TICK refused=` line. The app prints a tick only after the gateway admitted the `SSC-Schedule-Token` and the note verified. Read up to three times, 15 s apart, for log lag. If the logs cannot be read the check is "not read", the command prints the `ssc logs` line to run by hand, and the proof ends INCOMPLETE. |
| 7 | With `--disable` **[real]**: after `ssc disable`, `minute` is `paused` with `pause_reason` `app_disabled`. |
| 8 | With `--disable` **[real]**: after `ssc enable`, `minute` is `active` with `next_run_at` set. |

`--disable` **[real]** runs `ssc disable <slug>` and, always, `ssc enable <slug>`; it reads the schedules for up to 60 s after each command and prints how long each took. If `ssc enable` fails it prints `undo: ssc enable <slug>`. Apart from those two commands and `ssc logs`, the command only reads the control API, with the operator's login, which is never printed or saved.

Pass: every check that ran passed. The schedules and runs read, and the check results, go to `results/ga-4.1.json` and to `results/timers-<app>-<UTC stamp>.json`. Copy the final line into the GA-4.1 record.

### Files (GA-4.2)

The app is `apps/files`: a FastAPI app with `[files]` in its `ssc.toml` that puts, gets, links and deletes through `ssc_app.files` (a vendored copy of `packages/ssc_app`'s `files.py` and `workload.py`), and a `/files/loop` that asks for a `put` link every second. It needs a cell with `connections` on (the first such deploy creates the data gateway, a few minutes). Deploy it to preview, with `uv run ssc deploy --app <slug> spikes/proofrun/apps/files --wait`, give the signed-in person preview access, and put the host's session cookie in the jar (Setup). Then, from `spikes/proofrun`:

```sh
uv run python -m proofrun files --app <slug> --label $L2 [--env preview] [--wait-expiry] [--disable]
```

It puts 4 KiB of random bytes as `ga42/<UTC stamp>.bin` and reaches Cloud Storage from the laptop with the signed link alone, no cookie. The checks:

| # | Check |
| --- | --- |
| 1 | put: `POST /files/put` answers `ok: true` with the right size. |
| 2 | get by link: the `get` link answers 200 with the same bytes and a `Content-Disposition` that starts with `attachment`, and its `X-Goog-Credential` starts with `ssc-data@ssc-c-<label>.iam.gserviceaccount.com`. `expires_at` is printed. |
| 3 | prefix isolation: the same link with `files/<this env id>/` in its path changed to `files/<the other env id>/` answers 403 `SignatureDoesNotMatch`. The other environment need not be deployed; only its id is used. |
| 4 | With `--wait-expiry`: 30 s after the link's `X-Goog-Date` plus `X-Goog-Expires`, the link answers 400 `ExpiredToken`. This takes about 11 minutes. Without the flag it is "not run". |
| 5 | With `--disable` **[real]**: see below. Without the flag it is "not run". |
| 6 | clean-up: `POST /files/delete` answers `ok: true`. |

`--disable` **[real]** gets a fresh `get` link, starts `/files/loop?seconds=90`, runs `ssc disable <slug>` and, always, `ssc enable <slug>` (if that fails it prints `undo: ssc enable <slug>`). The loop runs in a thread on the app, so it keeps writing `LOOP at=<time> result=<ok|CODE>` lines to the log even if the gateway cuts the stream; the kit holds at least 20 s after the disable before it enables, so the log has lines from the suspended spell. Its sub-checks:

- 5a: at least one `LOOP ... result=APP_NOT_ACTIVE` line is in the stream, else in `ssc logs --source app` (read up to three times, 15 s apart, after enable). It prints that line's time, the disable command's start and the difference, which includes clock skew between the laptop and Cloud Run. If no loop line is found anywhere the proof ends INCOMPLETE; if lines were found but none was refused it fails.
- 5b: the disclosure. The link fetched before the disable still answers 200 afterwards; links live up to 10 minutes after a disable. It is printed either way and passes on 200.
- 5c: a new put after `ssc enable` answers `ok: true` (up to five tries, 5 s apart, while the snapshot catches up and the app starts).

Pass: every check that ran passed. The checks, the link's path, credential, date and lifetime (never its signature or the operator login), the loop lines and the timings go to `results/ga-4.2.json` and to `results/files-<app>-<UTC stamp>.json`. Copy the final line into the GA-4.2 record. This covers `infra/README.md`, File storage, live checks 3, 4, 6 and 8.

### Rollback warning (GA-4.5)

The app is `apps/rollback`: a FastAPI app with `[state] postgres = true` and an alembic ledger in `alembic/versions` (`0001_ga45_first.py`, `0002_ga45_second.py`). The platform reads the migration names from the source and never runs them, and the app does not run them either; `/` prints the names its release carries. The kit makes two releases from the one folder: R1 from a temporary copy without `0002_ga45_second.py` (removed afterwards), R2 from the whole folder. Only preview is used, because `ssc deploy` always targets preview, so the command has no `--env`.

Before the first run:

1. Create the app once: `uv run ssc apps create <slug>` (at the repository root).
2. Give the person who will check the console preview access (`uv run ssc share <slug> <usr_…> --env preview`), as under Probe apps.
3. For the MCP surface, keep an agent login on this machine: `uv run ssc login --org <org id> --agent ga45` (or set `SSC_TOKEN` to an agent's token). `ssc mcp` refuses a person's token. Without it check 5 is "not read" and the command prints this line. The audit export needs the org admin's own login.

From `spikes/proofrun`:

```sh
uv run python -m proofrun rollback --app <slug> [--console-url https://console.delimitus.com]
```

The kit prints the app and the four **[real]** steps, then goes on without asking. A first run creates the app's database, so the first deploy may take several minutes (its `--timeout` is 1200 s, the others 900 s); expect roughly 15 to 30 minutes in all.

| # | Check |
| --- | --- |
| 1 | **[real]** deploy R1 (0001 only): healthy. |
| 2 | **[real]** deploy R2 (0001 and 0002): healthy, with a higher release number. |
| 3 | `ssc rollback <slug> R1` is refused: exit non-zero, `Code: SCHEMA_AHEAD`, and the `Fix:` line names `0002_ga45_second` (and not `0001_ga45_first`). |
| 4 | The same with `--json`: `error.code` is `SCHEMA_AHEAD`. The names are not in the JSON (`CliError.fix` is text-only, `errors.py:104-106`); the output and the results say so in a `note:` line. |
| 5 | `ssc mcp` over stdio (newline-delimited JSON-RPC, 60 s limit): `initialize`, `tools/list` has `rollback`, and `tools/call rollback` without `confirm` answers `isError` with `SCHEMA_AHEAD` and `0002_ga45_second` in its text and in `structuredContent.error.detail`. |
| 6 | `ssc audit export --since <start>` holds exactly one `rollback.started` row for R1 on this environment, the confirmed one: the refusals wrote none. |
| 7 | **[real]** `ssc rollback <slug> R1 --confirm --wait`: healthy on R1. |
| 8 | That row says `confirmed: true` and `migrations_ahead` contains `alembic:0002_ga45_second`; its actor and action are printed. |
| 9 | **[real]** `ssc rollback <slug> R2 --wait` needs no `--confirm` and is healthy. It puts R2 back, so R1 can be picked in the console (the live release cannot). |
| 10 | Console, manual: open `<console-url>/apps/<app id>`, on the Preview card press Roll back, pick R1, type the slug and press Roll back. Do not tick the checkbox; press Cancel. You must see "The database may have run migrations R<n> does not have" and a list item `0002_ga45_second (alembic)`, with "Roll back anyway" off. Record what the page showed. |

Pass: every automatic check (1 to 9) passed; check 10 never sets the verdict. The kit never reads the operator's token. The checks, the MCP reply, the audit row and the note go to `results/ga-4.5.json` and to `results/rollback-<app>-<UTC stamp>.json`. Copy the final line, and what check 10 showed, into the GA-4.5 record.

### Secrets (GA-4.6)

The app is `apps/secrets`: a FastAPI app with no database whose `/secret` answers only a fingerprint of the environment variable `GA46_TOKEN` (the first 12 hex digits of the SHA-256 of its bytes, and its length), never the value. A secret's name is the environment variable the app reads, and no manifest key is needed. The kit makes two random values in memory, hands each to `ssc secret set` on stdin only (the CLI takes no value on the command line), and keeps only their fingerprints. Only preview is used (`ssc deploy` always targets preview), so `--env` accepts `preview` alone.

Before the first run:

1. Create the app once: `uv run ssc apps create <slug>` (at the repository root). It needs no deploy first: `ssc secret set` before any deployment only stores the version (`operation_id` null), and the kit then runs `ssc deploy` of `apps/secrets`. With a live deployment already, `ssc secret set` starts the deployment itself.
2. Give the signed-in person preview access, and put the host's session cookie in the jar (Setup). A login redirect on `/secret` fails check 3 with the `cookie set <host>` line.
3. For checks 9 and 10, be logged in to `gcloud` as an operator who can read the cell's deny policy and the secret's IAM policy. Both are read-only. If they are refused the checks are "not read" and the kit prints the command to run by hand.

From `spikes/proofrun`:

```sh
uv run python -m proofrun secrets --app <slug> --label $L2 [--env preview] [--control-sa <SSC_CONTROL_SA>]
```

The kit prints the **[real]** steps and goes on without asking. Expect 5 to 20 minutes.

| # | Check |
| --- | --- |
| 1 | **[real]** `ssc secret set GA46_TOKEN --wait --timeout 900` with v1 on stdin: `changed`, a numbered version. |
| 2 | **[real]** v1 is live: the set's own deployment (when something was live) or `ssc deploy --wait --timeout 1200` ended healthy. |
| 3 | `GET /secret` through the app's host with the cookie (up to 6 tries, 10 s apart): v1's fingerprint and length, and no value in the answer. |
| 4 | `ssc secret list --json`: one `GA46_TOKEN` row with exactly `name`, `version`, `live_version`, `updated_at`; `version` and `live_version` are v1's; neither the value nor its fingerprint appears in anything the `ssc` commands printed (text and JSON). |
| 5 | `ssc secret --help` lists only `set` and `list`: no command reads a value back. |
| 6 | **[real]** `ssc secret set` with v2 on stdin and no `--wait`: a newer version and an `operation_id`. Rotation redeploys by itself (`api/routes/v1/secrets.py`, `set_secret` starts a deployment of the live release); the kit runs no `ssc deploy` for it. |
| 7 | Right after: `version` is v2, `live_version` is still v1, and the app still answers v1's fingerprint: the running deployment keeps its pin. If the deployment had already landed this is "not read". |
| 8 | **[real]** `secret list` is polled every 10 s for up to 600 s until `live_version` is v2; then the app answers v2's fingerprint and no output held a value or fingerprint. |
| 9 | Read-only: `gcloud iam policies get ssc-deny-secret-read --attachment-point=cloudresourcemanager.googleapis.com/projects/ssc-c-<label> --kind=denypolicies` has a rule with no condition denying `secretmanager.googleapis.com/versions.access` to `ssc-secret-intake`. The control plane's account is reported as named or not (only when `--control-sa` is given) and never fails the check: it holds no role in the cell, so its absence from the rule is expected. |
| 10 | Read-only: `gcloud secrets get-iam-policy ssc-a-<env>-GA46_TOKEN --project=ssc-c-<label>` gives read (`secretAccessor`, admin, owner, editor) to exactly one service account, and names neither the intake, the control plane (`--control-sa`) nor a public member. |
| 11 | Manual (`infra/README.md` Secrets, live check 2): the printed `gcloud secrets versions access` and `gcloud secrets create`, impersonating `ssc-secret-intake`, must be refused. |
| 12 | Manual (live check 3): the printed `gcloud secrets versions add`, impersonating the control plane's account, must be refused. If it succeeds it adds a junk version: stop. |

Checks 11 and 12 need `roles/iam.serviceAccountTokenCreator` on the account named, for the operator, while they run; on 2026-10-08 the founder's account could not impersonate (GA-5.6, `infra/README.md`). Do not run 11 or 12 while a deployment is pending. Live check 4 of that list (the IAM condition matches `:addVersion`) is shown by check 1 passing, since the intake added the version under it.

The gap, as the kit prints it: a deployment's pinned versions are visible only as `live_version` in `secret list` (`routes/v1/secrets.py` `SecretOut`, `_SELECT_SECRETS`); no deployment record exposes `secret_refs`.

Leaves behind: the secret `GA46_TOKEN` in the app's preview with two more versions per run (there is no `ssc secret delete`).

Pass: every automatic check (1 to 10) passed; 11 and 12 never set the verdict. The kit never prints or saves a value, the operator's token or the cookie; the results hold the fingerprints and lengths, the versions, the checks and the notes, in `results/ga-4.6.json` and `results/secrets-<app>-<UTC stamp>.json`. Copy the final line, and what 11 and 12 showed, into the GA-4.6 record.

### Session apps (GA-4.7)

Two apps in `apps/reconnect` prove SSC-090's helpers: `py/` (FastAPI with `ssc_app.reconnect`) and `node/` (`@delimitus/ssc-reconnect`), both `sessions = true`, each serving `/ws?after=<n>` that sends n+1, n+2, ... once a second. `apps/reconnect/check.py` is the measurement; `sessions` runs it for both apps at once and keeps the record. The Streamlit leg uses the existing `pstream` (`apps/streamlit`).

**What the 1012 is.** The helper, not the gateway, ends the connection: it reads the deadline the gateway announces (`X-SSC-Request-Deadline`, 3600 s) and closes with 1012 about 30 s before it, so at about 59.5 minutes. The gateway's own 60-minute cut is never reached. The helpers only close; the client reconnects, which is `check.py` here and `sscSocket` in a browser. The record says "the helper's 1012 close and the client's reconnect", never "a gateway cut".

Before the first run:

1. Create and deploy the apps once **[real]**: `uv run ssc apps create prcpy`, `uv run ssc apps create prcnode`, then `uv run ssc deploy --app prcpy spikes/proofrun/apps/reconnect/py --wait` and `uv run ssc deploy --app prcnode spikes/proofrun/apps/reconnect/node --wait` at the repository root.
2. Share both previews with the person whose cookie the kit uses (`uv run ssc share <slug> <usr_…> --env preview`), and keep `pstream` deployed and shared the same way.
3. Keep a fresh session cookie for each app host in the jar: `uv run python -m proofrun cookie set prcpy--preview.proofcell01.delimitusapps.com` and the same for `prcnode--...`. The cookie lasts 12 hours. `check.py` reads the jar itself on every connect; the kit passes it nothing (no argument, setting or input) and replaces it with `[cookie]` in any line a child prints.

From `spikes/proofrun`:

```sh
uv run python -m proofrun sessions --minutes 70 [--hosts prcpy--preview.proofcell01.delimitusapps.com,prcnode--preview.proofcell01.delimitusapps.com] [--streamlit-host pstream--preview.proofcell01.delimitusapps.com]
```

The hosts default to those above (the Streamlit host to `pstream--preview.` plus the first host's cell). `--minutes` defaults to 70 (the ticket) and is refused below 62: `check.py` passes only after 60 minutes with a 1012, which comes at about 59.5. The kit runs two `check.py` processes at once and prints their lines with `[prcpy]` and `[prcnode]` in front. It runs 70 minutes; keep the machine awake.

| # | Check (1 to 4 for the first host, 5 to 8 for the second) |
| --- | --- |
| 1, 5 | The host has a cookie in the jar. Without one the host is not started, its checks 2 to 4 are "not read" (the detail carries the `cookie set <host>` hint) and the other host still runs. |
| 2, 6 | `check.py`'s last line is PASS: held 60 minutes or more, at least one restart on 1012, no gap. |
| 3, 7 | The first connection that ended 1012 was held 58.0 to 61.0 minutes, and the 1012 lines match the restarts `check.py` counted. The time and the number it reached are recorded. |
| 4, 8 | No user action and no gap: one launch with input closed, no `gap:` line, and numbers went on to a later number on a new connection after the 1012. Other ends are listed in the detail and never fail it. |
| 9 | Manual: the Streamlit screenshot `results/ga-4.7-streamlit.png`, reported "present" or "absent" and never part of the verdict. |

Verdict: any FAIL is FAIL, else any "not read" is INCOMPLETE, else PASS. A host whose `check.py` ends without a final line (it crashed or was stopped) is "not read".

**Streamlit screenshot (check 9).** The kit prints these steps at the start, with the times worked out:

1. At the printed start time open `https://pstream--preview.proofcell01.delimitusapps.com/` in a signed-in browser and note the caption "page served at HH:MM:SS UTC".
2. Leave the tab open and untouched (no reload, the machine awake).
3. At start plus 61 minutes or a little after, and before the run ends, take a screenshot of the whole window and save it as `spikes/proofrun/results/ga-4.7-streamlit.png`. It must show the URL bar with the host, the title "SSC proof run: Streamlit + pandas", a caption with a later time (about 60 minutes after the first) and no "Connecting" banner, and the clock. That Streamlit reruns its script when its own client reconnects is expected; the changed caption is the evidence, and the record says what the page showed.

Results go to `results/ga-4.7.json` and to `results/sessions-<UTC stamp>.json` (the checks, each drop's time and last number, other ends, and each host's `check.py` lines). Copy the final line, and the screenshot, into the GA-4.7 record.

### Warm option (GA-4.8)

The app is `apps/warm`: a FastAPI app whose `ssc.toml` declares the runtime only (no schedules, files, state or egress, so the control plane creates no cell resource for it). `/health` answers `started_at` (when the process started, ISO UTC), `pid` and `uptime_s`. `/` is a small page with the same `started_at` and `pid` in `<meta>` tags. A kept instance answers the same `started_at` after an idle hold, and a new one answers a later one. Before the first run, at the repository root:

1. **[real]** `uv run ssc apps create ga4warm`, `uv run ssc deploy --app ga4warm spikes/proofrun/apps/warm --wait`, then `uv run ssc promote ga4warm --wait`.
2. Share prod with the person whose cookie the kit uses: `uv run ssc share ga4warm <email or usr_…>` (prod, role user, by default).
3. Put the prod host's session cookie in the jar: `uv run python -m proofrun cookie set ga4warm.proofcell02.delimitusapps.com`.
4. Be logged in with `ssc login` as an org admin (admin2), not an agent session. Nothing in the org may be warm: the kit stops before any change otherwise, because `PUT /v1/warm` names every warm environment and the kit's own "off" would turn the others off too.

Then, from `spikes/proofrun`:

```sh
uv run python -m proofrun warm --app ga4warm --label proofcell02 [--project ssc-c-proofcell02] [--idle-minutes 20] [--settle-seconds 300]
```

The cell's project defaults to `ssc-c-<label>`. `--idle-minutes` is refused below 16, because Cloud Run takes an idle request-billed instance away after about 15 minutes. Every `PUT /v1/warm` sends `gateway: false`. Min instances are read with `gcloud run services describe` as the service's `run.googleapis.com/minScale`. That is the v1 view of the `scaling.minInstanceCount` that the cell agent writes (`ssc_agent/cloud_run.py`). The template's `autoscaling.knative.dev/minScale` is recorded, not judged. Page loads are sent the way a browser sends them (`Accept: text/html…`, `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`, `Sec-Fetch-Site: none`, the session cookie, no wake cookie), so the gateway marks them for the wake route (`ssc_edge.gate.page_load`). There, an app that has not answered within 2 s gets the waking page (503, `pages.WAKING`). The checks:

| # | Check |
| --- | --- |
| 1 | `GET /v1/warm`: the prod environment is listed with `warm` false, nothing is warm (`monthly_usd` 0), the gateway is not wanted and its `state` is `off`, and the service is at minimum 0. `monthly_usd` and `environment_monthly_usd` are recorded. On a FAIL the kit stops before any change. |
| 2 | Refusals: the prod environment with `monthly_usd_shown` off by one, and the preview environment at the right cost, each answer `422 VALIDATION_FAILED`. A `GET` after still shows nothing warm. |
| 3 | `PUT /v1/warm` with the prod environment at `environment_monthly_usd`: 200, `warm` true, `monthly_usd` is the cost, gateway `off`. This is T_on. |
| 4 | One reconciler pass: read the service every 5 s, up to `--settle-seconds`, until its minimum is 1 and it is ready (`observedGeneration` caught up, `Ready` true). The seconds from T_on to each are recorded. The serving revision must be the one from before: the minimum is a service setting, so no new revision is made. The kit then waits 30 s and reads `/health` three times, 5 s apart, to record the kept instance. |
| 5 | Audit: an `org.updated` row on target kind `warm` since T_on (less 2 minutes for clock skew), with `after.environment_ids` the prod environment, `gateway` false and `monthly_usd_shown` the cost. |
| 6 | The idle hold (`--idle-minutes`, no request to the app), one GET to the cell's `www` host, then one page load of `/`. Pass: 200 with the fixture page and not the waking page, a `started_at` that matches one of check 4's reads (the detail names which), and a first byte under 2.0 s. The `www` GET's time, whether the wake cookie was set (the gateway sets it on every page load, so it is recorded and never judged) and a `/health` read after are recorded. |
| 7 | `PUT /v1/warm` with nothing warm and cost 0: 200, `warm` false, `monthly_usd` 0, gateway `off`. This is T_off. Then the service's minimum is back to 0 and the service is ready, timed as in 4. |
| 8 | Audit: the row for the change off. `before.environment_ids` has the environment, `after.environment_ids` is empty and `monthly_usd_shown` is 0. |
| 9 | The same idle hold, `www` GET and page load. If the waking page shows, the kit does what the page does by itself: it loads `/` again every 2 s with the wake cookie, for up to 60 s. Pass: the app's eventual answer has a `started_at` other than check 6's (a new process). Whether the waking page showed, the retries and the cold time are recorded. |
| 10 | Manual, never in the verdict: the console. |

**Console (check 10).** Sign in to the console as admin2 and open the environment screen ("Your environment"). In the "Warm option" panel, tick `ga4warm`'s prod app and read the monthly add the screen shows. It should equal `environment_monthly_usd` ($10). Untick it and do not save. Save a screenshot showing the ticked box and the figure as `spikes/proofrun/results/ga-4.8-console.png`. The kit prints these steps and reports `check 10 manual` as "present" (a non-empty PNG) or "absent".

Verdict: any FAIL is FAIL, else any "not read" is INCOMPLETE, else PASS. If a check that the rest depend on fails (1, 3, 4, or the PUT in 7), the remaining checks are "not read".

**Leaves behind nothing.** If the kit set warm on, it sets it off again (`PUT` with no environment and cost 0) before the command ends, whatever stopped the run: a failed or timed-out check, an error, or Ctrl-C. It says so in a `finally:` line. The final line ends `warm left off: yes` or `warm left off: NO`. On `NO` it prints how to undo by hand (untick in the console's Warm option and save). There is no `--keep-warm`.

**Disclosed gaps.**

- Only the environment part of the warm option is proven. The gateway part sets the cell stack's `warm` flag through the cell deployer, whose image is stale on cell 2 and must not be run (GA-4.2). The kit never sends `gateway: true` and checks that the gateway's `state` stays `off`.
- The gateway itself is at minimum 0, so its cold start is paid by the GET to the cell's `www` host just before each page load. The gateway answers that GET itself and never asks the app. The page load's first byte then measures the app hop, which is what the wake route's 2 s timer covers.
- The monthly figures (API and console) are what the setting is said to cost, not a bill. Nothing charges for the option (A6).

Wall time: about 2 × `--idle-minutes` plus up to 2 × `--settle-seconds`, plus a few minutes. That is about 45 minutes with the defaults. Keep the machine awake. Results go to `results/ga-4.8.json` and to `results/warm-<UTC stamp>.json` (the checks, the timeline of every change, read and page load, and the timings). Neither ever holds the operator's token or the cookie. Copy the final line, and the screenshot, into the GA-4.8 record.

## What feeds what

| Result | Feeds |
| --- | --- |
| T1, T5, T6, T11, T12; subnet layout, the gateway's path to the auth host, agent ingress | decision 022 (cell bootstrap) |
| T2, T3, T7, T8, T10; relay checks | decision 023 (gateway) |
| instance_count during a WebSocket, T7 against `startup_latencies`, T9 | decision 025 (its usage source and reversal conditions) |
| T1 cost (empty under $25, full under $50 a month), T9 bill | decision 006 (A7) |
| T12 | SSC-089 |
| T9 hold and its drops (the 60-minute reconnect) | SSC-090 |
| T9 bill, T1 cost | SSC-096 (the first reconciliation) |

## What the kit cannot measure as written

- **T6**'s NAT leg uses a stand-in job in the data gateway's place, and NAT logging is off in the stack, so the NAT log read needs it turned on. The proxy leg is the real proxy, but only for hosts on the org allowlist.
- **T8** has no query or tunnel leg. Its times include `uv run ssc` starting, so they err high. The compile time is the difference between this machine's clock and the bucket's `update_time`.
- **T2/T3** with a sealed cookie skip the real login.
- **T4**'s range leg is not applicable while every cell has the same address plan.
- **T7**'s Streamlit health path does not run the script, so the first page view after a cold start is slower than the figure.
- **T9**'s request-billed leg and **T10** run on overrides, with no stack setting behind them.
- The deployer's live step 5 needs an empty cell that the control plane serves. Only cell 1 is served, and its database is on from T1.

## Files

- `proofrun/`: one module per proof (`t1.py` to `t12.py`), `instances.py`, and their shared parts (`common.py`, `probes.py`, `cloudrun.py`, `cost.py`).
- `apps/`: the probe apps. `standins/egress/`: T6's jobs (`apps/egress` is T6's proxy leg). `configs/`: the example stack configs. `seal_cookie.py`: the T2/T3 fallback.
- `tests/`: offline tests with fakes for every network call. None needs cloud credentials. `PROOFRUN_LIVE=1` runs the one live-only check (the operator's tools are installed).
