# Sign-in and directory: known limits

Part of the trust pack (GA-11.4). Each line is a fact from decision 024 or a measured check;
none is a promise beyond what is written here.

## Supported identity provider

- V1 supports Okta (SAML sign-in, SCIM directory) through WorkOS. Google Workspace sign-in worked
  in the SSC-002 spike, but its lockout and rename checks were not run on SSC's own stack, so it is
  not offered in V1. Microsoft Entra has not been tested. The code itself refuses only consumer
  sign-in (any OAuth type and magic links); the operator chooses each org's connection.

## Removing access

- Deactivating or unassigning a person in Okta ends their SSC access: every session is revoked,
  the API and the command line refuse them at once, and app gateways refuse older sessions within
  one snapshot (at most 5 s after SSC records the deactivation).
- End to end, measure from the Okta action: Okta to WorkOS, then up to 60 s for SSC's directory
  sync, then the 5 s above. Measured 2026-10-01: SSC deactivated the person 23 s (Deactivate) and
  11 s (unassign) after the WorkOS event.
- Okta **Suspend** never reaches WorkOS, so a suspended person keeps SSC access. Use Deactivate
  or unassign the app.
- SSC revokes its own sessions and command-line tokens; WorkOS does not (measured in SSC-002).

## Matching a login to a person

- Okta logins match by the directory's `idp_id`, so an email change keeps the same person
  (checked 2026-10-01).
- A login that matches no person, or several, is refused and listed for admins:
  `ssc logins list`, then `ssc logins link <id> --to <email or usr_ id>`. A link is stored and
  audited.
- Google Workspace (not in V1): the login matches by email, so after a rename the login fails
  until the directory catches up, and an address-shaped subject cannot be linked by hand.

## Abuse limits

The auth host counts sign-in requests in a sliding hour, before it reads the database or calls
WorkOS, by the address the control load balancer saw (decision 024, amendment of 2026-10-19).

| Route | Counted | Limit an hour | Answer when over |
|---|---|---|---|
| `GET /login` | every call, per address, then per org | 60 per address, 600 per org | the "Too many tries" page, `429` |
| `GET /callback` | every call, per address | 60 | the page, `429` |
| `GET /callback` | refused sign-ins only, per address | 20, then the next call is answered without WorkOS or an audit row | the page, `429` |
| `POST /device` (the user code) | every call, per address, then per org | 10 per address, 60 per org | the page, `429` |
| `POST /token` | every call, per address | 2 000 | `429` JSON |
| `POST /token` | `invalid_grant` answers only, per address | 30, then none until the hour slides | `429` JSON |
| `POST /revoke` | every call, per address | 60 | `429` JSON |
| `POST /internal/redeem` | codes not redeemed, per org | 60, then none for that org | `429` JSON |

- The `429` JSON is `{"error": "invalid_request", "error_description": "too many token requests"}`
  with `retry-after: 3600`.
- A command-line login polls `/token` every 5 s for at most 10 minutes, 120 calls, so several
  logins behind one network address stay well inside 2 000.
- The counts are kept in each auth-host instance's memory. At most two instances run, so a
  client can reach at most double each limit; a restart starts the counts again.
- Everyone behind one network address (an office NAT) shares that address's counts.
- A person who is limited waits up to an hour, or signs in from another network. There is no
  operator override.
- `/register`, the work-email step, `/device/authorize` and `/ci-tokens` keep their own limits
  (`ssc_control.identity.limits`).

## Changing directories

- Rebinding an org to a new directory does not carry people over: they are new people in SSC,
  and sharing rules naming the old ones must be set again.
