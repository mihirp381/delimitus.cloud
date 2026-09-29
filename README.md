# Small Software Cloud (SSC)

The place where AI-built internal apps run, and the rules they run under. Product plan and tickets live in `../Cloud_for_small_soft/` (start with `SSC_MVP_Build_Path.md`, then `SSC_MVP_V1_Tickets.md`).

## Layout

| Path | What | Ticket |
|---|---|---|
| `packages/ssc_contracts` | Wire and storage contracts. Pure data, pyright strict. `manifest.py` reads `ssc.toml` (`ssc/v1`); `capabilities.py` diffs what a manifest asks for against what an environment grants; `app_env.py` names the environment variables the platform sets in every app container; `snapshot.py` is the access snapshot `ssc-snapshot/v1` (`docs/contracts/access-snapshot.md`). | SSC-010, SSC-020, SSC-044, SSC-017, SSC-021 |
| `packages/ssc_shared` | Clock and other small shared pieces. `canonical.py` is RFC 8785 JSON and the manifest digest; `blobstore.py` is the `BlobStore` protocol, `blobstore_fs.py` its filesystem implementation with signed URLs; `access.py` is the one access evaluator (`decide`), used by explain and the gateway. | SSC-007, SSC-044, SSC-021 |
| `packages/ssc_bundle` | What `ssc deploy` uploads. | SSC-014 |
| `packages/ssc_control` | Control plane API, database, job queue, reconcilers. `api/` is the FastAPI application (`/v1`, `/internal/v1`; conventions in `docs/api/README.md`); `db/` holds the schema, migrations, roles and the org bind (see `db/README.md`, `db/PLPGSQL.md`, `db/PII.md`); `audit/` is the audit log (decision 012). `api/`, `approvals/`, `audit/`, `db/`, `domain/` and `metrics/` are pyright strict. | SSC-010 onward |
| `packages/ssc_control/src/ssc_control/api/routes/v1/` | The `/v1` routes, one module per resource (`whoami`, `apps`, `grants`, `access`, `deployments`, `audit`, `approvals`); `common.py` holds the shared models and helpers; `__init__.py` mounts each router on its own line. | SSC-011 |
| `packages/ssc_control/src/ssc_control/ports.py` | Frozen cross-lane Protocols, each with a safe stub: `ProdGate` (stub refuses), `SnapshotPort` (stub never confirms), `TimersPort` and `MetricsPort` (stubs do nothing). Pyright strict. | W0 |
| `packages/ssc_control/src/ssc_control/api/authz.py` | Authorisation checks run inside a unit of work: `require_admin` (an active org admin with a user credential), `require_builder` (an admin, the app's owner or a builder of the environment; what may change that environment's sharing, decision 019). | SSC-012, SSC-045, SSC-021 |
| `packages/ssc_control/src/ssc_control/audit/` | The audit log (decision 012): `chain.py` appends, `views.py` limits what a row may say, `verify.py` names the first broken link (`python -m ssc_control.audit verify --org <id>`), `search.py` and `export.py` read it back for `/v1/audit`. Pyright strict. | SSC-012 |
| `packages/ssc_control/src/ssc_control/approvals/` | Approvals (decision 016): `service.py` opens and decides requests, `gate.py` is the fail-closed production gate and `ApprovalsProdGate` (the real `ProdGate`), `capabilities.py` says what a release asks to reach, `policy.py` writes `policy_decision` rows. The rules are pure, in `domain/approval_rules.py`. Pyright strict. | SSC-045 |
| `packages/ssc_control/src/ssc_control/snapshot/` | Access snapshots (decision 019): `compiler.py` compiles an org's `ssc-snapshot/v1` in one read and publishes it content-addressed with a `latest.json` pointer, `service.py` is `mark_dirty` (the per-org lock handshake), heartbeat acknowledgements and `Snapshots`, the real `SnapshotPort`, `jobs.py` the `snapshot:compile` task. Floors are in `domain/grant_rules.py`; directory sync in `directory.py` (`/internal/v1/directory`). Pyright strict. | SSC-021 |
| `packages/ssc_control/src/ssc_control/runtime/` | The runtime seam (decision 014): `driver.py` is the `RuntimeDriver` protocol and the pure `desired_for`, `fake.py` the in-memory driver, `reconciler.py` one change per pass, `jobs.py` the reconcile tick and per-environment job, `specs.py` where a release's manifest comes from. Pyright strict. | SSC-017 |
| `packages/ssc_control/src/ssc_control/worker.py` | The worker (`python -m ssc_control.worker`): one Procrastinate app, each lane's tasks added from a blueprint factory, the stalled-job sweep and the composition root; `worker_ports.py` hands tasks their `Ports`; `deferral.py` defers a job in the caller's transaction (the API uses it). Pyright strict. | SSC-017 |
| `packages/ssc_control/src/ssc_control/metrics/` | Product metrics (SSC-028): `events.py` is `Metrics`, the real `MetricsPort`, writing `metrics_event` in the caller's transaction, and `metrics_port(key)`, which the API (and the worker, when B4 needs it) builds its recorder with; `pseudonym.py` derives the per-org HMAC pseudonym from `SSC_METRICS_KEY`; `source_tool.py` names the builder tool (`X-SSC-Source-Tool`, or the agent `client_id`); `report.py` is the report and the pilot kill criteria (`python -m ssc_control.metrics.report --org <id>`). Small-sample statistics are in `domain/stats.py`. Pyright strict. | SSC-028, SSC-060 |
| `packages/ssc_edge` | Cell gateway (Envoy ext_authz, login). `identity_note.py` mints the identity note; pyright strict. | SSC-020, SSC-018, SSC-019 |
| `packages/ssc_datagw` | Read-only data gateway and file broker. | SSC-050, SSC-046 |
| `packages/ssc_egress` | Egress proxy control. | SSC-053 |
| `packages/ssc_cli` | The `ssc` command. | SSC-022 |
| `packages/ssc_app` | The helper apps install to read the identity note (`ssc_app.identity`); pyright strict. | SSC-020 |
| `conformance/` | Black-box tests any deployment must pass. `identity_note/` holds the shared identity-note vectors and their generator; `ssc_conformance/contracts/` holds contract suites every implementation of a port runs (`BlobStoreContract`, `RuntimeDriverContract`); `ssc_conformance/runtime_probes.py` and `runtime/probe_app/` are the fourteen runtime probes (four local, ten waiting for a staging cell). | SSC-020, SSC-044, SSC-056, SSC-017 |
| `helpers/node/ssc-identity` | Node verifier for the identity note, zero dependencies, `npm test` runs the shared vectors. | SSC-020 |
| `console/` | Admin console (decision 018): React 19, Vite 8, TanStack Router and Query, a client generated from `docs/api/openapi.json`, `--ssc-*` tokens, Vitest and a Playwright smoke test against the API in Docker. See `console/README.md`. | SSC-057 |
| `infra/` | Pulumi in Python. Empty until the cloud is chosen. | SSC-001, SSC-013 |
| `spikes/bakeoff` | SSC-001 cloud bake-off harness: three test apps, probes, runner, scorecard. | SSC-001 |
| `spikes/appdb` | SSC-005 per-app database creation and driver matrix. | SSC-005 |
| `gates/` | One planted violation per CI gate. `gates/run_gates.py` proves every gate fires. | SSC-007 |
| `tools/` | `lock_age_check.py` (7-day rule), `deptry_all.py`, `openapi_check.py` (committed spec matches the code), `openapi_breaking.py` (refuses breaking API changes), `npm_lock_age_check.py` (7-day rule for `console/package-lock.json`). | SSC-007, SSC-011, SSC-057 |
| `tools/dev_stack.py` | Local control plane for development and CLI tests: `up` (roles, migrations, one org, a signing key), `token`, `serve` (`--port 0` prints the chosen port). State in `.ssc-dev/`. | SSC-022 |
| `docs/decisions/` | Decision records. | SSC-006 |
| `docs/contracts/` | Frozen cross-squad contracts (identity note, `ssc.toml` manifest). | SSC-020, SSC-044 |
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
uv run python tools/npm_lock_age_check.py      # console/package-lock.json
(cd console && npm ci && npm run typecheck && npm test && npm run build)   # Node 22.22.2+, 24.15+ or 26+
```

## Rules the tooling enforces

- **No package younger than 7 days.** `[tool.uv] exclude-newer` in `pyproject.toml` stops the resolver from picking one; `tools/lock_age_check.py` re-checks the lock in CI. For the console, `console/.npmrc` `before=` and `tools/npm_lock_age_check.py` do the same. Exceptions go in `docs/lock-exceptions.toml` with a reason and an expiry.
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
