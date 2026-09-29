# Small Software Cloud (SSC)

The place where AI-built internal apps run, and the rules they run under. Product plan and tickets live in `../Cloud_for_small_soft/` (start with `SSC_MVP_Build_Path.md`, then `SSC_MVP_V1_Tickets.md`).

## Layout

| Path | What | Ticket |
|---|---|---|
| `packages/ssc_contracts` | Wire and storage contracts. Pure data, pyright strict. | SSC-010, SSC-020 |
| `packages/ssc_shared` | Clock and other small shared pieces. | SSC-007 |
| `packages/ssc_bundle` | What `ssc deploy` uploads. | SSC-014 |
| `packages/ssc_control` | Control plane API, database, job queue, reconcilers. `api/` is the FastAPI application (`/v1`, `/internal/v1`; conventions in `docs/api/README.md`); `db/` holds the schema, migrations, roles and the org bind (see `db/README.md`, `db/PLPGSQL.md`, `db/PII.md`); `audit/` is the audit log (decision 012). `api/`, `audit/`, `db/` and `domain/` are pyright strict. | SSC-010 onward |
| `packages/ssc_control/src/ssc_control/api/routes/v1/` | The `/v1` routes, one module per resource (`whoami`, `apps`, `grants`, `deployments`, `audit`); `common.py` holds the shared models and helpers; `__init__.py` mounts each router on its own line. | SSC-011 |
| `packages/ssc_control/src/ssc_control/ports.py` | Frozen cross-lane Protocols, each with a safe stub: `ProdGate` (stub refuses), `SnapshotPort` (stub never confirms), `TimersPort` and `MetricsPort` (stubs do nothing). Pyright strict. | W0 |
| `packages/ssc_control/src/ssc_control/api/authz.py` | Authorisation checks run inside a unit of work: `require_admin` (an active org admin with a user credential). SSC-021 adds per-app roles. | SSC-012, SSC-021 |
| `packages/ssc_control/src/ssc_control/audit/` | The audit log (decision 012): `chain.py` appends, `views.py` limits what a row may say, `verify.py` names the first broken link (`python -m ssc_control.audit verify --org <id>`), `search.py` and `export.py` read it back for `/v1/audit`. Pyright strict. | SSC-012 |
| `packages/ssc_edge` | Cell gateway (Envoy ext_authz, login). `identity_note.py` mints the identity note; pyright strict. | SSC-020, SSC-018, SSC-019 |
| `packages/ssc_datagw` | Read-only data gateway and file broker. | SSC-050, SSC-046 |
| `packages/ssc_egress` | Egress proxy control. | SSC-053 |
| `packages/ssc_cli` | The `ssc` command. | SSC-022 |
| `packages/ssc_app` | The helper apps install to read the identity note (`ssc_app.identity`); pyright strict. | SSC-020 |
| `conformance/` | Black-box tests any deployment must pass. `identity_note/` holds the shared identity-note vectors and their generator. | SSC-020, SSC-056 |
| `helpers/node/ssc-identity` | Node verifier for the identity note, zero dependencies, `npm test` runs the shared vectors. | SSC-020 |
| `console/` | Admin console, TypeScript. Empty until SSC-057. | SSC-057 |
| `infra/` | Pulumi in Python. Empty until the cloud is chosen. | SSC-001, SSC-013 |
| `spikes/bakeoff` | SSC-001 cloud bake-off harness: three test apps, probes, runner, scorecard. | SSC-001 |
| `spikes/appdb` | SSC-005 per-app database creation and driver matrix. | SSC-005 |
| `gates/` | One planted violation per CI gate. `gates/run_gates.py` proves every gate fires. | SSC-007 |
| `tools/` | `lock_age_check.py` (7-day rule), `deptry_all.py`, `openapi_check.py` (committed spec matches the code), `openapi_breaking.py` (refuses breaking API changes). | SSC-007, SSC-011 |
| `tools/dev_stack.py` | Local control plane for development and CLI tests: `up` (roles, migrations, one org, a signing key), `token`, `serve` (`--port 0` prints the chosen port). State in `.ssc-dev/`. | SSC-022 |
| `docs/decisions/` | Decision records. | SSC-006 |
| `docs/contracts/` | Frozen cross-squad contracts (identity note). | SSC-020 |
| `docs/api/` | API conventions and the committed `openapi.json`. | SSC-011 |

## Toolchain

uv 0.12, Python 3.14 only. Pins: FastAPI 0.141, Pydantic 2.13, SQLAlchemy 2.0.54 (not 2.1), psycopg 3.3, Alembic 1.20, Procrastinate 3.10 (core, no SQLAlchemy extra), PyJWT 2.15, Typer 0.27, httpx2 2.13. Our code imports `httpx2`, never `httpx`; import-linter refuses `httpx`.

```
uv sync --all-packages
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run python tools/deptry_all.py
uv run zizmor --no-online-audits .github/workflows
uv run python tools/lock_age_check.py
uv run python tools/openapi_check.py            # docs/api/openapi.json matches the code (--write to refresh)
uv run python tools/openapi_breaking.py OLD NEW  # CI runs it against the merge base
uv run pytest            # needs Docker: control-db, API and job-queue tests start postgres:18
uv run python gates/run_gates.py
(cd helpers/node/ssc-identity && npm test)   # Node 22+
```

## Rules the tooling enforces

- **No package younger than 7 days.** `[tool.uv] exclude-newer` in `pyproject.toml` stops the resolver from picking one; `tools/lock_age_check.py` re-checks the lock in CI. Exceptions go in `docs/lock-exceptions.toml` with a reason and an expiry.
- **Cell services never import the control plane or its database.** `ssc_edge`, `ssc_datagw`, `ssc_egress`, `ssc_app` may not import `ssc_control`, `sqlalchemy`, `alembic` or `psycopg`.
- **Layers.** `ssc_contracts` < `ssc_shared` < `ssc_bundle` < services.
- **GitHub Actions pinned by commit hash**, checked by zizmor.
- **Every gate has a planted violation** in `gates/fixtures/`; CI fails if any gate stays silent.
- **Secrets.** gitleaks runs on every pull request; the planted fixture is allowlisted by path in `.gitleaks.toml`.
- **Identity note.** One header, `X-SSC-Identity`, ES256 `ssc-id+jwt`, twelve claims (`docs/contracts/identity-note.md`). Apps key on `sub`, never on `email`, and verify once per request. Both helpers must pass `conformance/identity_note/vectors.json`.
- **API.** Every refusal is an RFC 9457 problem from the catalogue in `ssc_contracts.errors` (fixed text, evidence in the log under the request id). Every `POST` needs `Idempotency-Key`, claimed in the request's transaction; sharing-rule edits need `If-Match`; deployments are `202` operations. `docs/api/openapi.json` is committed and CI refuses a breaking change. See `docs/api/README.md`.
- **Control database.** Every table is org-scoped with forced row-level security; queries without a bound org fail with `SC001`. Bind once per unit of work with `ssc_control.db.bound_org`. The app role owns nothing and has no privilege on the migration ledger. Catalog tests pin the table list, the PL/pgSQL list, the privilege matrix and the personal-data columns to `ssc_control.db.catalog`.

## Never

- Modify the Delimitus repository (`../Ristretto-python-conversion`). It is read for patterns and tests only.
- Use `RISTRETTO_*_LIVE` flags, or create anything in GCP project `ristretto-506621`.
- Use the 161-app corpus for anything but internal testing.
