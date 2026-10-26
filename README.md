# Small Software Cloud (SSC)

**A cloud for small internal apps, with the rules they run under.**

SSC gives the tools your team builds, by hand or with a coding agent, a place to run. Every app
sits behind your company's sign-in, and you decide who may open it, what company data it may
read, and which internet hosts it may call.

SSC is in a private pilot. V1 runs Python and Node web apps and signs people in with Okta.

## What you get

- **Deploy** an app folder to preview with one command, then promote it to production.
- **Sign-in and sharing** through your company's single sign-on: share with a person, a group,
  or everyone.
- **A database and file storage** for each app, and read-only access to company data.
- **Controlled internet access:** an app reaches only the hosts your organisation allows.
- **Approvals, a kill switch, and an audit log** for every change.
- **Coding agent support** through MCP: an agent can check and deploy to preview. Coding agents
  cannot promote: a builder of the app promotes a release to production, and a release that
  asks for new internet hosts or data connections waits for an admin to approve them.

## Quick start

Your SSC admin gives you three things: your org id (`org_...`), an assignment to SSC in Okta,
and the console's address (`https://console.delimitus.com`). The CLI needs Python 3.14. Then,
from your app's folder:

```sh
uv tool install 'ssc-cli[mcp]'      # the ssc command, and the local MCP server for coding agents
ssc login --org <org id>            # signs you in through Okta
ssc init                            # AGENTS.md, a Claude Code skill, MCP settings and ssc.toml
ssc apps create expenses            # the app, with preview and production environments
ssc deploy . --app expenses --wait  # builds the folder and runs it in preview
ssc promote expenses --wait         # builds what preview runs for production and deploys it
ssc share expenses someone@company.com  # lets a person use the production app
```

Sharing by email address needs an org admin; anyone else shares by the person's `usr_` id
(their `ssc whoami` shows it as `subject`).

When something fails, `ssc status expenses` shows each environment's state and
`ssc logs expenses --source build` the build's output. An error names its code and, when there
is a next step, ends with a `Fix:` line. The exit code says what kind of failure it was: 1
refused or failed, 2 bad usage, 3 not signed in, 4 stopped on this machine before anything was
sent, 5 network. [How to get help](docs/support/how-to-get-help.md) says what to send support.

Run `ssc requirements` for the rules an app must follow, and `ssc <command> --help` for options.

## Documentation

- [`ssc.toml` reference](docs/contracts/manifest.md): what an app can ask the platform for.
- [Connecting a coding agent](docs/runbooks/remote-mcp.md).
- [Known limits](docs/README.md#known-limits): what V1 does and does not do.
- [Getting help](docs/support/how-to-get-help.md).
- [Architecture](docs/architecture.md) and the full [documentation index](docs/README.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, and repository rules.
