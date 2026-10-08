# App access, logs and audit: known limits

Part of the trust pack (GA-11.4). Each line is a fact from the code or a measured check; none is a
promise beyond what is written here. Decided for MVP V1 by the founder, 2026-10-08 (GA-3.6, 3.8).

## Taking access away

- Removing a sharing rule or deactivating a person is recorded at once by the API, which then
  refuses that person. App gateways learn it from the next access snapshot: 2.0 to 3.4 s from the
  command to the snapshot, measured on 2026-10-07 (T8).
- There is no faster "revoke now" path that skips the snapshot. It is on the V2 backlog.
- A request already in flight when access is removed can run on for a few seconds. A disabled
  app's new file links are refused; a link issued before the disable keeps working until it
  expires, at most 10 minutes later.

## Request limits

- The API limits each login or token: bursts of up to 60 requests, refilled at 1 per second, per
  API instance. A caller over the limit gets `429` with `Retry-After`.
- App gateways have **no per-person rate limit** in V1. What bounds load is fixed: each gateway
  instance takes at most 1000 requests at once, and a cell runs at most 20 gateway instances by
  default. One person can use that capacity, up to those bounds, until access is removed. A
  per-person gateway limit is on the V2 backlog.

## Logs

- `ssc logs --follow` shows a line 12 to 17 s after the app writes it, measured on cell 1 on
  2026-10-07. The delay is Cloud Logging's: a line becomes readable that long after it is
  written. SSC looks back 60 s, so a late line is still shown, once.
- Only an app's builders, its owner and org admins can read its logs. A person with the `user`
  role on an app is refused (`403 FORBIDDEN`).

## Audit

- Every change is in one hash-chained audit log per org. `ssc audit export` downloads it (org
  admins), and `ssc audit verify FILE` recomputes every hash offline with no login. An anchor of
  the chain's head is written to the cell's storage once a day. Checked on cell 2, 2026-10-08:
  238 events intact, and the export agreed with that day's anchor.
