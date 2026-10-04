# Kill switch drill (SSC-054)

How fast the kill switch cuts an app off, measured in a staging cell and published as a table. The drill is `python -m ssc_conformance.kill_drill`; the app it kills is `conformance/kill_drill_app`. The workflow is `.github/workflows/kill-drill.yml`: run by hand only for now, and off until its settings exist.

## What is measured

The drill fires the kill as the console does: `POST /v1/apps/{app_id}/kill-switch` with an admin's token, then polls `GET /v1/apps/{app_id}/kill-switch/{run_id}`. It does not drive a browser. Between runs it calls `POST /v1/apps/{app_id}/enable`. Every time is in seconds from the moment the command was sent.

| Measure | Where it comes from |
| --- | --- |
| Front-door denial | `/health` polled every 0.25 s through the cell's public load balancer, as a person signed in to the app; the first answer that is not 200 |
| Stream cut | An open WebSocket to the drill app; the time the client sees it end |
| Query end | The drill app's log line when its long query (`SELECT pg_sleep(25)` through the data gateway) is refused or cut, read from Cloud Logging |
| Tunnel close | The drill app's log line when its tunnel through the egress proxy (held open with a `GET /rate_limit` every 20 s) closes |
| Instance stop | `since_command_ms` of the `scale_to_zero` row of the audit's `kill_switch.step` events |
| Timer pause | `since_command_ms` of the `pause_timers` row |
| `latest.json` moved | The `updated` time of `snapshots/<org>/latest.json` in the cell bucket |

Query end and tunnel close count only if the leg was running when the kill came; a run where one was not is a failure, not a fast time.

The drill runs `SSC_DRILL_RUNS` times (ten) in each of two states, and the table gives the median and the maximum of each.

- **Awake.** The drill app holds an open WebSocket, a running long query and an open tunnel when the kill is pulled.
- **Asleep.** The app, the gateway and the data gateway have had no request for 26 minutes, so each is at zero. The drill waits that long by the Cloud Run request log, pulls the kill, and sends one request after the `gateway_deny` step is done. Nothing can send a query or open a tunnel as an app that is at zero, so the data gateway and proxy cells are published as "nothing started", and the logs prove it: no request reached the app, no new app instance, no new data gateway instance or query for this environment, and no proxy line for it. The gateway's own request line for the refused request must be in the log, and the proxy's log must have been seen in every awake run, or the drill reports the proof as not made.

**Pass.** Front-door denial under 10 seconds and everything else under 60 seconds, in both states. The drill prints the verdict, the table and the longest an open stream survived, and exits 1 on a fail.

A full drill takes about 5 hours, mostly waiting for the app to go to zero before each asleep run. The drill gives up on a cell that has not gone quiet after 75 minutes, so it runs in a cell kept for it, with nothing else running there (the nightly included).

## Setting it up

All steps are by a person, once, in a staging cell with the `connections` and `egress` flags on. Nothing here is created by code.

1. Deploy the drill app: `ssc deploy conformance/kill_drill_app` as a builder. Its `ssc.toml` names the connection `drill-db` and the host `api.github.com`.
2. Create the connection `drill-db` to a staging Postgres (`infra/README.md`, Data gateway) and grant it to the app. `SELECT pg_sleep(25)` and `SELECT 1` must be allowed.
3. Put `api.github.com` on the org's egress allowlist (a `*.github.com` entry will do).
4. Share the app with the person whose session the drill uses (`ssc share`).
5. Note the ids: `app_...`, its environment `env_...`, the org `org_...`, the person `usr_...`, the cell project, and the app's public host.
6. An admin's access token for the control plane, and the person's session: either the cell's session keyring JSON with the person's `usr_` id, which the drill uses to seal a cookie in memory, or a cookie value.
7. Google credentials for the caller: log read on the cell project and read on its bucket. The drill takes `SSC_ACCESS_TOKEN` if set (one token, about an hour, so for a test run only), else the credential file `GOOGLE_APPLICATION_CREDENTIALS` names, which it refreshes as the token nears expiry, else `gcloud auth login`.

Run it by hand:

```
export SSC_DRILL_API_URL=https://<control plane>
export SSC_DRILL_APP_ID=app_... SSC_DRILL_ENV_ID=env_... SSC_DRILL_ORG_ID=org_...
export SSC_DRILL_HOST=<the app's public host> SSC_DRILL_PROJECT=<cell project>
export SSC_DRILL_USER=usr_...
read -rs SSC_DRILL_TOKEN; export SSC_DRILL_TOKEN
read -rs SSC_DRILL_KEYRING; export SSC_DRILL_KEYRING
SSC_DRILL_RUNS=1 uv run python -m ssc_conformance.kill_drill
```

Start with one run in each state. The drill never prints the token, the keyring or the cookie.

The workflow runs by hand only (`workflow_dispatch`). Set the variables and secrets named at the top of `.github/workflows/kill-drill.yml`, let the Google identity in `SSC_DRILL_SERVICE_ACCOUNT` trust that file, and run it from the Actions tab. `SSC_DRILL_TOKEN` must outlive the run, about 5 hours. There is no long-lived control plane credential yet, so the weekly schedule stays commented out until a machine credential exists for the drill.

## Results

Not yet run. Seconds from the command, median / maximum over ten runs. The first row repeats SSC-086 T8 with the real data gateway and proxy in place of its stand-ins; the numbers are copied from `spikes/proofrun/RESULTS.md` once T8 runs. The "longest an open stream survived" is the maximum of the stream cut column.

| Row | Runs | Front door | Stream cut | Query end | Tunnel close | Instance stop | Timer pause | `latest.json` moved |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SSC-086 T8 (stand-ins, `ssc disable`) | not yet run | not yet run | not yet run | not yet run | not yet run | not yet run | not yet run | not yet run |
| awake | not yet run | not yet run | not yet run | not yet run | not yet run | not yet run | not yet run | not yet run |
| asleep | not yet run | not yet run | none open | not yet run | not yet run | not yet run | not yet run | not yet run |

- **Date, commit, cell:** not yet run.
- **Longest an open stream survived:** not yet run.
- **Verdict:** not yet run.
