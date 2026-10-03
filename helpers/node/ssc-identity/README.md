# @delimitus/ssc-identity

Read the identity note the SSC gateway attaches to every request as `X-SSC-Identity`. Zero dependencies; Node 22 or newer (uses `node:crypto` webcrypto and `fetch`).

```js
import { IdentityRefused, IdentityVerifier } from '@delimitus/ssc-identity';

const verifier = new IdentityVerifier({
  audience: 'https://quiet-river-7f3k.delimitusapps.com',   // this app's exact origin
  keys: process.env.SSC_IDENTITY_KEYS_URL,  // a data: URL in a cell; or a JWKS URL or object
});

const note = await verifier.fromHeaders(req.headers);
note.sub     // 'usr_…' or 'sch_…': key on this
note.name    // display only; null for a schedule run
note.email   // display only; null for a schedule run
note.role    // 'builder' | 'user' | 'schedule'
note.groups  // ['grp_…'], at most 50
```

Two rules:

- **Key on `sub`, never on `email`.** `email` and `name` are display strings the company directory can change; `sub` never changes.
- **Verify once, when the request arrives.** Do not re-verify inside a long WebSocket or event stream. The note expires after five minutes; ending open streams is the platform kill switch's job.

Every refusal is an `IdentityRefused` with a `code` (`missing`, `malformed`, `wrong_type`, `wrong_algorithm`, `unknown_key`, `bad_signature`, `wrong_audience`, `wrong_issuer`, `expired`, `not_yet_valid`, `ttl_too_long`, `bad_claims`). Treat them all as "not a signed-in user of this app": answer 401 and never echo the token.

The full contract is `docs/contracts/identity-note.md` in the SSC repository. `npm test` runs the shared vectors in `conformance/identity_note/vectors.json`, the same ones the Python helper `ssc_app.identity` runs.
