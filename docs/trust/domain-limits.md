# Your own address: known limits

Part of the trust pack. Each line is a fact from the code or from decision 004's amendment of
2026-10-28 (founder decisions of 2026-10-26); none is a promise beyond what is written here. The
feature is built and tested locally and has not yet been run against a live domain
(`docs/runbooks/ssc-111-customer-domain-proof.md`); the landing page makes no claim about it.

## What we run

- Your apps live under one name per organization: by default `<label>.delimitusapps.com`, where
  the label is random and never your name. You may instead give us one subdomain of your own, for
  example `tools.testorg.com`, and your apps move to `ledger.tools.testorg.com` and
  `ledger--preview.tools.testorg.com`. One per organization, and no per-app names under your
  domain (aliases are not offered in V1). [decision 004, amendment 2026-10-28]
- Your IT adds the NS records we show, once, in the zone of the parent (`testorg.com`). Nothing
  else in your DNS is read or written: we hold no credential for your DNS provider. [`ssc domains
  add`, the console's "Your own address"]
- In your organization's cell project we run one public Cloud DNS zone for that subdomain and
  write only these records, each with a TTL of 300 s:
  - `*.tools.testorg.com` A, your cell's entry address;
  - one `_acme-challenge` CNAME that lets Google's certificate authority check the name;
  - at `tools.testorg.com`: CAA `0 issue "pki.goog"`, so no other public authority may issue for it;
  - the mail records below.
  [ssc_agent domains.py, `_records`]
- One certificate covers `*.tools.testorg.com` and `tools.testorg.com`, issued and renewed by
  Google Certificate Manager. [ssc_agent domains.py, `create`]
- Refused names, each with its own reason: fewer than three labels, a public suffix or your
  registered domain itself, any name under `delimitus.com` or `delimitusapps.com`, a name your
  organization already uses or used, and a name equal to, above or below one another
  organization uses, used or delegated. Asking for a name does not reserve it: a request that
  failed or was retired without ever being delegated leaves the name free for any organization.
  [ssc_control domains/admission.py]

## Mail

- At `tools.testorg.com` we write a null MX (`0 .`), SPF `v=spf1 -all` and, at
  `_dmarc.tools.testorg.com`, DMARC `v=DMARC1; p=reject`: no mail is accepted for that name, and
  receivers are told to reject mail that claims to come from it.
- Hosts under it (`ledger.tools.testorg.com`) have only the A record. A receiver that follows RFC
  7489 looks for a host's DMARC policy at the host and then at your registered domain
  (`testorg.com`), not at `tools.testorg.com`, so your own domain's policy governs mail that
  claims to come from an app host.

## Certificate transparency

- Every public certificate is written to certificate transparency logs, which anyone can search.
  Yours names `*.tools.testorg.com` and `tools.testorg.com`, so the subdomain you choose is public
  from the day the certificate is issued. The default label named no customer.
- App names do not appear: the certificate is a wildcard, so `ledger` is never logged.

## Cookies and forms

- Apps under your subdomain share a site with your other `*.testorg.com` systems, and browsers
  treat requests between them as same-site. What the gateway does about it (it drops app cookies
  that name a `Domain`, and limits where an app's forms may post and who may frame it) and what it
  does not cover are in [App access, "Forms and cookies on your own domain"](app-access-limits.md#forms-and-cookies-on-your-own-domain).
- For apps that handle sensitive data, and for regulated customers, we recommend a domain you
  register only for Delimitus (`testorg-tools.com`), delegating a name under it
  (`apps.testorg-tools.com`): the apps then share a site with nothing of yours. The mechanics are
  the same. [decision 004, Q6]

## Moving and the 30-day redirect

- The move is one confirmed step by an organization admin, never by an AI agent, once every app
  is ready. All apps move at once, in one access snapshot. [`POST /v1/domain/flip`]
- For 30 days after a move, the old addresses answer `307` to the same app, path and query
  under the new name, with `Cache-Control: no-store` and no cookie; after that they are not found
  (`404`). Moving back works the same way in reverse. Links in old mail or bookmarks stop working
  after the 30 days.
- Sign-in sessions belong to a host, so after a move each person signs in once more on each app's
  new address. If their Delimitus sign-in is still live, that is a redirect with no password
  prompt. A scheduled call that meets the redirect is sent once more to the new address.

## After you leave: zone retention and the takeover guard

- If your NS records still point at our zone after you stop using it, and the zone were deleted,
  anyone able to create a zone of the same name on the same name servers could serve your
  subdomain. So we keep the zone: a retire asks the public resolvers again at that moment and
  is refused while any of them names our servers, or when one of them does not answer, for 12
  months after the delegation was last seen. Once your NS records are gone, the zone can be
  emptied straight away. [ssc_control domains/lifecycle.py, `recheck` and `retire_allowed`]
- Retiring empties the zone (the certificate, its map entry, its authorization and every record
  but NS and SOA are deleted) and keeps the zone itself for as long as your organization's cell
  exists, so a delegation you forget still points at a zone only we hold. Remove the NS records at
  your parent when you leave: we will not shut down your cell while the public resolvers still see
  them.
- A delegated name is never given to anyone else: a retired subdomain that was ever delegated is
  kept on record for your organization (`ssc.org_suffix_retired`, its time set to the retire) and no other organization
  may ask for it, a name above it or a name below it. Any future clean-up of that table must keep
  those rows. [packages/ssc_control db/README.md, revision 0046]
