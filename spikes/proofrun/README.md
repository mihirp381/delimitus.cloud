# SSC-086 proof-run kit

One command per proof from SSC-086 (architecture document, section 8). Each command ends in one line, `T<n> <number> PASS|FAIL|INCOMPLETE`, appends the full result to `results/t<n>.json` and exits 0, 1 or 2. Copy the final lines into `RESULTS.md`.

Steps marked **[real]** create, change or delete a real resource, as in the SSC-064 runbook. Run them in order, each only after the founder has agreed. Every other step only reads. The kit itself never creates anything: its commands read with `gcloud ... describe/list`, `ssc status`, HTTP requests to the cell's public hosts, and Cloud Monitoring. There are three exceptions, and each is called out where it happens: `t6 nat` and `t6 proxy` execute an existing Cloud Run job, `t8` runs `ssc disable`, and `t3 nightly`/`t4` run the existing nightly, which deploys the probe apps.

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

Results taken with a sealed cookie say the real login was not exercised. One cookie is kept per host, valid for at most 12 hours, so refresh it during T9.

## Probe apps

Everything under `apps/` deploys through `ssc deploy`. Create each app once with `uv run ssc apps create <slug>`, then run `uv run ssc deploy --app <slug> <folder> --wait` at the repository root **[real]**. An app's owner gets no grant (decision 019), so give the signed-in person preview access to each app with `uv run ssc share <slug> <usr_…> --env preview`, or the gateway answers 404 `not_granted`. The kit always reads the `preview` environment. `ssc deploy` places apps in cell 1 only, because the control plane serves one cell (`cell_label`).

| Slug (example) | Folder | Used by |
| --- | --- | --- |
| `proba`, `probb` | `uv run python -m proofrun stage-probe /tmp/probe` (the conformance probe app with the kit's manifest) | T2, T3 public, T10 |
| `pstatic` | `apps/static` | T7 |
| `papi` | `apps/api` (`/vpc`, `/ws`, `/deny`) | T7, T8, T10, T11 |
| `pstream` | `apps/streamlit` (the bake-off's workload) | T7, T9 |
| `pg01` to `pg10` | `apps/pg` (`[state] postgres = true`) | T5 |

The kit bypasses `ssc deploy` in two places:

- **Cell 2's probe apps.** They come from the nightly (`t3 nightly` against cell 2), because the control plane does not serve cell 2.
- **T6's stand-in jobs.** They need a chosen service account, subnet and network tag, and no SSC app can have those.

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
| 11 | T6 stand-ins **[real]** | under $0.10 | below |
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

### T6: NAT and the proxy, with stand-ins

`ssc_datagw` and `ssc_egress` are empty, so T6 uses the stand-ins the ticket allows. It is evidence for neither SSC-050 nor SSC-053. Set up **[real]**. Run the `docker` and `gcloud` lines at the repository root and the kit's line in `spikes/proofrun`:

```sh
REG=us-central1-docker.pkg.dev/$P1/ssc-apps
docker buildx build --platform linux/amd64 --provenance=false --push --tag $REG/proofrun-egress:1 spikes/proofrun/standins/egress
gcloud run jobs create proofrun-egress-nat --project=$P1 --region=us-central1 --image=$REG/proofrun-egress:1 \
  --service-account=ssc-data@$P1.iam.gserviceaccount.com --network=ssc-cell --subnet=gateway \
  --network-tags=ssc-data --vpc-egress=all-traffic --max-retries=0 --task-timeout=120s
gcloud run jobs create proofrun-egress-app --project=$P1 --region=us-central1 --image=$REG/proofrun-egress:1 \
  --service-account=ssc-deny-probe@$P1.iam.gserviceaccount.com --network=ssc-cell --subnet=apps \
  --vpc-egress=all-traffic --max-retries=0 --task-timeout=120s
uv run python -m proofrun t6 envoy-config --envoy-image envoyproxy/envoy:<tag>@sha256:<digest> --out results/envoy-user-data.yaml   # in spikes/proofrun
INST=$(gcloud compute instance-groups managed list-instances ssc-proxy --zone=us-central1-a --project=$P1 --format='value(name)')
gcloud compute instances add-metadata $INST --zone=us-central1-a --project=$P1 --metadata-from-file=user-data=spikes/proofrun/results/envoy-user-data.yaml
gcloud compute instances reset $INST --zone=us-central1-a --project=$P1
```

The cloud-config points the proxy machine's resolver at 8.8.8.8 over the NAT, because the cell's resolver sinkholes docker.io and the allowed hosts. It then starts stock Envoy with a fixed two-host CONNECT list on 10.20.4.10:3128. If the group recreates the machine before Envoy answers its health check, the machine's name changes; repeat the last two lines.

```sh
uv run python -m proofrun t6 nat --project $P1 --nat-ip $NAT1      # executes the job: the data gateway's place
uv run python -m proofrun t6 proxy --project $P1 --nat-ip $NAT1    # executes the job: an app's place
```

Pass: `nat` leaves from the reserved NAT address. In `proxy`, both allowed hosts tunnel and report the NAT address, and both unlisted hosts are refused.

Teardown **[real]**:

```sh
gcloud run jobs delete proofrun-egress-nat --project=$P1 --region=us-central1
gcloud run jobs delete proofrun-egress-app --project=$P1 --region=us-central1
gcloud artifacts docker images delete $REG/proofrun-egress --delete-tags --project=$P1
gcloud compute instance-groups managed recreate-instances ssc-proxy --instances=$INST --zone=us-central1-a --project=$P1
```

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
uv run python -m proofrun t9 hold --state results/t9-instance.json --app pstream --project $P1 --mode instance
```

Request-billed needs an override. Pause the worker so nothing re-applies the service while it holds:

```sh
cd infra && pulumi config set --stack platform worker_instances 0 && pulumi up --stack platform
gcloud run services update <ssc-a-...> --project=$P1 --region=us-central1 --cpu-throttling
uv run python -m proofrun t9 hold --state results/t9-request.json --app pstream --project $P1 --mode request
gcloud run services update <ssc-a-...> --project=$P1 --region=us-central1 --no-cpu-throttling
cd infra && pulumi config set --stack platform worker_instances 1 && pulumi up --stack platform
```

`hold` refuses to start if the service is billed the other way. Each hold keeps `/_stcore/stream` open as a browser tab does. When the stream drops, the hold records why and reconnects at once, reading the cookie again. A refused reconnect, such as an expired cookie, is retried every 30 s. Refresh the cookie before its 12 hours are up. A stopped hold resumes with the same command.

The next day, read the service's usage amounts for the hold's day. Use Billing, Reports, filtered to the project and Cloud Run, grouped by SKU, with credits unticked. Then run:

```sh
uv run python -m proofrun t9 report --state results/t9-instance.json
uv run python -m proofrun t9 bill --state results/t9-instance.json --vcpu-seconds <n> --gib-seconds <n>
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

- **T6** measures stand-ins only.
- **T8** has no query or tunnel leg. Its times include `uv run ssc` starting, so they err high. The compile time is the difference between this machine's clock and the bucket's `update_time`.
- **T2/T3** with a sealed cookie skip the real login.
- **T4**'s range leg is not applicable while every cell has the same address plan.
- **T7**'s Streamlit health path does not run the script, so the first page view after a cold start is slower than the figure.
- **T9**'s request-billed leg and **T10** run on overrides, with no stack setting behind them.
- The deployer's live step 5 needs an empty cell that the control plane serves. Only cell 1 is served, and its database is on from T1.

## Files

- `proofrun/`: one module per proof (`t1.py` to `t12.py`), `instances.py`, and their shared parts (`common.py`, `probes.py`, `cloudrun.py`, `cost.py`).
- `apps/`: the probe apps. `standins/egress/`: T6's job. `configs/`: the example stack configs. `seal_cookie.py`: the T2/T3 fallback.
- `tests/`: offline tests with fakes for every network call. None needs cloud credentials. `PROOFRUN_LIVE=1` runs the one live-only check (the operator's tools are installed).
