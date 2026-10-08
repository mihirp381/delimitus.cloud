# Sign-in and directory: known limits

Part of the trust pack (GA-11.4). Each line is a fact from decision 024 or a measured check;
none is a promise beyond what is written here.

## Supported identity provider

- V1 supports Okta (SAML sign-in, SCIM directory) through WorkOS. Google Workspace sign-in worked
  in the SSC-002 spike, but its lockout and rename checks were not run on SSC's own stack, so it is
  not offered in V1. Microsoft Entra has not been tested.

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

## Changing directories

- Rebinding an org to a new directory does not carry people over: they are new people in SSC,
  and sharing rules naming the old ones must be set again.
