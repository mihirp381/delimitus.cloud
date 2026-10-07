# Reconnect apps (SSC-090)

Two session apps, one per helper: `py` uses `ssc_app.reconnect` (copied from
`packages/ssc_app`), `node` uses `@delimitus/ssc-reconnect` (copied from `helpers/node`). Each
serves `/ws?after=<n>`, which sends n+1, n+2, ... once a second, and closes with 1012 before the
gateway's deadline through the helper. `check.py` is the client: it reconnects on 1012 with the
last number, as the browser client `sscSocket` does, and passes when the numbers run on with no
gap across the 60-minute mark. Each connection open is logged as `OPEN after=<n> left=<s>`.
