# ssc-app

The tiny helper an app installs to read the SSC identity note (`X-SSC-Identity`). Depends on `pyjwt[crypto]` and `ssc-contracts` only.

```python
import os

from ssc_app.identity import IdentityRefused, IdentityVerifier

verifier = IdentityVerifier(
    audience="https://quiet-river-7f3k.delimitusapps.com",  # this app's exact origin
    keys=os.environ["SSC_IDENTITY_KEYS_URL"],  # a data: URL in a cell; or a JWKS URL or dict
)
note = verifier.from_headers(request.headers)
note.sub  # "usr_…" or "sch_…": key on this
note.name  # display only; None for a schedule run
note.email  # display only; None for a schedule run
```

Key on `sub`, never on `email`. Verify once per request; never re-verify inside a long WebSocket or event stream. Every refusal is `IdentityRefused` with a `code`; treat them all as "not a signed-in user of this app". Contract: `docs/contracts/identity-note.md`.
