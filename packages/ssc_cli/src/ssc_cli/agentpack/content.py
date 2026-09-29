"""The text ``ssc init`` writes. Command lines are filled in from the registered commands, and
the runtime environment names from ``ssc_contracts.app_env`` (decision 014)."""

from typing import Final

from ssc_contracts import app_env

ENV_NAMES: Final = {
    "port": app_env.PORT,
    "home": app_env.HOME,
    "home_value": app_env.HOME_VALUE,
    "database_url": app_env.DATABASE_URL,
    "app_origin": app_env.APP_ORIGIN,
    "keys_url": app_env.IDENTITY_KEYS_URL,
}

BEGIN: Final = "<!-- ssc:begin v1 -->"
END: Final = "<!-- ssc:end -->"

SKILL_FRONTMATTER: Final = """\
---
name: ssc
description: How this internal app runs on SSC (Small Software Cloud) and how to use its \
command line tool, ssc. Use when changing how the app starts, signs people in or stores \
data, when checking it with `ssc doctor`, when deploying it to preview or rolling it back, or \
when sharing it or checking its status.
---
"""

GUIDE: Final = """\
## SSC: how this app runs

This app runs on SSC (Small Software Cloud), the company's platform for internal apps. SSC signs
people in with company single sign-on before a request reaches the app, and builds and runs the
app from its source in a container. Follow these rules when you change it.

### The `ssc` command line

{commands}

Every command takes `--json` and then prints one JSON object on stdout; a failure prints
`{{"error": {{...}}}}` with a stable `code`. Exit codes: 0 ok, 1 refused or failed, 2 bad usage,
3 no token or token refused, 4 blocked on this machine before anything was sent (`ssc doctor`
found a blocking problem, or `ssc deploy` found a secret, an invalid `ssc.toml` or a folder it
cannot upload), 5 network error.

`ssc deploy --app <slug>` deploys the folder to the app's preview environment, never to
production. It checks the folder first and uploads nothing if it holds a secret. It waits for the
build; add `--wait` to also wait until the new release is live. A failed build or deployment exits
1 with the reason as `code` (for example `HEALTH_CHECK_FAILED`) and the next step in the error.
`ssc releases` lists the numbered releases and `ssc rollback <app> R<number>` deploys an earlier
one again, keeping today's sharing and secrets.

A sharing change made with an agent's token waits for another admin: `ssc share` and
`ssc unshare` then change nothing, print the approval request ids (`pending` under `--json`) and
exit 0. Tell the person, and run the same command again once it is approved. The code
`APPROVAL_REQUIRED` means showing an app that reaches company data to more people needs another
admin's approval first; without `--json` the error says how to ask.

`ssc share` names a person by `usr_` id or email address (looking up an email needs an org admin's
token) and a group by `grp_` id or name. When more than one fits, it exits 2 and lists their ids.

### Tools for coding agents: `ssc mcp`

`ssc mcp` serves SSC's agent tools (MCP) over stdio: `list_apps`, `get_app`, `get_status`,
`list_releases`, `rollback`, `deploy` (a folder, to preview only), `request_share` and
`request_connection`. Asking only opens an approval request; no tool approves or promotes. It
needs the extra (`uv tool install 'ssc-cli[mcp]'`) and a token issued to the agent, not a
person's, and refuses to start otherwise. Agent tokens are not self-serve yet. With the agent's
token in `SSC_AGENT_TOKEN`:

- Claude Code: `claude mcp add --transport stdio --env SSC_TOKEN="$SSC_AGENT_TOKEN" ssc -- ssc mcp`
- Codex: `codex mcp add ssc --env SSC_TOKEN="$SSC_AGENT_TOKEN" -- ssc mcp`, then set
  `tool_timeout_sec = 1500` under `[mcp_servers.ssc]` in `~/.codex/config.toml`; the default of
  60 seconds is shorter than a build.
- Cursor, in `.cursor/mcp.json`:

```json
{{
  "mcpServers": {{
    "ssc": {{
      "command": "ssc",
      "args": ["mcp"],
      "env": {{"SSC_TOKEN": "${{env:SSC_AGENT_TOKEN}}"}}
    }}
  }}
}}
```

Run `ssc doctor` after every change that affects how the app installs or starts, and fix every
finding marked `block`.

### Runtime rules

- Listen on 0.0.0.0 and take the port from the `{port}` environment variable.
- One app per folder. It starts from the `start` script in package.json, a `web:` line in a
  `Procfile`, or `start` under `[runtime]` in `ssc.toml`.
- The app runs as a non-root user, and anything it writes to disk is kept in memory and lost on
  every restart or deploy. `{home}` is `{home_value}`. Write scratch files under /tmp and keep
  lasting data in Postgres.
- Keep the lock file in step with the dependency list; the build installs with it frozen.
- Never put secrets in code, in `ssc.toml` or anywhere in the repository.
- Values the browser needs at build time (`VITE_*`, `NEXT_PUBLIC_*`) go under
  `[build.public_env.preview]` and `[build.public_env.prod]` in `ssc.toml`; any other name must
  also be listed in `public_names` under `[build]`. Anyone who can open the app can read them.

### Who is signed in: the identity note

- Every request carries a signed identity note in the `X-SSC-Identity` header. Do not build a
  login screen or add a sign-in vendor.
- Python: `from ssc_app.identity import IdentityVerifier`, then
  `IdentityVerifier(audience=..., keys=...).from_headers(request.headers)`.
- Node: `import {{ IdentityVerifier }} from '@delimitus/ssc-identity'`, then
  `await new IdentityVerifier({{ audience, keys }}).fromHeaders(req.headers)`.
- Key users on `note.sub`, never on email or name, which can change.
- Take `audience` from the `{app_origin}` environment variable, the app's own exact origin
  (such as `https://expenses.qtbvkmrdhpsc.delimitusapps.com`, and
  `https://expenses--preview.qtbvkmrdhpsc.delimitusapps.com` in preview), and `keys` from
  `{keys_url}`, the JWKS address (`https://keys.delimitus.com/<cell>/jwks.json`). Never
  hard-code either. While one is unset, treat every request as not signed in.
- Treat every refusal as "not a signed-in user": answer 401 and never echo the note.

### Data

- For a database, add `[state]` with `postgres = true` to `ssc.toml` and connect with the
  `{database_url}` environment variable.
- SSC offers no key-value store such as Redis. Keep that data in a Postgres table; for a cache,
  use an `UNLOGGED` table with an `expires_at` column.
- Company data connections are not live yet. Do not write code against them until SSC documents
  the API.

### ssc.toml

The file starts with `schema = "ssc/v1"`, and the format is strict: unknown keys and values of
the wrong type are refused. `ssc doctor` prints each problem as
`ssc.toml:LINE:COL: field: message`. The format is a draft until SSC freezes it.

```toml
schema = "ssc/v1"

[runtime]
start = "uvicorn main:app --host 0.0.0.0 --port ${port}"

[state]
postgres = true
```

SSC sets `{port}`, `{home}`, `{database_url}` (only with `postgres = true`), `{app_origin}` and
`{keys_url}` itself; the last three are not set in every environment yet. `ssc.toml` cannot set
them, nor any other name that starts with `SSC_`.
"""

CLAUDE_IMPORT: Final = "@AGENTS.md\n"

STARTER_MANIFEST: Final = """\
# SSC app manifest, format ssc/v1. `ssc doctor` checks it.
schema = "ssc/v1"

# How to start the app, when package.json or a Procfile does not say:
# [runtime]
# start = "streamlit run app.py --server.port $PORT --server.address 0.0.0.0"

# A Postgres database for the app, reached through DATABASE_URL:
# [state]
# postgres = true
"""

STARTER_IGNORE: Final = """\
# Files left out of the upload. Same syntax as .gitignore.
.git/
node_modules/
.venv/
venv/
__pycache__/
.ssc-dev/
.env
.env.*
*.pem
"""
