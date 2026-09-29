# Identity note v1 (`X-SSC-Identity`)

The one fixed way an app learns who the user is. Frozen contract (SSC-020, decision 010). It changes by adding v2 beside it, never by editing v1. Implementations: `ssc_contracts.identity` (claims), `ssc_edge.identity_note` (mint), `ssc_app.identity` (Python verifier), `helpers/node/ssc-identity` (Node verifier), `conformance/identity_note/` (shared test vectors).

## Wire form

The gateway attaches one request header, `X-SSC-Identity`, holding a compact JWS. Any incoming `X-SSC-*` header from the outside is stripped first (SSC-018), so an app never sees one it did not get from the gateway.

| JOSE header | Value | Rule |
|---|---|---|
| `alg` | `ES256` | Only algorithm. `none`, HMAC and RSA are refused before the key is looked up. |
| `typ` | `ssc-id+jwt` | Required. A plain `JWT` is refused: it stops a token minted for another purpose being replayed as an identity note. |
| `kid` | key id | Required. Selects one of at most two keys in the cell JWKS. |

## Claims

| Claim | Type | Meaning |
|---|---|---|
| `iss` | string | The cell issuer, `https://keys.delimitus.com/<cell_label>`. |
| `aud` | string | This app's exact origin, `https://<host>`; no path, no trailing slash. One string, never a list. |
| `sub` | string | `usr_…` for a person, `sch_…` for a schedule run. **Never an email.** |
| `iat`, `exp` | int | Unix seconds. `exp = iat + 300`. A verifier refuses `exp - iat > 300` even if not yet expired. |
| `org` | string | `org_…`. |
| `app` | string | `app_…`. |
| `env` | string | `prod` or `preview`. |
| `role` | string | `builder` or `user` for a person (from the sharing rule that admitted them); `schedule` for a schedule run. |
| `groups` | list of string | `grp_…` ids, at most 50, no repeats, and only the groups the app's sharing rule references. May be empty. |
| `name` | string | Display name. Persons only. |
| `email` | string | Display address. Persons only. |

No other claim is allowed; a note with an unknown claim is refused. Schedule notes carry no `name` and no `email`. Optional claims are omitted, never sent as `null`.

## The two rules every app must keep

1. **Key on `sub`, never on `email`.** `sub` is stable for the life of the user. `email` and `name` are whatever the company directory says today; they change on marriage, rename and domain moves, and a schedule run has neither. A table keyed by email will merge two people or lose one.
2. **Verify once per request, when it arrives.** Do not re-verify inside a long WebSocket or event stream. The note expires after five minutes, and a Streamlit-class stream lives up to an hour. Ending open streams when access is revoked is the kill switch's job (drain within 60 seconds, SSC-025); an app that re-checks mid-stream only breaks its own stream.

## Keys

- The cell signing key is an EC P-256 private key in the cell's Secret Manager, loaded by the gateway at start. It never appears in a snapshot, a log or the control database.
- The public half is a JWKS at `<iss>/jwks.json`, a static file in the cell bucket behind a CDN, so verification never depends on the gateway being up. Cache it; refetch when a `kid` is unknown.
- Rotation: publish the new key beside the old one, start signing with the new `kid`, remove the old key after a day. The JWKS therefore holds one or two keys.

## Refusal codes

Both helpers raise one exception type, `IdentityRefused`, with a `code` from this closed list. The checks run in this order and stop at the first failure, so the same bad token gets the same code from both helpers.

| Code | When |
|---|---|
| `missing` | no header, or an empty one |
| `malformed` | not three base64url segments of JSON |
| `wrong_type` | `typ` is not `ssc-id+jwt` |
| `wrong_algorithm` | `alg` is not `ES256` |
| `unknown_key` | no `kid`, or no such key in the JWKS |
| `bad_signature` | the signature does not verify (tampered, or signed by a foreign key) |
| `wrong_audience` | `aud` is not this app's origin |
| `wrong_issuer` | `iss` is not the pinned issuer (only when the app pins one) |
| `expired` | `now >= exp + leeway` |
| `not_yet_valid` | `iat > now + leeway` |
| `ttl_too_long` | `exp - iat > 300` |
| `bad_claims` | anything the claim table above refuses: email as subject, 51 groups, name on a schedule note, unknown role, unknown claim |

Treat every code the same way: this request is not from a signed-in user of this app. Log the code, answer 401 or your framework's equivalent, never echo the token. Default clock leeway is 30 seconds in both helpers.

## Using the helpers

Python (`ssc_app`, depends on `pyjwt[crypto]` only):

```python
from ssc_app.identity import IdentityRefused, IdentityVerifier

verifier = IdentityVerifier(
    audience="https://quiet-river-7f3k.delimitusapps.com",
    keys="https://keys.delimitus.com/cell-01/jwks.json",
)


def who(request):
    try:
        note = verifier.from_headers(request.headers)
    except IdentityRefused as e:
        return None, e.code
    return note.sub, None  # key on note.sub; note.name and note.email are for display
```

Node (`@delimitus/ssc-identity`, zero dependencies, Node 22+):

```js
import { IdentityRefused, IdentityVerifier } from '@delimitus/ssc-identity';

const verifier = new IdentityVerifier({
  audience: 'https://quiet-river-7f3k.delimitusapps.com',
  keys: 'https://keys.delimitus.com/cell-01/jwks.json',
});

app.use(async (req, res, next) => {
  try {
    req.user = await verifier.fromHeaders(req.headers); // key on req.user.sub
    next();
  } catch (e) {
    if (e instanceof IdentityRefused) return res.status(401).end();
    throw e;
  }
});
```

`ssc init` (SSC-022) writes both snippets into an app's agent pack so a coding agent uses them without being told.

## Test vectors

`conformance/identity_note/vectors.json` holds the JWKS, a fixed `now`, two app origins and 33 cases: six that verify and 27 that must be refused with a named code (wrong audience, expired, not yet valid, foreign key, unknown `kid`, missing `kid`, tampered payload, `alg` none, HS256, wrong or missing `typ`, TTL over 300 s, 51 groups, email as subject, schedule note with email or name, unknown role, unknown claim, malformed tokens). Regenerate with `uv run python conformance/identity_note/generate.py`; the signing keys beside it are throwaway test keys. Both helpers run every case in CI, and a Python test checks that regenerating gives the same cases.

## Still owed by other tickets

- SSC-013: create the signing key in Secret Manager, publish `jwks.json` to the cell bucket and CDN under `keys.delimitus.com/<cell_label>`, and rotate.
- SSC-018: mint on every admitted request with `ssc_edge.identity_note.compose_note` and `sign_note`, filling `role` and `groups` from the sharing-rule evaluation and stripping inbound `X-SSC-*` headers.
- SSC-050: the data gateway accepts this same note with the calling app's origin as `aud`, alongside the app's own workload token.
- Open design item (build plan §3.3): a bounded stream token for app-to-data-gateway calls from long streams.
