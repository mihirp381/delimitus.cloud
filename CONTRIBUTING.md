# Contributing to SSC

The root Python workspace contains the product packages and conformance suite. The console,
Node helpers, and infrastructure have their own tooling. Start with the
[repository overview](README.md) and [architecture guide](docs/architecture.md).

## Toolchain and setup

| Tool | Requirement |
| --- | --- |
| Python | 3.14 only, as declared in `.python-version` and the package manifests. |
| uv | 0.12; CI uses 0.12.19. |
| Node.js | CI uses 24.x. The console requires `^22.22.2`, `^24.15.0`, or `>=26.0.0`; helpers require Node 22+. |
| Docker | Running for database and browser tests; the root suite uses PostgreSQL 17 and 18. |

Run from the repository root:

```sh
uv sync --all-packages --locked
uv run ssc --help
```

Use the committed lockfiles. Dependency versions and tool settings live in
[pyproject.toml](pyproject.toml), the package manifests, and
[console/package.json](console/package.json).

## Local control plane

Run these commands from the repository root. Start a dedicated local Postgres container:

```sh
docker run --detach --name ssc-dev-postgres \
  --publish 127.0.0.1:55432:5432 \
  --env POSTGRES_PASSWORD=ssc-dev \
  postgres:18
docker exec ssc-dev-postgres pg_isready --username postgres
```

Wait until `pg_isready` reports that the database accepts connections, repeating the second
command if needed. Then initialise the database and start the API and worker:

```sh
uv run python tools/dev_stack.py up \
  --dsn postgresql://postgres:ssc-dev@127.0.0.1:55432/postgres > /dev/null
SSC_BUILD_DRIVER=fake SSC_RUNTIME_DRIVER=fake \
  uv run python tools/dev_stack.py serve --port 8000 --worker
```

`up` creates roles, runs migrations, and creates a development organisation and signing keys.
It prints credentials, so the example discards its output. State is saved under the ignored
`.ssc-dev/` directory; `--dir PATH` selects another state directory and goes before the
subcommand. The local builder and runtime record simulated deployments. Use a staging cell to
check actual container execution.

In a second terminal at the repository root:

```sh
export SSC_API_URL=http://127.0.0.1:8000
export SSC_TOKEN="$(uv run python tools/dev_stack.py token)"
uv run ssc whoami
uv run ssc apps
```

Stop the server with Ctrl-C and the database with `docker stop ssc-dev-postgres`. To resume,
run `docker start ssc-dev-postgres` and restart the server; the container and `.ssc-dev/` keep
their state. The [login rig runbook](docs/runbooks/ssc-019-login.md) covers local checks with
real WorkOS staging tenants.

## Local console and landing page

With the API running, start the console in another terminal:

```sh
cd console
npm ci
SSC_API_URL=http://127.0.0.1:8000 npm run dev
```

Vite serves the console at `http://localhost:5173` and proxies `/v1` to the local API. Retrieve
a development token with `uv run python tools/dev_stack.py token` from the repository root and
paste it into the console's development sign-in form. See the
[console guide](console/README.md) for production sign-in and browser tests.

The public page has its own local server. From the repository root:

```sh
SSC_LANDING_ENV=dev SSC_LANDING_PAGE=landing/index.html \
  SSC_LANDING_ORIGIN=http://localhost:8080 SSC_LANDING_TRUSTED_HOPS=0 \
  uv run python -m ssc_landing
```

Development pilot requests stay in memory. See the [landing guide](landing/README.md) for
hosting and content rules.

## Checks

Run checks relevant to your change. The complete set and its pinned tool versions are in
[.github/workflows/ci.yml](.github/workflows/ci.yml).

### Python workspace

From the repository root:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run lint-imports
uv run python tools/deptry_all.py
uv run zizmor --no-online-audits .github/workflows .github/actions
uv run python tools/lock_age_check.py
uv run python tools/openapi_check.py
uv run pytest
uv run python gates/run_gates.py
```

The database tests use Docker. The planted violations in `gates/fixtures/` are intentional;
`gates/run_gates.py` fails if a quality gate stops detecting its violation.

`packages/ssc_app` is installed into customers' apps: ruff and pyright check it as Python 3.11,
and CI job `python-helper` runs its tests from the built wheel on Python 3.11 to 3.14.
`tools/release_versions.py` checks that every package the release workflow publishes, to PyPI
and to npm, carries one version.

### Node helpers and console

From the repository root:

```sh
npm --prefix helpers/node/ssc-identity test
npm --prefix helpers/node/ssc-reconnect test
npm --prefix helpers/node/ssc-files test
npm --prefix helpers/node/ssc-data test
npm --prefix console ci
npm --prefix console run gen:api
git diff --exit-code -- console/src/api/schema.d.ts
npm --prefix console run typecheck
npm --prefix console test
npm --prefix console run build
git diff --exit-code -- console/src/routeTree.gen.ts
uv run python tools/npm_lock_age_check.py console/package-lock.json
uv run python tools/npm_lock_age_check.py e2e/isolation/package-lock.json
uv run python tools/npm_lock_age_check.py landing/e2e/package-lock.json
node --test landing/calculator.test.mjs
```

CI checks that the API client and route tree are committed and match their sources. If you
changed the API or routes, regenerate and commit the affected files before checking for drift.
Regenerate console tokens with `npm --prefix console run gen:tokens` after changing their
source. Follow the [console](console/README.md#end-to-end-test),
[isolation](e2e/isolation/README.md), and [landing](landing/README.md) guides for Playwright
setup and browser checks.

### Infrastructure

Infrastructure uses its own uv project and mocked provider tests:

```sh
cd infra
uv sync --locked
uv run pytest
```

Then, from the repository root:

```sh
uv run pyright -p infra
uv run python tools/lock_age_check.py infra/uv.lock
```

The [infrastructure guide](infra/README.md) covers cloud configuration and live checks.

## Repository and dependency rules

- Keep product Python code in `packages/<package>/src/<module>/`, with tests in that package's
  `tests/`. Shared black-box checks and vectors belong in `conformance/`; operational
  procedures and contracts belong in `docs/`.
- Preserve the dependency order: contracts, shared code, bundles, then services. Cell services
  and the app helper cannot import the control plane or its database dependencies. The public
  page and console host serve independently of the platform services. Import-linter enforces
  the exact boundaries in `pyproject.toml`.
- Product code imports `httpx2`. The `httpx` development dependency is for FastAPI's test
  client; product imports of it are refused.
- Admit no dependency younger than seven days. uv's `exclude-newer`, npm's `before` setting,
  and the lock-age checks enforce the cutoffs. Exceptions need a reason and expiry in
  [docs/lock-exceptions.toml](docs/lock-exceptions.toml).
- Pin external GitHub Actions by full commit hash. zizmor checks workflows and composite
  actions; gitleaks checks secrets on pull requests. The planted secret fixture is allowlisted
  only by its path in `.gitleaks.toml`.
- Keep caches, dependencies, build output, local environment files, and operator scratch keys
  out of version control. Commit the generated OpenAPI spec, console API client, route tree,
  and design tokens, which CI checks against their sources.
- Keep new documentation links relative to the repository and list new guides in
  [docs/README.md](docs/README.md). Local planning material under the ignored
  `Cloud_for_small_soft/` directory is not available to someone cloning the repository.

## Contracts and changes

App helpers verify the `X-SSC-Identity` note once per request and key people by `sub`. Both
Python and Node implementations must pass the shared identity and reconnect vectors. Wire
contracts live in [docs/contracts/](docs/contracts/), and
[docs/api/README.md](docs/api/README.md) defines API conventions: RFC 9457 problems,
`Idempotency-Key` for `POST`, `If-Match` for sharing-rule edits, and `202` operations for
deployments.

Control database tables use organisation scoping and forced row-level security. Bind the org
once per unit of work with `ssc_control.db.bound_org`; catalog tests pin schema, privileges,
PL/pgSQL functions, and personal-data columns. See the
[database guide](packages/ssc_control/src/ssc_control/db/README.md),
[PL/pgSQL rules](packages/ssc_control/src/ssc_control/db/PLPGSQL.md), and
[personal-data catalog](packages/ssc_control/src/ssc_control/db/PII.md).

After an API change, refresh `docs/api/openapi.json` with
`uv run python tools/openapi_check.py --write` and regenerate the console client. CI compares
the spec with the merge base and refuses breaking changes; to compare two specs locally, use
`uv run python tools/openapi_breaking.py OLD NEW`. Record architectural choices and contract
amendments in [docs/decisions/README.md](docs/decisions/README.md).

Use a focused branch and describe the resulting behaviour and relevant validation in the
change description.

## Related repositories and test data

- The related Delimitus repository at `../Ristretto-python-conversion` is reference-only for
  patterns and tests. Do not modify it as part of SSC work.
- Do not use `RISTRETTO_*_LIVE` flags or create resources in GCP project `ristretto-506621`.
- The 161-app corpus is for internal testing only.
