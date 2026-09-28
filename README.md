# Small Software Cloud (SSC)

The place where AI-built internal apps run, and the rules they run under. Product plan and tickets live in `../Cloud_for_small_soft/` (start with `SSC_MVP_Build_Path.md`, then `SSC_MVP_V1_Tickets.md`).

## Layout

| Path | What | Ticket |
|---|---|---|
| `packages/ssc_contracts` | Wire and storage contracts. Pure data, pyright strict. | SSC-010, SSC-020 |
| `packages/ssc_shared` | Ids, clock, identity-note signing helpers. | SSC-020 |
| `packages/ssc_bundle` | What `ssc deploy` uploads. | SSC-014 |
| `packages/ssc_control` | Control plane API, database, job queue, reconcilers. `domain/` is pyright strict. | SSC-011 onward |
| `packages/ssc_edge` | Cell gateway (Envoy ext_authz, login, identity note). | SSC-018, SSC-019 |
| `packages/ssc_datagw` | Read-only data gateway and file broker. | SSC-050, SSC-046 |
| `packages/ssc_egress` | Egress proxy control. | SSC-053 |
| `packages/ssc_cli` | The `ssc` command. | SSC-022 |
| `packages/ssc_app` | Tiny helper apps may install to read the identity note. | SSC-020 |
| `conformance/` | Black-box tests any deployment must pass. | SSC-056 |
| `console/` | Admin console, TypeScript. Empty until SSC-057. | SSC-057 |
| `infra/` | Pulumi in Python. Empty until the cloud is chosen. | SSC-001, SSC-013 |
| `spikes/bakeoff` | SSC-001 cloud bake-off harness: three test apps, probes, runner, scorecard. | SSC-001 |
| `spikes/appdb` | SSC-005 per-app database creation and driver matrix. | SSC-005 |
| `gates/` | One planted violation per CI gate. `gates/run_gates.py` proves every gate fires. | SSC-007 |
| `tools/` | `lock_age_check.py` (7-day rule), `deptry_all.py`. | SSC-007 |
| `docs/decisions/` | Decision records. | SSC-006 |

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
uv run pytest            # needs Docker: one test starts postgres:18
uv run python gates/run_gates.py
```

## Rules the tooling enforces

- **No package younger than 7 days.** `[tool.uv] exclude-newer` in `pyproject.toml` stops the resolver from picking one; `tools/lock_age_check.py` re-checks the lock in CI. Exceptions go in `docs/lock-exceptions.toml` with a reason and an expiry.
- **Cell services never import the control plane or its database.** `ssc_edge`, `ssc_datagw`, `ssc_egress`, `ssc_app` may not import `ssc_control`, `sqlalchemy`, `alembic` or `psycopg`.
- **Layers.** `ssc_contracts` < `ssc_shared` < `ssc_bundle` < services.
- **GitHub Actions pinned by commit hash**, checked by zizmor.
- **Every gate has a planted violation** in `gates/fixtures/`; CI fails if any gate stays silent.
- **Secrets.** gitleaks runs on every pull request; the planted fixture is allowlisted by path in `.gitleaks.toml`.

## Never

- Modify the Delimitus repository (`../Ristretto-python-conversion`). It is read for patterns and tests only.
- Use `RISTRETTO_*_LIVE` flags, or create anything in GCP project `ristretto-506621`.
- Use the 161-app corpus for anything but internal testing.
