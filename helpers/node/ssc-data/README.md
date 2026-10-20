# @delimitus/ssc-data

Read a database your org connected, from an SSC app. Zero dependencies; Node 22 or newer. An ES module; a CommonJS app can `require()` it on Node 22.12 or newer.

```
npm install @delimitus/ssc-data
```

Name the connection in `ssc.toml`; an org admin grants your environment the connection (`ssc connections` shows what it has):

```toml
[connections]
names = ["finance"]
```

For a `postgres` connection:

```js
import { query } from '@delimitus/ssc-data';

const result = await query('finance', 'select id, total from invoices where year = $1', [2026]);
for (const row of result.rows) console.log(row); // an array in column order
```

The call carries the app's own identity, so the app holds no database credentials. The statement is one read, and nothing can write. Its form follows the connection's kind: one `SELECT` for `postgres` (`$1`, `$2`, ... placeholders) and for `mysql`, `sqlserver`, `bigquery` and `snowflake` (`?` placeholders), with at most 1,000 parameters (strings, numbers, booleans, null); and, with no parameters, an A1 range for `gsheets`, `list <prefix>` or `get <key>` for `s3` and `gcs`, `<table>[ view <view>][ where <formula>]` for `airtable` and a GET path for `rest`.

- `{ maxRows, maxBytes, timeoutMs }` only narrow what the platform, the connection and the grant allow. `result.truncated` and `result.truncatedReason` say the gateway stopped reading before the result ended.
- To act for the signed-in user, pass the `X-SSC-Identity` value of the request you are answering as `{ identity }`. Without it the app acts for itself.
- A decimal is a string, a date or timestamp is ISO 8601, bytes are base64.

Errors are `DataError` with a `code`: the data gateway's (`CONNECTION_NOT_GRANTED`, `CONNECTION_SUSPENDED`, `QUERY_REFUSED` with a `reason`, `QUERY_FAILED` with a `sqlstate`, `DAILY_BUDGET_SPENT`, `APP_NOT_ACTIVE`, ...) or `UNREACHABLE`. A `reason` is `other_database`, `not_read_only`, `multiple_statements` or `unsupported_statement`. The data gateway starts from zero, so a query is tried once more on a lost connection or a 502, 503 or 504; one that timed out is not.

The gateway's address comes from the metadata server; `SSC_DATAGW_URL` replaces it. The Python helper is `ssc_app.data`, with the same names (`max_rows`, `max_bytes`, `timeout_ms`). The contract is `docs/contracts/data-gateway.md`.
