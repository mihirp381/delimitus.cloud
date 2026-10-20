# @delimitus/ssc-identity

Read the identity note the SSC gateway attaches to every request as `X-SSC-Identity`. Zero dependencies; Node 22 or newer (uses `node:crypto` webcrypto and `fetch`). An ES module; a CommonJS app can `require()` it on Node 22.12 or newer.

```
npm install @delimitus/ssc-identity
```

```js
import { IdentityRefused, IdentityVerifier } from '@delimitus/ssc-identity';

const verifier = new IdentityVerifier({
  audience: process.env.SSC_APP_ORIGIN,     // this app's exact origin, preview's included
  keys: process.env.SSC_IDENTITY_KEYS_URL,  // a data: URL in a cell; outside one a JWKS URL or object
});

const note = await verifier.fromHeaders(req.headers);
note.sub     // 'usr_…' or 'sch_…': key on this
note.name    // display only; null for a schedule run
note.email   // display only; null for a schedule run
note.role    // 'builder' | 'user' | 'schedule'
note.groups  // ['grp_…'], at most 50
```

Make one `IdentityVerifier` per process: it fetches a JWKS URL once and caches it. The top-level `verify(token, { audience, keys })` takes the JWKS as an object only; the URL form is for `IdentityVerifier`.

Two rules:

- **Key on `sub`, never on `email`.** `email` and `name` are display strings the company directory can change; `sub` never changes.
- **Verify once, when the request arrives.** Do not re-verify inside a long WebSocket or event stream. The note expires after five minutes; ending open streams is the platform kill switch's job.

Every refusal is an `IdentityRefused` with a `code` (`missing`, `malformed`, `wrong_type`, `wrong_algorithm`, `unknown_key`, `bad_signature`, `wrong_audience`, `wrong_issuer`, `expired`, `not_yet_valid`, `ttl_too_long`, `bad_claims`). Treat them all as "not a signed-in user of this app": answer 401 and never echo the token.

`SSC_APP_ORIGIN` is the app's exact origin, such as `https://expenses.<cell label>.delimitusapps.com` (`https://expenses--preview.<cell label>.delimitusapps.com` in preview). Code outside SSC may pass `https://keys.delimitus.com/<cell label>/jwks.json` as `keys`. The note is an ES256 JWT (`typ: ssc-id+jwt`). This helper and the Python one, `ssc_app.identity`, are tested against the same shared vectors, so the same token gets the same answer from both.
