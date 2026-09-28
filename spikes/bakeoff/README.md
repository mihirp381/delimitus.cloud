# SSC-001 cloud bake-off harness

Everything a pair needs to score one candidate. No cloud resources are created by this repo; the deploy notes are run by hand in a throwaway project.

## What the founder must do first

1. Create three throwaway accounts with billing on: one GCP project (not `ristretto-506621`), one AWS account in its own OU, one Azure subscription. Plus one Fly organisation for the control run.
2. Grant the two engineers for each candidate Owner on that project only.
3. Own a DNS zone used for nothing else (the canary zone, for example `canary.<any domain we own>`), with query logging on. Give the runner's `--canary-zone` that name.
4. Install CLIs on the engineers' machines: `brew install awscli azure-cli flyctl`. `gcloud` is already present.

## Run order for one candidate

1. Build and push the three images in `apps/` (`static`, `probe_api`, `streamlit_pandas`) with the candidate's build service (see `deploy/<candidate>.md`).
2. Follow `deploy/<candidate>.md` to create the isolated network, the deny-all egress rule, NAT with a fixed IP, the DNS policy, a zero-permission identity, the three apps and the internal load balancer.
3. From a VM inside that network:
   ```
   uv sync
   uv run python runner/run_checks.py --candidate gcp \
     --api-url https://<internal LB>/probe-api --static-url https://<internal LB>/static \
     --streamlit-url https://<internal LB>/streamlit --peer-url https://<second app internal address>/healthz \
     --public-api-url https://<default public address> --canary-zone canary.example.com --cold-gap 900
   ```
   `--cold-gap 900` leaves 15 minutes between samples so the app scales to zero between them; check the candidate's scale-to-zero delay and set the gap above it. Use `--quick` for a dry run; the real run holds SSE 10 min and WS 30 min.
4. Cut-off: `uv run python runner/kill_timer.py --candidate gcp --url https://<internal LB>/probe-api/healthz --command "<kill command from deploy notes>"`. Repeat per kill command; keep the fastest.
5. Streamlit origin check: open the Streamlit URL through the load balancer in a browser. Record in `results/<candidate>.manual.json` under `streamlit_ws_origin` which flag combination was needed for the page to load (see the caption in the app).
6. Manual rows: write `results/<candidate>.manual.json` with keys `empty_cell_monthly_usd`, `streamlit_ws_origin`, `org_deny_secret_read`, `isolated_account_per_customer`, `microvm_isolation`, `postgres_pitr_cmek`, `managed_build`, `log_query_api`, `fixed_egress_ip`, `operating_experience_years`, `customer_data_location`. Numbers or yes/no with a source URL, not adjectives. Cost comes from `COST_SHEET.md`.
7. `uv run python runner/scorecard.py` merges all `results/*.json` into `SCORECARD.md`.
8. Tear the project down.

## Reading the probes

- `pass` on egress, DNS, metadata and peer means the thing was **blocked**. Run locally these read `fail`; that is expected.
- DNS: the runner sends `<random>.<canary zone>`; the pass condition is NXDOMAIN from the system resolver **and** no reply on direct UDP to 8.8.8.8. Also confirm the name never appears in the canary zone's query log.
- Metadata: a GCP token with `403` on the project list counts as pass (the identity exists but can do nothing). Token values are never printed.

## Local smoke test

```
docker build -t ssc-bakeoff-api apps/probe_api && docker run --rm -p 8089:8080 ssc-bakeoff-api
curl localhost:8089/healthz; curl localhost:8089/probe/egress
```
