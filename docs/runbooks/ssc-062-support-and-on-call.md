# SSC-062 support and on call

What pages a person, what that person does first, how a builder gets help, and how an outage is announced. The alerts are built by `infra/ssc_infra/alerts.py`; `infra/README.md` (Alerts and on call) says how to turn them on.

Nothing here has run against a real project yet. Every step marked "not run" is written from the code and the provider's schema, and is listed under live checks in `infra/README.md`.

## How the alerts work

- One email channel per project, from the Pulumi setting `oncall_email`. With it unset, a stack has no alert resources at all. A cell's alerts and its channel live in the cell project; the control plane's live in `ssc-control-<stage>`.
- A gateway or data gateway with no instances is healthy: it scales to zero between requests. So no alert watches an instance count, an uptime check or a missed heartbeat. Every alert counts log lines that only a request or a failure writes, or a load balancer error ratio, and missing data never fires one. A cell with nothing running raises nothing overnight.
- The nightly run (SSC-017) is the liveness check for an idle cell. Nothing keeps a service awake to watch it.
- Each alert's notification links to its heading below, which has the same name as the alert.

## ssc-gateway-authoriser-errors

Pages when the gateway's authoriser raises more than 5 errors in 5 minutes (log line `authz check failed`). The authoriser refuses with a 503 when its check raises, so builders see "temporarily unavailable" on every app in the cell.

1. Read the gateway's logs in the cell project for `authz check failed`; the traceback names the cause.
2. If the snapshot cannot be read, see `ssc-gateway-snapshot-stale`.
3. If the last gateway release is the cause, roll the gateway back to the previous image in Cloud Run.

## ssc-gateway-snapshot-stale

Pages when a request was served from an access snapshot more than 60 seconds old (the gateway's warning line `gateway snapshot stale snapshot_age_ms=N`, at most one a minute per instance). The age is the time since the gateway last confirmed its view against `latest.json`. A gateway with no traffic logs nothing.

1. Check `ssc-snapshot-late` in the control project: a late compile or bucket write is the usual cause.
2. Read the cell bucket's `snapshots/<org>/latest.json` and compare its version with the control plane's.
3. A gateway that has never read a snapshot refuses requests and logs `snapshot_age_ms=-1`, which does not page; that is a setup fault, not a stall.

## ssc-gateway-lb-error-rate

Pages when more than 5% of the gateway's requests at the cell's load balancer fail with a 5xx for 5 minutes. With little traffic one failing request is a large share, so check the request count before acting.

1. Open the load balancer's logs in the cell project and group the 5xx by `statusDetails`.
2. A Cloud Run cold-start timeout shows as a 503 or 504 on the first requests after a quiet spell; the warm option removes it.
3. Otherwise treat it as `ssc-gateway-authoriser-errors`.

## ssc-datagw-refusals

Pages when the data gateway refuses more than 20 calls in 5 minutes. `APP_NOT_ACTIVE` is left out: a stopped app calling its data is expected. A refusal is any `outcome` other than `served`.

1. Read the data gateway's logs for `datagw query` lines and group them by `outcome` and `env_id`.
2. One app is usually a bug or a grant removed on purpose; tell the builder.
3. Many apps with `UNAVAILABLE` or `FILES_UNAVAILABLE` is the connection or the bucket: check the connection in the console, then the cell bucket.

## ssc-proxy-unhealthy

Pages when the egress proxy's health check goes from healthy to unhealthy (the health check's own logs). The machine is in a managed group that recreates it when its TCP check on port 3128 fails, so it normally comes back by itself in minutes. A stopped machine takes the same route.

1. Watch the group: `gcloud compute instance-groups managed list-instances ssc-proxy --zone us-central1-a --project <cell project>`. The instance should return to `RUNNING` with a new name.
2. If it does not come back, or the zone is down, follow the zone-loss outage below.
3. A cell with `egress` off has no proxy, so this alert cannot fire for it.

## ssc-cell-budget

The cell's budget (`cell-monthly`, $50) mails the on-call address at 50, 90 and 100 percent of spend and at 100 percent of forecast. It is the cell's own Cloud Billing budget; no second one exists. It is a heads-up, not a page: one customer's runaway app shows against that customer.

1. Find the cost with the SSC-096 reconciliation runbook.
2. Ask the builder about the app that grew, or use the kill switch (SSC-054) if it is abuse.

## ssc-cert-expiry

The nightly run reads the cell's wildcard certificate over TLS at its public host and fails when under 21 days remain, or when the certificate does not verify. A Certificate Manager certificate that fails to renew keeps serving until it expires, so a failed renewal shows as a falling expiry and is caught here 14 or more days ahead. No Certificate Manager metric is used: none is documented.

The check runs each night for every cell in `SSC_NIGHT_CELLS`, at `alpha.<base>` under the cell's apps domain. It shows as a failed nightly run, which the on-call person owns.

1. In the cell project, open the certificate in Certificate Manager and read its state and any failure reason.
2. The usual cause is the DNS authorisation record missing from the apps zone: restore it (`cert-dns-auth`, `dns-cert-auth` in the cell stack) and re-apply the cell stack.

## ssc-snapshot-late

Pages from the control project when a snapshot compile takes over 60 seconds or fails for good (log line `snapshot compile late`), or the 5-minute sweep finds an organisation's snapshot behind its data and marks it dirty (`stale snapshot marked dirty`). A late bucket write shows up as a late compile.

1. Read the worker's logs for the two lines; the compile line carries the org.
2. Check the worker pool and the database are up (SSC-064 runbook).
3. Once fixed, the next sweep compiles the dirty orgs again.

## ssc-build-failures

Pages when 3 builds fail in 15 minutes (log line `build failed`). One failing build is the builder's own mistake; three in 15 minutes is the build tooling.

1. Read the lines' `code` and `why`: a run of the same code across orgs is the platform.
2. Check the build images in the platform registry and the build service account's grants in the cell.

## Proxy zone-loss outage (not run)

Expected outage, said in the trust pack (SSC-058): the proxy is one machine in one zone. The group replaces a crashed or stopped machine in minutes. If the whole zone `us-central1-a` is lost, outbound calls from that customer's apps to outside hosts fail until someone recreates the group in another zone at the same address. Apps that call no outside host are not affected. A customer who cannot accept this is the case for the `proxy_ha` flag (SSC-053), which runs two machines in two zones.

Manual step, written from `infra/ssc_infra/cell.py`, not run:

1. Confirm the zone is the problem: the proxy alert fired, the instance does not return, and Google's status page names the zone.
2. Find the proxy's newest instance template: `gcloud compute instance-templates list --project <cell project> --filter="name~^ssc-proxy-" --sort-by=~creationTimestamp --limit=1`. The template pins the reserved address `10.20.4.10`.
3. Create a group of one from that template in another zone: `gcloud compute instance-groups managed create ssc-proxy --project <cell project> --zone us-central1-b --size 1 --template <template> --health-check ssc-proxy --initial-delay 300`.
4. If the new machine will not take the address because the lost machine still holds it, delete the lost machine or its group first; if the zone's API does not answer, wait.
5. Check an app's outbound call to an allowed host from the app's logs.
6. Do not apply the cell stack until the zone returns: the stack still names `us-central1-a` and would recreate the group there. Afterwards remove the extra group and run `pulumi refresh`, then apply.

## Status note process

A status note tells builders and the customer that something is wrong, before they write in. It is a message, not a page; no tool is built for it.

Checklist:

1. Decide: one customer or all, and what a builder sees (apps not loading, deploys failing, outbound calls failing).
2. Write the note in four lines: what is affected, since when (UTC), what we are doing, when the next update comes.
3. Send it from the support inbox to the customer's admin and builders who wrote in; for an all-customer outage, to every pilot admin.
4. Update at the time promised, even with no news.
5. When fixed, send the end time, the cause in one sentence and what builders must do, if anything.
6. Within two days, add the cause and fix to this runbook if it was new.

## Ready support answers

Reply from the support inbox. Each is short on purpose.

**"My app is slow the first time."** Apps sleep when nobody uses them, and the first load after a quiet spell takes several seconds while the app wakes up. After that it is fast. If this is a problem for an app people open every day, an org admin can choose the warm option (about $10 a month an environment, about $10 for the gateway).

**"My dashboard reloaded after an hour."** A session app's connection ends at 60 minutes, and a Streamlit app then starts again from the top and loses what was on screen. We cannot change it from outside Streamlit. Keep anything a person should not lose in the database, or in the page's URL with `st.query_params`.

**"My deploy is waiting."** If it is the first deploy that needs a database, the database is being created, which takes a few minutes; the deploy continues by itself. If it keeps waiting, ask for the deploy's id.

**"`DB_TIER_FULL`."** The company's database has no room for another app database. Nothing is broken and nothing was created. An org admin can see the places used on the "Your environment" screen and can ask for the bigger database, a paid step; the screen shows its monthly price.
