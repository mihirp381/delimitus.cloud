# SSC-111 prove a customer domain live

The customer-domain work (CD-1 to CD-7, `docs/plans/custom-domains-2026-10-26.md`) is built and tested, but no real domain has been delegated to a cell yet. This runbook is that proof. It takes one afternoon and then seven nights. Until it passes, `landing/CLAIMS.md` and the landing FAQ say nothing about customer domains (plan Q5).

The proof runs in a staging cell named in `SSC_NIGHT_CELLS`, as that cell's `org`, so the nightly suffix walk (`ssc_conformance.suffix_walk`) covers what you set up. You need an admin of that org signed in to the CLI (`ssc login`), a domain whose registrar you control, and `dig` and `curl`.

Write the UTC time of every step and every state change in the evidence file (below). The "expect" times are what the code allows, not promises: the tick runs every minute, `pending_dns` fails after 72 hours without the delegation, and after 120 minutes in `issuing` the operator gets one mail (`SSC_OPERATOR_MAIL`, `docs/runbooks/ssc-049-approval-email.md`).

## Before you start

- The vendored Public Suffix List is from 2026-10-22, and `test_psl` fails once it is 90 days old. If today is past 2027-01-20, refresh it first (`uv run python tools/psl_refresh.py`, `docs/README.md`).
- The cell agent's role `sscCellAgentDomains` holds `certificatemanager.dnsauthorizations.use` and `certificatemanager.certs.use`. Step 2 is the first time it uses them against real Google APIs, so watch the agent's log during that step.
- The cell's gateway signs env-id notes, so the org's apps are built on helpers 0.0.6 or later (`docs/contracts/identity-note.md`, amendment 2026-10-28). Release order: publish 0.0.6, rebuild the apps, roll the gateway.
- The worker runs `domains:tick` (`gcloud run jobs executions list` on the worker, or its log line `domains tick`).

## Steps

1. **Pick the domain.** Buy a throwaway domain, or use one we own that has no mail and no customers, at a registrar that lets you add NS records for a subdomain. The suffix is `tools.<domain>`: a registered domain itself is refused (`DOMAIN_SUFFIX_PUBLIC`). Record the domain, the registrar and the suffix.

2. **Ask for it.** `ssc domains add tools.<domain>` (t0). Expect, within a minute: the NS records to add, the ticket text for IT, and `state: pending_dns`. A `CELL_UNAVAILABLE` here, or a `403` in the agent's log, is the IAM gap above: stop, and fix the role before going on. Record the NS set and the `provider` the API guessed (`ssc domains show --json`).

3. **Delegate.** At the registrar, add the NS records for `tools.<domain>` exactly as printed, and nothing else (t1). Then run `ssc domains status --wait` and leave it running. Record the time of each state it prints:
   - `pending_dns` to `issuing` (t2): when Cloudflare, Google and Quad9 all answer exactly our NS set. Minutes after the registrar publishes, often under an hour; `ssc domains show` lists what each resolver answers meanwhile.
   - `issuing` to `active` (t3): when the Google-managed wildcard certificate is active. Usually under an hour. Two hours here means the operator mail; record whether it arrived.
   - Every active admin of the org gets the `domain_active` mail (or `domain_failed`, with the reason). Record when it arrived.
   - A `failed` state ends the proof: record `failure`, fix it, and start again from step 2. The same suffix may be asked for again.

4. **Check the zone.** Run `dig +short NS tools.<domain>`, and `dig +short` for `CAA`, `MX` and `TXT tools.<domain>`, `TXT _dmarc.tools.<domain>` and `A anything.tools.<domain>`. Expect our NS set, `0 issue "pki.goog"`, `0 .`, `"v=spf1 -all"`, `"v=DMARC1; p=reject"`, and the cell's entry address. Nothing else is in the zone.

5. **Wait for the roll.** `ssc domains status` shows `ready_to_flip: yes` once every environment has rolled with the new origins (`rolling: 0`). Record the time (t4).

6. **Start the request loop** in a second terminal before the flip, for one prod app `<app>` of the org, writing one line per second:

   ```sh
   D=<default suffix>; C=tools.<domain>; A=<app>
   while true; do
     for h in "$A.$D" "$A.$C"; do
       printf '%s %s %s\n' "$(date -u +%T)" "$h" \
         "$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "https://$h/")"
     done
     sleep 1
   done | tee flip-loop.txt
   ```

   Before the flip, expect `302` to `<auth URL>/login` on the default host and `404` on the customer host. In a browser, sign in to `https://<app>.<default suffix>/` and leave the tab open.

7. **Flip** (t5): `ssc domains flip --confirm`. Expect `Your apps now live under tools.<domain>` and the default suffix redirecting for 30 days. Within a few seconds the loop shows `307` to `https://<app>.tools.<domain>/` on the default host and `302` to login on the customer host. The gateway reads the snapshot every 2 seconds, so for a few seconds the two answers may mix. Expect no `5xx` and no `200` on the customer host without a session. Record the time of the first and of the last old-style answer.

8. **First app on the new host.** Reload the browser tab: it gets one `307` to the customer host, then one sign-in (sessions are per host, decision 023 amendment), then the app. Record whether it was exactly one sign-in. A second reload does not ask again. Run `curl -sI https://<app>.<default suffix>/x?y=1`: expect `307`, `location: https://<app>.tools.<domain>/x?y=1` and `cache-control: no-store`.

9. **Flip back** (t6): `ssc domains flip --back --confirm`. The loop now shows the reverse: `307` from the customer host to the default one, and `302` to login on the default host. The browser asks for one more sign-in on the default host.

10. **Re-flip** (t7): wait for `ready_to_flip: yes` again, then `ssc domains flip --confirm`. Expect the answers of step 7. Stop the loop. The org now stays on `tools.<domain>`, with the default suffix as its previous one, for the seven nights.

## Evidence

Copy the times into **Results** below and commit them. Put everything else in one folder named for the date, attached to SSC-111:

- `times.md`: the domain, registrar, suffix, NS set, and t0 to t7 with every state change from step 3. Note the operator mail if one came, and when each admin mail arrived.
- `ssc domains show --json` output after steps 2, 3, 7, 9 and 10.
- The `dig` output of step 4.
- `flip-loop.txt`, with the times of steps 7, 9 and 10 marked.
- The `curl -sI` output of step 8.
- The org's audit events from t0 on (`domain.requested`, `domain.flipped` three times), exported from the console.
- The agent's log from step 2, showing no `403`.
- The nightly evidence (`evidence-probes-<n>` artifact, `probes.json`) of each of the seven nights.

## Results

Not yet run. Times are UTC; durations count from the step before.

| Date | Suffix | NS added (t1) | `issuing` (t2) | `active` (t3) | Ready (t4) | Flip (t5) | Last old answer | Back (t6) | Re-flip (t7) | 5xx in loop | Sign-ins at flip | Nights counted |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## The seven-night rule

The night after step 10 is night one. A night counts when the page is green and the `suffixes` result in that cell's `probes.json` passes. Its reason must name `tools.<domain> (current)` with `CAA present`, and the default suffix as `(previous)`. The walk checks each active app host on both suffixes (a `200` or `302` to login on the current one, a no-store `307` on the previous), a certificate with 14 or more days left on each, and the CAA record on the customer suffix. It never asks for CAA on the default suffix, which has none of its own.

A red night, or a night with no `suffixes` result, starts the count again. The default suffix stays a previous one for 30 days after step 10, so it appears every night. Do not flip again during the seven nights.

After seven counted nights, and not before, change `landing/CLAIMS.md`: the dated "built and unproven" line becomes a claim whose proof is this folder, and the landing FAQ may answer "can apps live on our own domain". Then mark CD-7.2 done in the plan's status table.

## Afterwards

Keep the domain delegated if it is cheap to keep: it is the walk's standing customer suffix. To end it, in this order:

1. Flip back to the default suffix (`ssc domains flip --back --confirm`).
2. Undelegate: remove the NS records for the suffix at the registrar, and wait out their TTL. `dig NS <suffix> @1.1.1.1` (and `@8.8.8.8`, `@9.9.9.9`) must show `status: NOERROR` or `NXDOMAIN` and no longer name the zone's servers; an empty `dig +short` proves nothing, since a `SERVFAIL` prints nothing too.
3. Ask the operator to retire the suffix (`POST /internal/v1/orgs/{org_id}/domain/retire`). The retire asks the three public resolvers again first: it is refused ("the parent still delegates") while any of them still names the zone's servers, and refused when one of them does not answer (decision 004 amendment, Retention). Once retired, the name stays reserved to the org (`docs/trust/domain-limits.md`).

The retire empties the zone and keeps it. `python -m ssc_infra.onboard <label> --destroy` refuses the cell until both steps 2 and 3 are done.
