# App access, logs and audit: known limits

Part of the trust pack (GA-11.4). Each line is a fact from the code or a measured check; none is a
promise beyond what is written here. Decided for MVP V1 by the founder, 2026-10-08 (GA-3.6, 3.8; the internet-access section, GA-6.4).

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

- `ssc logs --follow` shows a line 12 to 26 s after the app writes it: measured on cell 2 on
  2026-10-08, 15 lines from 7 browser requests, median 16.8 s, slowest 25.4 s, none lost. Cloud
  Logging received each line within 5 s (most within 0.3 s); the rest is the time until a line
  can be read back, which arrived in batches about 11 s apart. SSC looks back 60 s, so a late
  line is still shown, once.
- Only an app's builders, its owner and org admins can read its logs. A person with the `user`
  role on an app is refused (`403 FORBIDDEN`).

## Audit

- Every change is in one hash-chained audit log per org. `ssc audit export` downloads it (org
  admins), and `ssc audit verify FILE` recomputes every hash offline with no login. An anchor of
  the chain's head is written to the cell's storage once a day. Checked on cell 2, 2026-10-08:
  238 events intact, and the export agreed with that day's anchor.

## Internet access

- The internet allowlist is **org-wide**. Every app in the org can reach every allowed host
  through the cell's egress proxy; there is no per-app list. The proxy checks that the caller is
  an active app environment with a valid credential, then whether the host is on the org's list;
  it does not check which app asked. An app's `[egress] hosts` in `ssc.toml` is compared with the
  list at deploy and a host the list lacks is reported as a change; it does not restrict what the
  app can reach once another host is allowed. [envoy.py:10-20; capabilities.py:105-109]
- Each app environment has its own proxy credential, a random token (256 bits) that lives only in
  the environment's `HTTPS_PROXY` secret. The org's access snapshot carries **SHA-1** digests of
  these tokens (Envoy's `basic_auth` accepts only that form), never the tokens, and the proxy
  reads them from the snapshot in the cell's bucket. SHA-1 is a weak hash for chosen or guessable
  passwords and for collisions; neither applies to a 256-bit random token, so a reader of the
  snapshot cannot recover a token from its digest. What a reader does see is which environments
  hold a credential and the allowed host names. The bucket enforces no public access, and the
  roles that read it are the cell's gateway, agent, data gateway and proxy accounts (the
  control plane's worker writes it).
  [egress.py:165-178; access-snapshot.md (SSC-053); cell.py bucket and `bucket-proxy`]
- The proxy's machine image is not pinned. It boots from the `cos-stable` family, so a new or
  replaced machine gets the newest stable Container-Optimized OS. Automatic updates are off on
  the machine, which is patched by replacing it. Pinning an exact image is not in V1.
  [cell.py:69, 1952; infra/README.md, Egress proxy]
