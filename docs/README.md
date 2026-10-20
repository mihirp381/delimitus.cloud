# SSC documentation

Start with the [product and repository overview](../README.md). For development setup and
checks, use [CONTRIBUTING.md](../CONTRIBUTING.md); for code navigation, use the
[architecture guide](architecture.md).

## Architecture and contracts

| Guide | Purpose |
| --- | --- |
| [Architecture and packages](architecture.md) | Control plane, customer cells, package responsibilities, and enforced boundaries. |
| [Decision records](decisions/README.md) | Architectural choices, amendments, and their evidence. |
| [Control plane API](api/README.md) | Public, internal, and MCP surfaces; request and error conventions. |
| [OpenAPI specification](api/openapi.json) | Committed machine-readable API contract. |
| [`ssc.toml` manifest](contracts/manifest.md) | App runtime settings, requested capabilities, validation, and versioning. |
| [Identity note](contracts/identity-note.md) | Identity passed to an app and the shared verification contract. |
| [Access snapshot](contracts/access-snapshot.md) | Policy distributed to cell services. |
| [Data gateway](contracts/data-gateway.md) | Read-only queries, admission limits, and the file broker. |
| [Control database](../packages/ssc_control/src/ssc_control/db/README.md) | Roles, migrations, org binding, and row-level security. |
| [Dependency exceptions](lock-exceptions.toml) | Approved exceptions to dependency age policy. |

## App development and integrations

| Guide | Purpose |
| --- | --- |
| [Python app helper](../packages/ssc_app/README.md) | Identity verification, reconnecting streams, files, and data queries. |
| [Node identity helper](../helpers/node/ssc-identity/README.md) | Verify the identity note. |
| [Node reconnect helper](../helpers/node/ssc-reconnect/README.md) | Maintain streams across the gateway's request limit. |
| [Node file helper](../helpers/node/ssc-files/README.md) | Store and retrieve an environment's files through the broker. |
| [Node data helper](../helpers/node/ssc-data/README.md) | Query granted company data connections. |
| [Remote MCP](runbooks/remote-mcp.md) | Connect a coding agent through OAuth and troubleshoot sign-in. |
| [GitHub preview deploy action](../.github/actions/ssc-deploy/README.md) | Deploy from CI using a preview-scoped credential. |
| [First data connection](runbooks/ssc-052-first-connection.md) | Create a connection, grant it, and check the first read. |

## Deployment and operations

| Guide | Purpose |
| --- | --- |
| [Infrastructure](../infra/README.md) | Platform and cell stacks, networking, lazy resources, and live checks. |
| [Control plane deployment](runbooks/ssc-064-control-plane.md) | Production setup, secrets, migrations, and release checks. |
| [Login rig](runbooks/ssc-019-login.md) | Local SSO and directory checks with WorkOS staging tenants. |
| [Approval email](runbooks/ssc-049-approval-email.md) | Configure delivery, diagnose failures, and rotate credentials. |
| [Database restore](runbooks/ssc-043-restore-database.md) | Restore an app database from a point-in-time clone. |
| [Admin recovery](runbooks/ssc-097-restore-admin.md) | Restore an organisation's admin and record the change. |
| [Support and on call](runbooks/ssc-062-support-and-on-call.md) | Alerts, diagnosis, escalation, and status notes. |
| [Cost reconciliation](runbooks/ssc-096-cost-reconciliation.md) | Compare usage records with cloud billing. |
| [Kill switch drill](kill-switch-drill.md) | Measure app shutdown and revocation. |

## Known limits

These documents describe V1 scope and measured behaviour. Read them before relying on a
feature or making a product claim.

- [Sign-in and directory](trust/identity-limits.md): supported identity provider, account
  deactivation, login matching, and directory changes.
- [App access, logs, audit, and internet access](trust/app-access-limits.md): revocation,
  request limits, log delay, audit anchors, and the organisation-wide egress allowlist.
- [Builds](trust/build-limits.md): build network access and secret scanning limits.

## Product surfaces, testing, and pilot

- [Admin console](../console/README.md), its [host](../packages/ssc_console_host/README.md) and [public page](../landing/README.md).
- [Landing-page claims](../landing/CLAIMS.md).
- [Build fixtures](../conformance/build_fixtures/README.md) and
  [customer isolation tests](../e2e/isolation/README.md).
- [Pilot success sheet](pilot/success-sheet.md) and [how to get help](support/how-to-get-help.md).
- Research harnesses and proof-run records are kept on an internal archive branch, not on main.

Keep this index current when adding a guide. Published documentation should resolve from a
fresh clone; planning files kept outside the repository are outside the versioned docs.
