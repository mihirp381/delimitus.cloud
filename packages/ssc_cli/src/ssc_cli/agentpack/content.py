"""The text ``ssc init`` writes. Command lines are filled in from the registered commands."""

from typing import Final

BEGIN: Final = "<!-- ssc:begin v1 -->"
END: Final = "<!-- ssc:end -->"

SKILL_FRONTMATTER: Final = """\
---
name: ssc
description: How this internal app runs on SSC (Small Software Cloud) and how to use its \
command line tool, ssc. Use when changing how the app starts, signs people in or stores \
data, when checking it with `ssc doctor`, or when sharing it or checking its status.
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
3 no token or token refused, 4 `ssc doctor` found a blocking problem, 5 network error.

A sharing change made with an agent's token waits for another admin: `ssc share` and
`ssc unshare` then change nothing, print the approval request ids (`pending` under `--json`) and
exit 0. Tell the person, and run the same command again once it is approved. The code
`APPROVAL_REQUIRED` means showing an app that reaches company data to more people needs another
admin's approval first; without `--json` the error says how to ask.

Run `ssc doctor` after every change that affects how the app installs or starts, and fix every
finding marked `block`.

### Runtime rules

- Listen on 0.0.0.0 and take the port from the `PORT` environment variable.
- One app per folder. It starts from the `start` script in package.json, a `web:` line in a
  `Procfile`, or `start` under `[runtime]` in `ssc.toml`.
- The app runs as a non-root user, and anything it writes to disk is kept in memory and lost on
  every restart or deploy. Write scratch files under /tmp and keep lasting data in Postgres.
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
- `audience` is the app's own exact origin, such as `https://quiet-river-7f3k.delimitusapps.com`.
  `keys` is the JWKS address `https://keys.delimitus.com/<cell>/jwks.json`. SSC will pass both to
  the app; the environment variable names are not fixed yet.
- Treat every refusal as "not a signed-in user": answer 401 and never echo the note.

### Data

- For a database, add `[state]` with `postgres = true` to `ssc.toml` and connect with the
  `DATABASE_URL` environment variable.
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
start = "uvicorn main:app --host 0.0.0.0 --port $PORT"

[state]
postgres = true
```
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
