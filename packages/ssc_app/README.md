# ssc-app

The tiny helper an app installs to read the SSC identity note (`X-SSC-Identity`) and to reconnect streams before the gateway's limit. Depends on `pyjwt[crypto]` and `ssc-contracts` only.

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

## Reconnect before the limit

Each WebSocket or event stream ends at the limit the gateway reports (5 minutes for most apps, 60 for session apps). `ssc_app.reconnect` reads the gateway's `X-SSC-Request-Deadline` header and ends the stream a little before then; the browser reconnects without a reload. No extra dependency.

```python
from ssc_app.reconnect import browser_client, close_before_deadline, end_before_deadline

# Event stream: EventSource reconnects by itself, with Last-Event-ID if your events carry an id.
return StreamingResponse(
    end_before_deadline(events(), request.headers), media_type="text/event-stream"
)

# WebSocket: closed with code 1012, 30 seconds before the limit.
await ws.accept()
restart = asyncio.create_task(close_before_deadline(ws.close, ws.headers))
try:
    ...
finally:
    restart.cancel()

# The browser side: serve browser_client(), then sscSocket(url, {onopen, onmessage}) instead of new WebSocket(url).
```

`seconds_left(request.headers)` gives the seconds until the limit, or None without the gateway. Options: `margin` (30 s) on both, `retry_ms` (1000) on `end_before_deadline`. A stream that opens with less than the margin left is ended after half the time left (`seconds_to_wait`), never at once. A reconnect is a new connection: keep what must outlive it in Postgres or in the browser. The Node helper `@delimitus/ssc-reconnect` has the same names and runs the same vectors (`conformance/reconnect/vectors.json`).
