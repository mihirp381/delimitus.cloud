# Decision records (SSC-006)

One short file per decision: the choice, the reason, and what would make us reverse it. Filled in as SSC-001, SSC-002 and SSC-005 finish.

| # | Decision | Source | Status |
|---|---|---|---|
| 001 | Cloud and runtime | SSC-001 scorecard (`spikes/bakeoff/SCORECARD.md`) | pending bake-off |
| 002 | Login vendor and directory join key | SSC-002 | pending |
| 003 | App database connection string form | SSC-005 (`spikes/appdb/RESULTS.md`) | draft |
| 004 | Domains: `delimitus.com` platform hosts; apps on a separate Delimitus-branded domain with opaque labels | tickets §2 SSC-006 | decided 2026-09-28 |
| 005 | The seven decide-once rules | build path | decided 2026-09-28 |
| 006 | Assumptions A1 to A7 | tickets §1 | decided 2026-09-28 |
| 007 | Toolchain pins and the 7-day rule | SSC-007 | decided 2026-09-28 |
| 008 | Job queue: Procrastinate 3.10.0 core on Postgres 18, no DBOS fallback | SSC-004 (`packages/ssc_control/tests/test_jobqueue_crash.py`) | decided 2026-09-28 |
| 009 | Control database rules: org-scoped tables, forced RLS, `SC001`, two roles, bounded PL/pgSQL | SSC-010 (`packages/ssc_control/src/ssc_control/db/`) | decided 2026-09-28 |
| 010 | Identity note v1: `X-SSC-Identity`, ES256 `ssc-id+jwt`, twelve claims, key on `sub`, verify once | SSC-020 (`docs/contracts/identity-note.md`) | decided 2026-09-29 |
| 011 | API conventions: RFC 9457 problems from one catalogue, `Idempotency-Key` on every POST claimed in the transaction, `If-Match` on sharing rules, deployments as operations, committed OpenAPI with a breaking-change gate | SSC-011 (`docs/api/README.md`) | decided 2026-09-29 |
| 012 | Audit log: one hash chain per org over frozen ssc-audit-v1 canonical bytes, allowlisted views, no client IP, `verify` naming the first broken link, admin-only search and audited export | SSC-012 (`packages/ssc_control/src/ssc_control/audit/`) | decided 2026-09-29; anchors and PITR re-anchor pending (A1b) |
| 017 | Command line tool: only working commands registered, exit codes 0 to 5, append-only `--json` shapes, `SSC_TOKEN` then keychain, stable `doctor` codes, marked agent-pack blocks, workspace names on PyPI with publishing gated | SSC-022 (`packages/ssc_cli/`) | decided 2026-09-29 |

## 008 Job queue

Choice: Procrastinate 3.10.0, core package only (psycopg connector, no SQLAlchemy extra), on the control-plane Postgres 18.

Reason: the four SSC-004 crash tests pass in CI against a `postgres:18` container. Deferring with `task.configure(connection=conn)` rides the caller's transaction, so a job and its data row commit or roll back together. `queueing_lock` refuses a duplicate with `AlreadyEnqueued`. A worker killed with SIGKILL mid-job leaves the job in `doing` with a stale worker heartbeat; `job_manager.get_stalled_jobs(seconds_since_heartbeat=...)` finds it and `retry_job` returns it to `todo`, after which a fresh worker runs it again (attempts becomes 2). A duplicate defer inside `conn.transaction()` (a savepoint) leaves the outer transaction usable; without the savepoint the connection is in `InFailedSqlTransaction`.

Rules this imposes on SSC-016 and every reconciler: defer through the caller's connection, never a second connection; give every idempotent job a `queueing_lock`; wrap each defer in a savepoint; run a periodic task that sweeps `get_stalled_jobs` and retries, because 3.10 has no built-in `retry_stalled_jobs`; set `update_heartbeat_interval` and `stalled_worker_timeout` on workers and keep `seconds_since_heartbeat` above the heartbeat interval.

Reverse if: a later Procrastinate release drops the psycopg connector, or a job needs exactly-once side effects outside Postgres (then DBOS or an outbox pattern is re-evaluated).

## 009 Control database

Choice: one Postgres 18 database for the control plane, schema `ssc`, with customer separation enforced by the database itself. Full rules in `packages/ssc_control/src/ssc_control/db/README.md`; the list of PL/pgSQL functions in `PLPGSQL.md` there; the personal-data inventory in `PII.md` there.

- Every table carries `org_id`; foreign keys are `(org_id, id)` pairs. Row-level security is enabled and forced on all 18 tables. `ssc.current_org()` raises SQLSTATE `SC001` when no org is bound; it never returns NULL.
- The bind is `set_config('ssc.org', <org id>, true)`, once per unit of work, in the transaction. Never per statement (Delimitus paid 5 to 7 round trips for that), never per session (a pooled-connection leak). A test refuses any non-transaction-scoped `set_config` in the package.
- Two roles. `ssc_migrate` runs Alembic and owns every object. `ssc_app` is the application: `NOBYPASSRLS`, explicit per-table privileges written in `catalog.APP_ROLE_PRIVILEGES`, nothing on `ssc.alembic_version`. No `GRANT ... ON ALL TABLES` (Delimitus `003_rls.sql:113` gave the app role DELETE on the migration ledger that way).
- Our SQLSTATE class is `SC`: `SC001` no org bound, `SC002` last admin, `SC003` owner not active, `SC004` release immutable, `SC005` audit row immutable, `SC006` truncate refused, `SC007` schedule deleted.
- PL/pgSQL is a bounded exemption from the Python rule: six functions today, limit ten, listed in `PLPGSQL.md` and enforced by a catalog test. Everything else is Python.
- Releases and audit rows are immutable: no UPDATE or DELETE privilege, plus triggers that refuse the owner too, plus TRUNCATE guards. One deployment in flight per environment is a partial unique index. A deployment's release and environment must belong to the same app (composite FKs through `app_id`).
- Approvals: `decided_via_agent` is `CHECK (= false)` and an approved request's decider must differ from its requester. Only a person approves, never the requester.
- Table names `user_account` and `user_group`, because `user` and `group` are reserved words in Postgres.
- Org creation is admin-first and atomic (`db.orgs.create_org`), seeding the audit head with a 32-byte zero genesis hash. The `org.created` audit row joins that transaction when SSC-012 lands the writer.
- Audit actions and actor kinds are closed lists: a CHECK in the database and a `StrEnum` in `ssc_contracts.audit`, with a test that they match. Adding an action is an expand migration.
- Alembic runs from Python (`ssc_control.db.upgrade`), no `alembic.ini`, version table in schema `ssc`, expand-then-contract. Revision SQL lives in `migrations/sql/` and runs through the raw psycopg cursor because PL/pgSQL bodies contain `%s`.
- Procrastinate's tables go in their own schema (`procrastinate`) when SSC-011 wires the worker. They are not org-scoped and stay outside the RLS catalog check.

Reason: the product's central claim is per-customer isolation enforced at the data layer. Retrofitting RLS onto an existing schema is much harder than starting with it, and the Delimitus live test showed every footgun this closes (owner exempt under plain ENABLE, silent zero rows on a missing scope, over-broad grants, session-scoped binds). Tests in `packages/ssc_control/tests/test_control_db.py` attack the schema through raw SQL as `ssc_app` against a `postgres:18` container.

Reverse if: the control plane must shard across databases (then org routing replaces RLS as the first guard and RLS stays as the second), or the chosen cloud's managed Postgres cannot create non-superuser owner roles the way Cloud SQL does (then the two-role split is re-planned in SSC-013).

## 010 Identity note v1

Choice: apps learn who the user is from one signed header, `X-SSC-Identity`, and nothing else. Contract in `docs/contracts/identity-note.md`.

- Compact JWS, `alg` ES256 only, `typ` `ssc-id+jwt` required, `kid` required. Twelve claims and no others: `iss`, `aud`, `sub`, `iat`, `exp`, `org`, `app`, `env`, `role`, `groups`, `name`, `email`. `aud` is the app's exact origin as one string. `exp = iat + 300`, and a verifier refuses a longer life even when the note has not expired.
- `sub` is `usr_` or `sch_`, never an email. `name` and `email` are display strings and are absent on schedule notes. `role` is `builder`, `user` or `schedule`; `groups` holds at most 50 `grp_` ids and only the groups the app's sharing rule references.
- Both helpers (`ssc_app.identity`, `@delimitus/ssc-identity`) raise one exception with one code from a closed list of twelve, run the same checks in the same order, and pass the same 33 vectors in `conformance/identity_note/`. The Node helper has no dependencies (webcrypto), so a customer app inherits nothing from us.
- Keys: EC P-256 in cell Secret Manager, public JWKS at `https://keys.delimitus.com/<cell_label>/jwks.json` as a static CDN file with at most two keys during rotation (SSC-013 implements). The gateway is the only minter (`ssc_edge.identity_note`).
- Apps verify once per request and never mid-stream; revoking access ends open streams through the kill switch drain, not through token expiry.

Reason: the Delimitus design left end-user identity forwarding unspecified and header spoofing unaddressed (build plan §2, problem 3). Fixing the header, algorithm, type and claim set on day one, with vectors both helpers must pass, means the gateway, the data gateway, the CLI's agent pack and every customer app agree before any of them is finished. `typ` and single-string `aud` close the two classic JWT confusions (a token minted for something else replayed here; a note for app A accepted by app B). Names and emails ride along because the founder asked for them (tickets §9 question 8) and they are on the PII list.

Reverse if: a customer needs a non-JWT form (then v2 is added beside v1, never in place), or the cell's key store cannot hold P-256 keys (then the algorithm set is widened by a v2 `typ`, not by accepting more algorithms under v1).
## 011 API conventions

Choice: one FastAPI application, `/v1` for people and their tools and `/internal/v1` for cell services, with the conventions below fixed before the first real endpoint. Full text in `docs/api/README.md`; code in `packages/ssc_control/src/ssc_control/api/`.

- Every refusal is an RFC 9457 problem with exactly seven members (`type`, `title`, `status`, `detail`, `instance`, `code`, `request_id`) rendered by one function. The catalogue (`ssc_contracts.errors.CATALOGUE`) holds fixed sentences per code; evidence goes to the log under the request id, never to the client. Codes today: `VALIDATION_FAILED`, `NOT_FOUND`, `METHOD_NOT_ALLOWED`, `UNSUPPORTED_MEDIA_TYPE`, `UNAUTHENTICATED`, `FORBIDDEN`, `RATE_LIMITED`, `IDEMPOTENCY_KEY_REQUIRED`, `IDEMPOTENCY_KEY_REUSED`, `IDEMPOTENCY_IN_FLIGHT`, `PRECONDITION_REQUIRED`, `PRECONDITION_STALE`, `ALREADY_EXISTS`, `REFERENCE_NOT_FOUND`, `DEPLOYMENT_IN_FLIGHT`, `LAST_ORG_ADMIN`, `OWNER_NOT_ACTIVE`, `RECORD_IMMUTABLE`, `SCHEDULE_DELETED`, `INTERNAL`.
- Credentials are ES256 JWTs with `typ: ssc-api+jwt`, one audience per surface, verified against a configured JWKS. The `jti` is the credential id; rate limits and idempotency keys are scoped to it. A user credential on `/internal/v1` is `FORBIDDEN`.
- One org-bound transaction per request (`bound_org`), opened by a FastAPI dependency with `scope="function"` so it commits before the response is sent. Audit rows are appended inside it (`ssc_control.audit.append_event`, chain `sha256(prev_hash || canonical)` over sorted-key JSON, head locked `FOR UPDATE`).
- `Idempotency-Key` is required on every `POST` and claimed in the transaction through table `ssc.idempotency_claim` (revision `0002`, the 19th table, keyed by org, credential and key, RLS like the others). States: absent, claimed, settled. A duplicate blocks on the primary key until the first commits, then replays the stored status, body and headers with `Idempotency-Replayed: true`. Same key with a different body is `IDEMPOTENCY_KEY_REUSED`; a visible unsettled claim is `IDEMPOTENCY_IN_FLIGHT` and is never re-run. Only successful replies are stored; a refusal rolls the claim back.
- Sharing-rule edits (`PUT .../grants`) require `If-Match` equal to the environment's `grants_version`, returned as `ETag`. Missing: `428`; stale: `412`. The row is locked `FOR UPDATE` before the comparison; the edit is a diff (add and remove, each audited) and bumps the version.
- A deployment is an operation: `202` plus `Location: /v1/operations/{id}`; a second one in flight is `409 DEPLOYMENT_IN_FLIGHT` from the partial unique index.
- Rate limits are an in-process token bucket per credential with `Retry-After`. Settings are a frozen dataclass read from `SSC_*` environment variables; no settings library.
- `docs/api/openapi.json` is committed, rendered deterministically (sorted keys) and checked in CI; `tools/openapi_breaking.py` fails the lint job on a removed path, operation, response status or response property, a request property removed or made required, a changed type, a removed enum value or a new required parameter. The gate has a planted fixture like every other.

Reason: these are the things every client (CLI, Action, console, agents) will hard-code against, and they are cheap to fix now and expensive after the first external caller. Storing the reply with the claim is what makes "replay returns the first result" true byte for byte rather than "probably the same".

Reverse if: more than one API replica needs a shared rate limiter (then a store replaces the in-process bucket, keeping the header contract), or a client needs partial-failure detail beyond the fixed text (then a structured `errors` member is added to `Problem` as an additive change, never free text).

## 012 Audit log

Choice: every org has one append-only, hash-chained log in `ssc.audit_event`, written only by `ssc_control.audit.append_event` inside the transaction that makes the change. Code in `packages/ssc_control/src/ssc_control/audit/`.

- Canonical form ssc-audit-v1, frozen: a JSON object with exactly nine keys, `org_id`, `seq`, `at`, `action`, `actor` (`kind`, `id`, `via_agent`, `client_id`, `ip`), `target` (`kind`, `id`), `before`, `after`, `policy_decision_id`, encoded as `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` in UTF-8. `at` is ISO 8601 with its offset (the writer uses UTC). No floats anywhere. No version key: its absence means v1; a v2 adds `"format": "ssc-audit-v2"` and v1 rows stay as written.
- Chain: `hash = sha256(prev_hash || canonical)`. Genesis: `create_org` seeds `audit_head` at seq 0 with 32 zero bytes and appends `org.created` as seq 1 in the same transaction (default actor `operator` / `system:create_org`). An append locks `audit_head` `FOR UPDATE`; unique `(org_id, prev_hash)` and `(org_id, hash)` make a fork unstorable.
- Views: `before` and `after` hold only the keys listed for the row's target kind in `audit/views.py`, as strings, integers, booleans, nulls or lists of strings. The one nested object is an `audit` row's `filters`, limited to the search filter names. The views name no personal data and no secret (a test checks them against `db/catalog.py`), so an erasure request never needs the log edited. A new target kind adds its view before its first audit call.
- Client IP: none is recorded until SSC-013/018 supply a trusted address (lead decision, 2026-09-29). `actor_ip` stays NULL and the canonical `actor.ip` is null. How a trusted address is kept later (in the chain or in an erasable side table) is decided with SSC-013; exports carry canonical bytes only while they hold no IP.
- Policy link: `uow.audit(..., policy_decision_id=...)` stores the `pol_` id of the decision that allowed the change, a foreign key to `ssc.policy_decision`, and it is inside the canonical.
- `verify(conn, org_id)` walks the chain from genesis in seq order and names the first broken link as a seq and a cause: `missing` (a gap, or a head beyond the last row), `prev_link`, `hash`, `fields` (the canonical is not v1 canonical JSON or disagrees with the row's columns) or `head`. `python -m ssc_control.audit verify --org <id>` runs it in a REPEATABLE READ transaction with `SSC_DATABASE_DSN`, prints `ok: ...` or `broken: seq N cause C`, and exits 0 or 1.
- Reading: `GET /v1/audit` pages newest first on a `before_seq` cursor, for active org admins with a user credential; operators are refused in MVP (no standing staff access). `GET /v1/audit/export?format=csv|jsonl` first commits an `audit.exported` row (`after`: format and filters), then streams oldest first from its own read-only REPEATABLE READ snapshot, so the export lists itself. Agent credentials are refused. `actor_ip` is never exported; CSV cells starting with `=`, `+`, `-`, `@`, tab or carriage return get a leading `'`; each JSON line adds `canonical` in base64 so a customer can recompute every hash offline.
- What it protects against today: the application role cannot UPDATE, DELETE or TRUNCATE the log (no privilege, and triggers raising `SC005`/`SC006` refuse the owner too). A database superuser, or the owner with `session_replication_role = replica`, can still rewrite a row and recompute every later hash and the head; `verify` alone cannot see that. Anchors written outside the database detect it (A1b: a daily `ssc-audit-anchor/v1` object per org in the bucket, and `verify --anchors`), and the bucket retention lock (Step 5) stops anchors being rewritten. Until both land the log is tamper-evident against the application role only.
- Point-in-time restore (procedure lands with A1b's `reanchor`): restore to T; run `verify` for every org; compare each head with the newest bucket anchor, where an anchor seq above the head is expected loss and a different hash at the same seq is tampering (stop); then write a `restore` anchor with `restored_to = T` and append `audit.reanchored`.

Reason: the log is the evidence customers and auditors rely on after something goes wrong, so its bytes must be reproducible by anyone, forever, from the export alone. Freezing the canonical form before the first export is cheap; changing it after would split every chain. The views keep personal data and secrets out of a table that can never be edited, and serialising appends on one head row keeps the chain linear under concurrent requests without a sequence or advisory lock.

Reverse if: an org's append rate makes the single head row a bottleneck (then per-org chains are split into dated segments linked by their first `prev_hash`, as a v2), or a regulator requires the client IP inside the evidence (then the IP rule is revisited with SSC-013 before any IP is written).

## 017 Command line tool

Choice: `ssc` is a Typer 0.27 application over httpx2 in `packages/ssc_cli`. A command is registered only once it works against the live API; `test_help_lists_exact_set` holds the allowlist.

- Commands now (SSC-022 step 1): `whoami`, `token set`, `token clear`, `apps`, `apps create`, `status`, `share`, `unshare`, `doctor`, `init`. Later, each when its endpoint exists: `deploy`, `releases` and `rollback` (B3, B4), `promote` (C3), `disable` (B5), `mcp` (C5), `access explain` (A4), `login` (SSC-019, which makes `token set` the fallback), `logs` (SSC-024). `ssc deploy` always targets preview; production changes only through `promote`.
- Exit codes: 0 ok; 1 refused by the API or failed; 2 bad usage; 3 no token or 401; 4 `doctor` found a blocking problem; 5 network failure after retries. Usage errors are Typer's own: the message goes to stderr and there is no JSON, even with `--json`.
- `--json` on every command prints one object on stdout with sorted keys. A failure prints `{"error": {...}}` on stdout with `code`, `title`, `detail`, `status`, `request_id`, `instance` and `type`. API refusals keep the problem's members. Failures found locally have `status: null` and one of `NO_TOKEN`, `NO_KEYCHAIN`, `BAD_TOKEN_INPUT`, `BAD_API_URL`, `BAD_CONFIG`, `NETWORK_ERROR`, `BAD_RESPONSE`, `APP_NOT_FOUND`, `ENVIRONMENT_NOT_FOUND`. The shapes are append-only: fields are never renamed, removed or retyped. `packages/ssc_cli/tests/json_shapes.json` records every field and `test_json_shapes_are_append_only` enforces it.
- Client: each POST sends one `Idempotency-Key` and reuses it across retries. GET and POST retry transport errors and 5xx three times (0.5, 1 and 2 s); POST also retries `IDEMPOTENCY_IN_FLIGHT`. Any request gets one wait of `Retry-After` (capped at 60 s) after a 429. PUT is not otherwise retried, except that `share` and `unshare` re-read and re-apply after a 412 `PRECONDITION_STALE`, at most three times, and send nothing when there is nothing to change. Response models ignore unknown fields; `test_client_models_match_openapi` checks each field against `docs/api/openapi.json`.
- Token: `SSC_TOKEN`, else the OS keychain through `keyring` (service `ssc`, one entry per API address). `ssc token set` reads the token from stdin only and checks it with `GET /v1/whoami` before keeping it. A machine with no keychain gets an error that points at `SSC_TOKEN`.
- API address: `--api`, then `SSC_API_URL`, then `api_url` in `config.toml` in Typer's app folder for `ssc`, then `https://api.delimitus.com`. Plain http is refused except for localhost, 127.0.0.1 and ::1.
- `doctor` works offline on files only. Codes and severities, stable from now on: `LOCKFILE_STALE` block, `PUBLIC_ENV_AT_BUILD` warn, `NO_START_COMMAND` block, `PORT_BINDING` block, `EXTERNAL_SERVICE` warn, `WRITES_HOME` warn, `NOT_SINGLE_APP` block. `MANIFEST_MISSING` (warn) and `MANIFEST_INVALID` (block) join when B1's manifest loader lands (decision 013).
- `init` writes the agent pack: a block between `<!-- ssc:begin v1 -->` and `<!-- ssc:end -->` in `AGENTS.md` (created if absent) and, only if `CLAUDE.md` exists, a block there holding `@AGENTS.md`; `.claude/skills/ssc/SKILL.md`, which belongs to ssc; `ssc.toml` and `.sscignore` only if absent. Text outside the markers is never changed. A changed block or skill file is replaced only with `--force`, and broken or repeated markers are left alone. The command list is rendered from the registered commands, and a test checks that every `ssc <command>` the tool prints is real. The pack names only features that exist or are decided, and says which are drafts.
- PyPI names are the workspace names, `ssc-cli`, `ssc-contracts`, `ssc-shared`, `ssc-bundle` (and `ssc-app`); the command stays `ssc` because that name is taken on PyPI. Install with `uv tool install ssc-cli` (Python 3.14). `.github/workflows/release-cli.yml` builds the four packages on a `cli-v*` tag and publishes them with trusted publishing from the `pypi` environment. Both jobs are skipped until the repository variable `SSC_PUBLISH_ENABLED` is `true`.
- `tools/dev_stack.py` runs the real API locally (`up`, `token`, `serve`) with the issuer `https://dev.invalid`. The CLI's live tests run it on a free port against a `postgres:18` container.

Reason: scripts and coding agents drive this tool, so its exit codes and JSON are an API and get the same no-breaking-change rule as `/v1`. Listing only working commands keeps the agent pack honest: an agent told about `ssc deploy` before it exists would try it. Reading the token from stdin and keeping it in the keychain keeps it out of shell history and process listings.

Reverse if: a command must stream (then `--json` gains a JSON-lines mode as an additive change), or PyPI refuses the names (then the `delimitus-` prefix, option B of D4).
