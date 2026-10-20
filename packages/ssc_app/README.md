# ssc-app

The tiny helper an app installs to read the SSC identity note (`X-SSC-Identity`), to reconnect streams before the gateway's limit, to keep files and to read company data.

```
pip install ssc-app
```

Python 3.11 or later. Depends on `pydantic` (2.5 or later) and `pyjwt[crypto]` (2.15 or later) only, both as ranges, so the app keeps its own pins.

```python
import os

from ssc_app.identity import IdentityRefused, IdentityVerifier

verifier = IdentityVerifier(
    audience=os.environ["SSC_APP_ORIGIN"],  # this app's exact origin, preview's included
    keys=os.environ["SSC_IDENTITY_KEYS_URL"],  # data: URL in a cell; else JWKS URL or dict
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

## Files

Ask for file storage with `[files]` in `ssc.toml`; the first deploy that asks sets it up once, in a few minutes. `ssc_app.files` asks the cell's data gateway for a signed link with the app's own ID token and sends the bytes to Cloud Storage; the app holds no storage credentials. No extra dependency.

```python
from ssc_app import files

files.put("photos/cat.png", data, content_type="image/png")
data = files.get("photos/cat.png")
url = files.link("get", "photos/cat.png")["url"]  # redirect a browser here to download
files.delete("photos/cat.png")
```

Each environment has its own files: at most 25 MB each and 1 GB together. A name is `/`-separated segments of `A-Z a-z 0-9 . _ -`, each starting with a letter or digit. A link lasts 10 minutes, and a download always arrives as an attachment, never shown as a page. Upload from the server: the bucket allows no browser-direct upload. Files are not scanned for viruses. Errors are `FilesError` with the data gateway's `code` (`FILE_NOT_FOUND`, `FILES_QUOTA_EXCEEDED`, `APP_NOT_ACTIVE`, ...), `STORAGE_<status>`, `FILE_TOO_LARGE` (over the link's `max_bytes`, refused before anything is sent), `UNREACHABLE` (the gateway, Cloud Storage or the app's own ID token could not be had) or `BAD_GATEWAY_RESPONSE` (an answer that is not JSON); the data gateway starts from zero, so a call to it is tried once more on a timeout, a lost connection or a 502, 503 or 504. Delete is idempotent: a retried delete that finds nothing succeeds. The gateway's address comes from the metadata server (`SSC_DATAGW_URL` replaces it). The Node helper `@delimitus/ssc-files` has the same names, with `remove` for `delete`. Contract: `docs/contracts/data-gateway.md#files`.

## Company data

Name a connection in `[connections] names` in `ssc.toml`; an org admin grants the environment that connection. `ssc_app.data.query` sends one read-only statement to the cell's data gateway with the app's own ID token; the app holds no database credentials. No extra dependency. For a `postgres` connection:

```python
from ssc_app import data

result = data.query("finance", "select id, total from invoices where year = $1", [2026])
for row in result.rows:  # a list in column order
    ...

schema = data.describe("finance")  # describe(name) -> Schema(connection, kind, tables)
for table in schema.tables:  # Table(name, columns), each column {name, type, db_type}
    ...
```

`data.describe(name)` names the tables of a granted connection and their columns, typed as a query types them (none for a file not yet read); the data gateway keeps a description five minutes, and a refusal is a `DataError` as for a query.

The statement follows the connection's kind: one `SELECT` for `postgres` (`$1`, `$2`, ... placeholders) and for `mysql`, `sqlserver`, `bigquery` and `snowflake` (`?` placeholders); and, with no parameters, an A1 range for `gsheets`, `list <prefix>` or `get <key>` for `s3` and `gcs`, `<table>[ view <view>][ where <formula>]` for `airtable` and a GET path for `rest`. `max_rows`, `max_bytes` and `timeout_ms` only narrow what the platform, the connection and the grant allow; `result.truncated` and `result.truncated_reason` say the gateway stopped reading early. To act for the signed-in user pass the request's `X-SSC-Identity` value as `identity=`; without it the app acts for itself. A decimal is a string and a timestamp is ISO 8601. Errors are `DataError` with the data gateway's `code` (`CONNECTION_NOT_GRANTED`, `CONNECTION_SUSPENDED`, `QUERY_REFUSED`, `QUERY_FAILED` with a `sqlstate`, `DAILY_BUDGET_SPENT`, ...), `UNREACHABLE` (the gateway or the app's own ID token could not be had) or `BAD_GATEWAY_RESPONSE` (an answer that is not JSON); a query is tried once more on a lost connection or a 502, 503 or 504, and one that timed out is not. The Node helper `@delimitus/ssc-data` has the same names with `maxRows`, `maxBytes` and `timeoutMs`. Contract: `docs/contracts/data-gateway.md`.
