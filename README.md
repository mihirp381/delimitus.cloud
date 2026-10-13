# Small Software Cloud (SSC)

**A cloud for small internal apps, with the rules they run under.**

SSC gives teams a place to deploy apps built by people and coding agents, sign in with their
work account, and control who can use an app and what it can reach. This repository contains
the control plane, customer cell services, CLI, admin console, app helpers, and infrastructure
for the Delimitus product.

SSC is being developed for a private pilot. V1 supports Okta through WorkOS; the
[known limits](docs/README.md#known-limits) describe the scope of sign-in, access, and builds.

## What SSC does

- **Deploy and promote.** Deploy an app folder to preview through `ssc deploy` or connect a
  GitHub repository. Promote a release to production through a human-controlled approval path.
- **Control access.** Share an app with a person, a group, or the organisation. Explain access
  decisions and stop an app with the kill switch.
- **Keep state and connect data.** Give an app environment its own Postgres database and file
  storage. Grant read-only company data connections through the data gateway.
- **Control outbound traffic.** App runtime traffic uses a customer cell's network boundary;
  internet access goes through an organisation-wide host allowlist. Build network limits are
  documented separately in the [trust pack](docs/trust/build-limits.md).
- **Operate and inspect.** Use the console or CLI for logs, rollback, schedules, approvals, and
  the organisation's hash-chained audit log.
- **Work with coding agents.** The MCP interface exposes app checks and preview operations;
  production approvals and secret values remain outside the agent interface.

## Architecture

The **control plane** owns organisations, policy, releases, approvals, and background jobs.
Each customer has a **cell** with its own Google Cloud project and network. The cell agent
builds and runs apps, the gateway enforces access, and the data gateway and egress proxy mediate
data and internet access.

The implementation uses Python and FastAPI, PostgreSQL, React and Vite, and Pulumi. Google Cloud
provides Cloud Run, Cloud Build, Cloud SQL, and Cloud Storage. Infrastructure is maintained as a
separate Python project so provider dependencies stay out of the service workspace.

See the [architecture and package guide](docs/architecture.md) for the component diagram,
package responsibilities, and boundaries; [decision records](docs/decisions/README.md) explain
the choices behind them.

## Get started from source

Use **Python 3.14**, **uv 0.12** (CI pins 0.12.19), and **Node 24.15+ within the 24.x series** for
the console and browser tests. The console also accepts the Node ranges in its
[package manifest](console/package.json). Docker is needed for database and end-to-end tests.

```sh
git clone https://github.com/mihirp381/delimitus.cloud.git
cd delimitus.cloud
uv sync --all-packages --locked
uv run ssc --help
```

For a running local API, a development token, and the console, follow the
[local development walkthrough](CONTRIBUTING.md#local-control-plane). It uses a local Postgres
container and simulated build/runtime drivers to exercise the deployment lifecycle.

For an app already connected to an SSC organisation, start with `ssc init`, `ssc doctor`, and
`ssc deploy`; run `ssc <command> --help` for options. The
[`ssc.toml` contract](docs/contracts/manifest.md) describes runtime settings and requested
capabilities. The [remote MCP guide](docs/runbooks/remote-mcp.md) covers coding-agent connections.

## Repository layout

| Path | Purpose |
| --- | --- |
| [packages/](packages/) | Python workspace: contracts, shared code, control plane, cell services, CLI, app helper, and site hosts. |
| [console/](console/README.md) | React admin console, generated API client, unit tests, and browser tests. |
| [landing/](landing/README.md) | Public page, pilot request form assets, claims, and browser tests. |
| [helpers/node/](helpers/node/) | Node helpers for identity, reconnecting streams, files, and data queries. |
| [infra/](infra/README.md) | Separate Pulumi project for the platform and customer cells. |
| [conformance/](conformance/) | Shared contract tests, protocol vectors, build fixtures, and runtime probes. |
| [e2e/isolation/](e2e/isolation/README.md) | Browser and API tests of customer isolation. |
| [gates/](gates/) | Planted violations that prove the CI gates catch failures. |
| [tools/](tools/) | Development stack, dependency checks, API checks, and release tooling. |
| [docs/](docs/README.md) | Architecture, contracts, decisions, runbooks, pilot notes, and known limits. |
| [.github/](.github/) | CI, nightly checks, the release workflow for the CLI and the app helpers, and the preview deploy action. |

Python packages use `packages/<package>/src/<module>/` and keep tests beside each package.
The root [pyproject.toml](pyproject.toml) defines the workspace and quality gates;
[uv.lock](uv.lock) records its dependencies. Infrastructure and experiments have their own
project files and lockfiles. [spikes/](spikes/) holds research harnesses and proof runs.

## Development and operations

Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, dependency policy, and repository
rules. The main test suite runs from the repository root:

```sh
uv run pytest
```

Docker must be running; database tests start PostgreSQL 17 and 18 containers. Console, helper,
infrastructure, and browser checks are listed in the contributor guide and enforced by
[CI](.github/workflows/ci.yml).

For cloud deployment, use the [infrastructure guide](infra/README.md) and
[control plane runbook](docs/runbooks/ssc-064-control-plane.md). For support and the rest of the
operating procedures, start at the [documentation index](docs/README.md).
