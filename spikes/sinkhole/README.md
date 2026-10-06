# SSC-091 sinkhole experiment

Why does creating the DNS sinkhole's 1,438 rules take 78 minutes (about 3.3 s a rule)? The code
says the provider does not serialise them (see the ticket's phase 1 report), so the limit is in
Cloud DNS. `sinkhole.py` times rule creation on a throwaway response policy and tells which.

## What it touches

Only project `ssc-platform-0` (it refuses any other, and `ristretto-506621` by that rule), and
only names that start with `ssc-exp091`. The policy `ssc-exp091` has no network attached, so no
VPC and no cell is affected. At most 200 rule creations are sent in all. Python 3.11 or later,
standard library only; the token is `gcloud auth print-access-token`.

A burst of creates can use the project's write quota for a minute. Run it with no `pulumi up
--stack platform` and no control-plane DNS work in flight.

## Run

```
cd spikes/sinkhole
python3 sinkhole.py --dry-run                 # prints every call, sends none
python3 sinkhole.py --steps E0                # read only: quota listing and leftovers
python3 sinkhole.py                           # E0 to E6; writes results.json
python3 sinkhole.py --steps E0,E2,E3          # E1 is added when E2 to E5 need the policy
python3 sinkhole.py --cleanup-only            # deletes every ssc-exp091* rule and policy
```

`--concurrency N` sets E4's largest wave (default 32, at most 64). E6 (cleanup) runs even when a
step fails or you press Ctrl-C. After a killed run, `--cleanup-only` finishes the job and lists to
confirm; `results.json` has `E6.clean`.

| Step | What |
|---|---|
| E0 | Read only. Cloud Quotas listing for `dns.googleapis.com` (the rows that mention response policies), and the project's policies. If it answers 403 `SERVICE_DISABLED` for Cloud Quotas, run `gcloud beta quotas info list --service=dns.googleapis.com --project=ssc-platform-0` by hand. |
| E1 | Creates the policy `ssc-exp091`. Stops if one is already there: run `--cleanup-only`. |
| E2 | 10 rule creates, one after another, each timed. |
| E3 | 10 creates released together, each timed, plus the wall time. |
| E4 | Waves of 8, 16, then 32 creates at once, no client retry. Stops at the first 429 or other error, or at 195 rules sent. |
| E5 | One multipart request of 5 creates to `https://dns.googleapis.com/batch`. |
| E6 | Deletes every rule and policy named `ssc-exp091*` (8 at a time, retrying 429), then lists to confirm. |

## Reading `results.json`

`reading` holds a hint; check it against the numbers (`E2.median`, `E3.wall`, `E3.seconds_sorted`, `E4`).

1. **Cloud DNS serialises per policy.** `E3.wall` is about ten times `E2.median`, the ten
   latencies in `E3.seconds_sorted` climb in steps of about one `E2.median`, and no 429. No
   client-side concurrency helps; the 78 minutes is Cloud DNS's.
2. **A per-minute write quota.** E3 is fast, then `E4` stops on a 429. `E4.waves[-1].first_error`
   names the quota and `retry_after` the wait; `E4.rules_per_minute` is the rate reached. The
   provider retries 429 silently, which is why Pulumi only looked slow. The first fix is a higher
   quota (E0 says whether it is adjustable and at what level).
3. **Neither.** `E2.median` itself is several seconds (each create is slow), or E3 and E4 run
   without a 429 at a rate well above 18 a minute. Then the cause is elsewhere (the provider's
   read after each create, the engine) and phase 2 starts from there.

`E5.status` of 200 with 5 rules created means a batch endpoint exists (compare `E5.seconds` with
one create); 404 or 400 means it does not take these calls.

`results.json` is rewritten after each step, so a crash keeps what ran. It holds every call's
status and latency under `calls`.

## Tests

The script is tested against a fake Cloud DNS (serialised, quota-limited and fast), with no
network: `cd infra && uv run pytest ../spikes/sinkhole -p no:cacheprovider`.
