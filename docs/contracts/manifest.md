# `ssc.toml` v1 (`schema = "ssc/v1"`)

What an app asks the platform for. Contract of SSC-044 and decision 013: a draft until the founder's review, then frozen as the week-2 contract. Once frozen it changes by adding `ssc/v2` beside it, never by editing v1 (see [Versioning](#versioning)). Implementation: `ssc_contracts.manifest` (model, parser, writer), `ssc_contracts.capabilities` (the diff against what an environment grants), `ssc_shared.canonical.manifest_digest` (the digest).

A manifest is a **request, never enforcement**. A deploy that asks for something the environment does not grant still goes ahead; the builder sees the [capability diff](#capability-diff) and the platform decides each item.

**Pending changes (architecture review 2026-10-03, decision 025; not yet in the code or in this contract).** SSC-044 is reopened to add them, and nothing below changes until that ticket lands.
- **Session apps.** `sessions = true` stays the key (the architecture document writes `session`; the ticket settles the name). It will also be set by detection for Streamlit, Gradio, Dash and Shiny start commands. A session app runs on one instance, is instance-billed, and its connections end at 60 minutes.
- **`billing`.** A new optional `[runtime]` key, `"request"` or `"instance"`, with no default in the file: unset means the platform chooses (instance for session apps, request for the rest). To settle in SSC-044: an unset key must stay out of the digest so no v1 manifest changes digest, which makes this a widening change under [Versioning](#versioning). An agent session cannot change it (SSC-048).
- **State.** `postgres = true` stays the only state. SQLite on disk will be refused at build with `STATE_SQLITE_EPHEMERAL` (SSC-015), because the file system is memory and does not persist.
- **Files.** There is no manifest key for file storage yet; SSC-046 decides whether one is needed.

## Example

```toml
schema = "ssc/v1"

[runtime]
class = "medium"            # small | medium | large
port = 3000                 # the platform sets PORT to this
health_path = "/healthz"
start = "npm start"
sessions = false

[build]
public_names = ["API_URL"]  # extra names allowed in public_env

[build.public_env.prod]
VITE_SUPABASE_URL = "https://abc.supabase.co"
API_URL = "https://api.example.com"

[build.public_env.preview]
VITE_SUPABASE_URL = "https://preview.supabase.co"

[state]
postgres = true

[connections]
names = ["warehouse"]

[egress]
hosts = ["api.stripe.com"]

[[schedules]]
name = "nightly-report"
cron = "0 3 * * mon-fri"
timezone = "Europe/London"
path = "/tasks/nightly-report"
method = "POST"
timeout_seconds = 120
```

An app with no `ssc.toml` gets every default below (`default_manifest()`). A file that exists must start with `schema = "ssc/v1"`.

## Fields

Every table refuses unknown keys, and every value has one TOML type: `port = "8080"` or `sessions = "yes"` is refused, not coerced.

| Key | Type | Default | Rule |
|---|---|---|---|
| `schema` | string | required | Exactly `"ssc/v1"`. Any other value is refused by name, before other checks. |

`[runtime]`, how the app runs:

| Key | Type | Default | Rule |
|---|---|---|---|
| `class` | string | `"small"` | `small`, `medium` or `large`; see [Resource classes](#resource-classes). |
| `port` | integer | `8080` | 1 to 65535. The platform sets `PORT` to it (17 of 20 corpus apps never read `PORT`). |
| `health_path` | string | `"/"` | Starts with a single `/`, URL path characters only, no `?query` or `#`, at most 256 characters. |
| `start` | string | none | Start command, one line, at most 1024 characters (the Streamlit fix-it). |
| `sessions` | boolean | `false` | `true` for an app that keeps sessions in process memory: it then runs at most 1 instance. |

`[build]`, public build-time values:

| Key | Type | Default | Rule |
|---|---|---|---|
| `public_names` | array of string | `[]` | Extra names allowed in `public_env`, beyond `VITE_*` and `NEXT_PUBLIC_*`. Unique, at most 50. |
| `public_env` | table of tables | `{}` | `[build.public_env.<env>]` with `<env>` `prod` or `preview`; `NAME = "value"`, at most 50 per environment, values at most 4096 characters. |

A name is `^[A-Z][A-Z0-9_]{0,127}$` and must start with `VITE_` or `NEXT_PUBLIC_` (plus at least one more character) or be listed in `public_names`. These values are compiled into the browser bundle, so any name containing `SECRET`, `PASSWORD`, `PASSWD`, `SERVICE_ROLE` or `PRIVATE_KEY` is refused, and so are names the platform sets (`PORT`, `HOME`, `PATH`, `DATABASE_URL`, `SSC_*`). Secrets go through `ssc secrets`, never here.

`[state]`:

| Key | Type | Default | Rule |
|---|---|---|---|
| `postgres` | boolean | `false` | Ask for the environment's Postgres database (`DATABASE_URL`). |

Postgres is the only state offered. A key-value request (`kv`, `redis`, `valkey`, `memcached`, `cache`, `keyvalue`, `key_value`, in any case, or `state = "redis"`) is refused with the fix-it `STATE_KV_UNSUPPORTED`: set `postgres = true` and keep the data in a table; for a cache, an `UNLOGGED` table with an `expires_at` column.

`[connections]`:

| Key | Type | Default | Rule |
|---|---|---|---|
| `names` | array of string | `[]` | Company data connections by name, `^[a-z][a-z0-9-]{0,62}$` (the `ssc.connection.name` rule). Unique, at most 20. |

`[egress]`:

| Key | Type | Default | Rule |
|---|---|---|---|
| `hosts` | array of string | `[]` | Outbound hosts, exact lower-case DNS names with at least two labels. No scheme, path, port, IP address or wildcard. Unique, at most 50. |

`[[schedules]]`, at most 20, names unique. Each entry is what the timers port reads (`ssc_control.ports.DeclaredSchedule`):

| Key | Type | Default | Rule |
|---|---|---|---|
| `name` | string | required | `^[a-z][a-z0-9-]{0,62}$`. Schedules are matched by name across deploys. |
| `cron` | string | required | Five fields: minute, hour, day of month, month, day of week. Numbers, `*`, lists `a,b`, ranges `a-b`, steps `*/n`, `a-b/n`, `a/n`, month names `JAN`-`DEC` and day names `SUN`-`SAT` (either case); day of week `0`-`7`, both `0` and `7` are Sunday. No `L`, `W`, `#`, seconds field or `@daily`. A day of month that never occurs in the chosen months (`0 0 30 2 *`) is refused. |
| `path` | string | required | Starts with a single `/`, may carry a `?query`, no `#`, at most 512 characters. |
| `timezone` | string | `"UTC"` | `UTC` or an IANA zone such as `Europe/London`, checked against the zone database. |
| `method` | string | `"POST"` | `GET` or `POST`. |
| `timeout_seconds` | integer | `60` | 1 to 900. A run past it is cancelled. |

Preview timers are stored paused (decision, lane A).

## Resource classes

The sizes behind a class name are platform policy, not manifest data: they are not in the digest and may change by decision without a new schema.

| `class` | vCPU | Memory | Max instances |
|---|---|---|---|
| `small` | 1 | 512 MiB | 2 |
| `medium` | 1 | 2 GiB | 4 |
| `large` | 2 | 4 GiB | 8 |

`sessions = true` caps any class at 1 instance.

## Refusals

`load_manifest(text)` returns a `Manifest` or raises `ManifestError`, which lists **every** problem in file order, one per line:

```text
ssc.toml:LINE:COL: field: message
```

```text
ssc.toml:5:1: runtime.helth_path: unknown key; did you mean 'health_path'?
ssc.toml:10:1: schedules[1].cron: must have exactly five fields: minute hour day-of-month month weekday
ssc.toml:7:3: connections.names[2]: connection 'warehouse' is listed twice
```

- `LINE` and `COL` are 1-based and point at the key (or the array element) that is wrong; a missing key points at its table header, and a missing `schema` at line 1.
- `field` is the dotted path with `[i]` for array positions, in the order written in the file. Two pseudo-fields exist: `syntax` for a TOML syntax error (position from the TOML parser) and `(file)` for a file that is not UTF-8 or is larger than 256 KiB.
- The message is plain text, never a stack trace. A leading UTF-8 BOM and CRLF line ends are accepted.

`ManifestError.line`, `.column`, `.field` and `.message` are the first problem's. The API puts the fixed problem text in the response body and the line, column and field in evidence only; the CLI parses the manifest locally first, so the builder sees the lines above.

## Digest

`manifest_digest(m)` is `sha256:` followed by the lowercase hex SHA-256 of the RFC 8785 canonical JSON of `m.model_dump(mode="json", by_alias=True)`, taken over the **normalised model**, not the file text:

- every default applied, so an omitted key and the same key set to its default digest the same;
- `public_names`, `names` and `hosts` sorted, schedules sorted by `name`;
- `cron` normalised to single spaces with upper-case names (`0 9 * * mon-fri` becomes `0 9 * * MON-FRI`);
- keys are the TOML names (`schema`, `class`), and `start` is `null` when absent.

Comments, key order, table order, inline versus header tables, quoting style and integer notation therefore never change the digest; any change of value does. Normalisation is textual only: `1-5` and `MON-FRI` are different values and digest differently. The digest of a file with only `schema = "ssc/v1"` is `sha256:0ff2f05525bd8d69855565619833b59eb4945c2d58f949300bec78b9aac1fdf3`.

RFC 8785 is used for manifest, snapshot and bundle digests only, never for the audit chain (decision 012).

## Capability diff

`diff_capabilities(manifest, caps)` compares the manifest with what one environment grants (`EnvironmentCapabilities`: `postgres`, `connections`, `egress_hosts`) and returns a `CapabilityDiff` with `blocks` always `false`.

| Kind | Severity | One per | Approver |
|---|---|---|---|
| `postgres_missing` | high | manifest | none |
| `connection_missing` | high | ungranted connection | org admin |
| `egress_host_missing` | medium | ungranted host | org admin |
| `schedules_declared` | low | schedule | none |

Each change carries one fixed plain sentence about what happens at runtime. Changes sort by severity, then kind, then subject; at most 20 are listed, followed by "... and N more changes, not listed."; an empty diff renders as "No change". Approvers are refined by A3.

## Limits

| Limit | Value |
|---|---|
| File size | 256 KiB |
| `public_names`, values per `public_env` environment | 50 each |
| `connections.names` | 20 |
| `egress.hosts` | 50 |
| `schedules` | 20 |

## Versioning

- v1 freezes the key set, the types, the defaults, the refusal format and the digest rule.
- A change that would alter the digest of a manifest v1 accepts, or refuse a manifest v1 accepts, is a new schema: `ssc/v2` is added beside v1, `load_manifest` dispatches on `schema`, and v1 files keep loading and keep their digests.
- Accepting more within v1 (wildcard hosts, a new class name, a new cron form) is a widening change: allowed by a decision, because no accepted manifest changes digest.
- A newer file read by an older tool is refused by name (`"ssc/v2" is not a schema this version reads`), never partially read.
