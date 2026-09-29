# GCP candidate: Cloud Run gen2

Throwaway project only. Never `ristretto-506621`. Set `P=<project> R=<region>` first. "verify flag" marks a flag whose exact spelling was not confirmed against current `gcloud` help; run `gcloud <command> --help` before use.

1. Enable APIs: `gcloud services enable run.googleapis.com compute.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com dns.googleapis.com --project $P`
2. Network: `gcloud compute networks create cell-vpc --subnet-mode=custom --project $P` then `gcloud compute networks subnets create cell-sub --network cell-vpc --range 10.10.0.0/24 --region $R --project $P`
3. Deny all egress from the VPC (apps use Direct VPC egress, so this rule applies to them): `gcloud compute firewall-rules create cell-deny-egress --network cell-vpc --direction EGRESS --action DENY --rules all --destination-ranges 0.0.0.0/0 --priority 1000 --project $P`. Add a second rule for IPv6 (`--destination-ranges ::/0`) if the subnet has IPv6 enabled. For the "allowed host" comparison later, add a higher-priority ALLOW to the egress proxy's IP only.
4. Cloud NAT with a fixed IP (for the fixed outbound IP row): `gcloud compute addresses create cell-nat-ip --region $R --project $P`; `gcloud compute routers create cell-router --network cell-vpc --region $R --project $P`; `gcloud compute routers nats create cell-nat --router cell-router --region $R --nat-external-ip-pool cell-nat-ip --nat-all-subnet-ip-ranges --project $P`
5. Private DNS response policy (DNS exfil row): `gcloud dns response-policies create cell-rp --networks cell-vpc --project $P`; add a rule that returns NXDOMAIN for `*.<canary zone>`: `gcloud dns response-policies rules create canary-block --response-policy cell-rp --dns-name "*.<canary zone>." --behavior bypassResponsePolicy` is the wrong behavior; use `--local-data` with no records, or the "NXDOMAIN" behaviour if the CLI exposes it (verify flag). Alternative check: point the whole policy at a private zone that owns the canary name.
6. Service account with zero roles: `gcloud iam service-accounts create app-null --project $P`. Grant nothing. Confirm with `gcloud projects get-iam-policy $P` that it does not appear.
7. Build and push images (managed build service row): `gcloud builds submit apps/probe_api --tag $R-docker.pkg.dev/$P/cell/probe-api --project $P` (create the Artifact Registry repo `cell` first). Same for `apps/static` and `apps/streamlit_pandas`.
8. Deploy each app:
   ```
   gcloud run deploy probe-api --image $R-docker.pkg.dev/$P/cell/probe-api --region $R --project $P \
     --execution-environment gen2 --ingress internal-and-cloud-load-balancing \
     --network cell-vpc --subnet cell-sub --vpc-egress all-traffic \
     --service-account app-null@$P.iam.gserviceaccount.com --no-allow-unauthenticated \
     --min-instances 0 --max-instances 2 --cpu 1 --memory 512Mi --timeout 3600 \
     --session-affinity   # verify flag; needed for the WS hold
   ```
   Repeat for `static` and `streamlit`. For Streamlit pass `--set-env-vars XSRF=true,CORS=true` first, then flip to false if the origin check fails behind the LB.
9. Internal HTTPS load balancer in front of the services (serverless NEG per service, internal managed cert or self-signed for the spike): `gcloud compute network-endpoint-groups create probe-api-neg --region $R --network-endpoint-type serverless --cloud-run-service probe-api --project $P`, then backend service, URL map, target HTTPS proxy and forwarding rule with `--load-balancing-scheme INTERNAL_MANAGED` (verify flag set; the console wizard is acceptable for the spike). Run the runner from a small VM in `cell-vpc` so it sees the internal LB.
10. Peer URL for the "cannot reach peer app" row: the second service's `run.app` address, and also its internal LB path. Both should fail from inside `probe-api`.
11. Public ingress row: `--public-api-url https://probe-api-<hash>-$R.a.run.app` from your laptop; expected 403 or 404.
12. Kill command for `kill_timer.py` (measure each, record the fastest):
    - `gcloud run services update probe-api --ingress none --region $R --project $P` (verify that `none` is an accepted ingress value; if not, use `--no-allow-unauthenticated` plus IAM removal)
    - `gcloud run services update-traffic probe-api --to-revisions=LATEST=0 --region $R --project $P` (verify flag)
    - `gcloud run services delete probe-api --quiet --region $R --project $P`
13. Cost row: fill `COST_SHEET.md` from the project's billing report after 24 h idle, plus the price list.
14. Tear down: `gcloud projects delete $P`.

## Run notes 2026-09-29 (project delimitus-0926, us-central1)

- Step 5: a response policy was not needed. A private Cloud DNS zone that owns `canary.<zone>.` with no records, bound to `cell-vpc`, returned NXDOMAIN to Cloud Run with Direct VPC egress. The name never appeared in the public zone's query log.
- Step 9: regional internal Application LB (`INTERNAL_MANAGED`) needs a proxy-only subnet (`--purpose REGIONAL_MANAGED_PROXY`). HTTP on port 80 was used for the spike. `allUsers` invoker is safe only together with `--ingress internal-and-cloud-load-balancing`; the LB does not add an identity token.
- Step 11: the default `run.app` address returned 404 from the internet, with and without an owner token.
- Step 12: `--ingress none` is not a valid value. Removing the invoker binding took 80 s to cut traffic and an LB route swap took 142 s, both too slow. Shifting 100 % traffic to a tombstone revision cut traffic in 2.3 s and deleting the service in 1.6 s.
- Scale-to-zero: Cloud Run kept idle instances past a 15-minute gap, so a 900 s `--cold-gap` measures warm starts. Cold samples used a 26-minute gap.
- The metadata token cannot even reach the Google APIs because egress is denied; the probe records that as blocked.
