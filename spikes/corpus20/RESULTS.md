# SSC-003 results: 20 real apps against the SSC runtime rules

Date: 2026-09-28. Status: **complete: static scan, Railpack builds, local runs under the SSC rules, Cloud Run deploys, cleanup verified.**

Static columns come from a source scan; runtime columns were measured with Railpack 0.40.1 on this machine and on Cloud Run in the throwaway project `delimitus-0926`. Every Cloud Run service and the registry repo were deleted afterwards (see Cleanup below).

Corpus apps are internal testing only. No app source is in this repo. Secret-like strings are
counted, never recorded.

## Selection

20 apps, chosen by hand from `corpus-sources/corpus/artifacts` (`selection.json` has the reasons):

- 8 builder-export: 4 Lovable, 4 Replit
- 8 agent-repo: Cursor, Copilot, Lovable-marked and unmarked repos
- 4 arbitrary: two Lovable-style repos, a Replit Discord bot, a static site

Stacks: 6 Vite SPAs, 2 Next.js, 2 Express, 2 FastAPI, 2 Streamlit, 2 with a Dockerfile, 1 Flask,
1 Discord bot, 1 static HTML, 1 docker-compose pair.

## Ranked causes, static scan (n=20)

| bucket | apps | what the scan saw |
|---|---:|---|
| needs build-time public variable | 7 | `VITE_*` or `NEXT_PUBLIC_*` read in source; every Lovable export does this for its Supabase URL and anon key |
| uses SQLite on disk | 4 | `sqlite3` import or `.db` file; three of the four also keep the file next to the code, so a read-only, non-root container breaks them |
| needs Supabase auth | 3 | `@supabase/supabase-js` with `supabase.auth`; the app has its own login, which our front-door login would sit in front of |
| binds to localhost only | 2 | `app.run(host="127.0.0.1")` or a Vite `server.host` pinned to localhost |
| hard-coded key or password | 2 | secret-shaped strings in client code (both are Supabase anon keys, redacted by corpus ingest) |
| needs a native library | 2 | `sharp` or `bcrypt` in a dependency manifest |

Cross-cutting, not a ticket bucket but the biggest single runtime risk:

| signal | apps |
|---|---:|
| never reads `PORT` | 17 of 20 |
| hard-codes a port number | 11 of 20 |
| exposes a health path | 0 of 20 |
| ships a Dockerfile | 2 of 20 |

Railpack's framework providers cover the port for Vite, Next.js and Streamlit, so "never reads
PORT" only bites the hand-written servers (Flask, FastAPI with `uvicorn.run(port=8000)`, Express
with `app.listen(3000)`). Expect 4 to 6 of the 20 to fail the local run on this alone.

## Runtime results (measured 2026-09-28)

Railpack 0.40.1, images built for linux/amd64, run locally as uid 10001 with a read-only root, `/tmp` tmpfs,
`PORT=8080`, no network, probed at `/` for 60 s from inside the container's network namespace. Cloud Run in
`delimitus-0926`, us-central1, ingress internal, no unauthenticated access, 1 CPU, 512 MiB, max 1 instance.

| stage | count of 20 |
|---|---:|
| built by Railpack | 13 |
| answered HTTP locally under the SSC rules | 7 |
| Cloud Run revision became ready | 8 |

| id | class | source | stack | built | started locally (HTTP, seconds) | Cloud Run ready (deploy seconds) | outcome | final bucket | evidence |
|---|---|---|---|---|---|---|---|---|---|
| ab-attend-ops | arbitrary | lovable | unknown | no | skipped | skipped | build failed | other: monorepo, no app at root | Railpack found backend/ and frontend/ but no provider at the root |
| ab-cloudy-the-discord-bot | arbitrary | replit | python discord | no | skipped | skipped | build failed | other: stale lockfile | poetry.lock incompatible with current Poetry; also a Discord bot, not an HTTP app |
| ab-codesphinx-freshlaundry-withadmin-and-monthlypla | arbitrary | lovable | vite spa | no | skipped | skipped | build failed | other: stale lockfile | bun install --frozen-lockfile: lockfile had changes |
| ab-flat-calculator | arbitrary | unknown | static html | no | skipped | skipped | build failed | other: prebuilt static site, no index at root | only dist/ and src/, no provider detected |
| ar-broken-link-website | agent-repo | lovable | dockerfile | yes | yes (200, 3.1 s) | yes (12.7 s) | runs | runs but: needs build-time public variable | HTTP 200 from Caddy static build; VITE_ Supabase values baked at build |
| ar-budget-tracker | agent-repo | unknown | next.js | yes | yes (500, 10.3 s) | yes (49.3 s) | runs | runs but: other: third-party auth (Clerk) needs keys | HTTP 500: @clerk/nextjs Missing publishableKey |
| ar-draw-a-ui | agent-repo | copilot | next.js | yes | yes (200, 17.6 s) | yes (49.3 s) | runs | - | HTTP 200; OpenAI key needed only when drawing is submitted |
| ar-flask-expense-app | agent-repo | unknown | python | no | skipped | skipped | build failed | other: pins too old for Python 3.13 | requirements.txt is UTF-16 (Windows pip freeze); numpy 1.24 and cffi 1.15 cannot build on 3.13 |
| ar-habit-tracker | agent-repo | unknown | express | yes | no | no | fails at run | other: needs an external database (MongoDB URI) | MongooseError: uri must be a string, got undefined |
| ar-nutri-agent-bot | agent-repo | cursor | python fastapi | yes | no | no | fails at run | needs Supabase auth | SupabaseException: supabase_url is required |
| ar-ohmytodolist | agent-repo | cursor | vite spa | yes | yes (200, 2.4 s) | yes (9.9 s) | runs | - | HTTP 200, Vite static via Caddy |
| ar-poi-extraction-tool | agent-repo | cursor | python streamlit | yes | no | no | fails at run | other: no start command detected | Railpack produced /bin/bash as the command; Streamlit app with no Procfile or railpack.json |
| bx-clinical-evidence-synthesizer | builder-export | replit | python streamlit | yes | no | no | fails at run | other: wrong start command for Streamlit | Railpack ran `python app.py`; Streamlit needs `streamlit run app.py` |
| bx-db-buddy | builder-export | lovable | vite spa | no | skipped | skipped | build failed | other: stale lockfile | bun install --frozen-lockfile: lockfile had changes |
| bx-eat-what-today | builder-export | lovable | vite spa | no | skipped | skipped | build failed | other: stale lockfile | bun install --frozen-lockfile: lockfile had changes |
| bx-fastapi-deta-on-replit | builder-export | replit | python fastapi | yes | no | no | fails at run | other: hosting-platform SDK (Deta) | ImportError: cannot import name 'Deta' from 'deta' |
| bx-fin-bloom-dash | builder-export | lovable | vite spa | yes | yes (200, 2.1 s) | yes (8.0 s) | runs | runs but: needs build-time public variable | HTTP 200; Supabase URL and anon key baked at build |
| bx-freecodecamp-project-exercise-tracker | builder-export | replit | express | yes | yes (200, 11.3 s) | yes (14.6 s) | runs | runs but: other: needs an external database (MongoDB) | HTTP 200 while Mongoose retries the connection |
| bx-my-rest-api | builder-export | replit | dockerfile | yes | no | yes (19.6 s) | fails at run | other: non-root user has no writable home | npm start fails writing /.npm/_logs as uid 10001 |
| bx-secure-shopper | builder-export | lovable | vite spa | yes | yes (200, 2.6 s) | yes (41.5 s) | runs | runs but: needs build-time public variable | HTTP 200; Supabase values baked at build |

### Ranked failure causes, runtime (build and run, n=13)

| cause | apps |
|---|---:|
| other: stale lockfile | 4 |
| other: no or wrong start command (Streamlit) | 2 |
| other: monorepo, no app at root | 1 |
| other: prebuilt static site, no index at root | 1 |
| other: pins too old for Python 3.13 | 1 |
| other: needs an external database (MongoDB URI) | 1 |
| needs Supabase auth | 1 |
| other: hosting-platform SDK (Deta) | 1 |
| other: non-root user has no writable home | 1 |

### Runs, but carries a problem our rules will surface

| cause | apps |
|---|---:|
| needs build-time public variable | 3 |
| other: third-party auth (Clerk) needs keys | 1 |
| other: needs an external database (MongoDB) | 1 |

Cleanup: services left `[]`, registry repos left `[]`.

### What the runtime run changed in the ranking

- The static scan's top bucket (build-time public variables, 7 apps) did not stop a single build.
  Vite bakes an empty string and the site still serves; the app breaks only when a user reaches a
  Supabase call. It is a "runs but" problem, not a "fails" problem, so its message moves to a
  warning shown after deploy, not a block.
- The single biggest real failure is one the static scan did not have a bucket for: **stale lock
  files** (4 of 20). Lovable exports carry a `bun.lockb` that no longer matches `package.json`, and
  Railpack installs with `--frozen-lockfile`. Poetry has the same failure. Add the bucket.
- Two Streamlit apps built fine and then ran the wrong command (`python app.py`, or no command at
  all). Railpack's Python provider does not recognise Streamlit unless a `Procfile` or
  `railpack.json` names the start command. Add the bucket "no or wrong start command".
- SQLite never got to fail: three of the four SQLite apps died earlier (stale lockfile, monorepo,
  old pins), the fourth is a Discord bot. Keep the message; it is still right, it is just not first.
- The non-root rule alone breaks one app (`npm start` as uid 10001 with no `HOME`): Cloud Run, which
  runs images as root by default, accepted the same image. Our image rule must set `HOME=/tmp` or
  create the user's home, or this becomes a class of "works on Cloud Run, fails on SSC" bugs.
- Local and Cloud Run outcomes agree on 12 of 13 built apps; the one disagreement is the `HOME`
  case above. The local harness is therefore a trustworthy stand-in for the cloud in SSC-015 tests.
- Cloud Run deploy time for a ready revision: 8 to 49 seconds (median about 17 s). Time to first
  HTTP answer locally: 2 to 18 seconds under amd64 emulation, so treat those as upper bounds.

## Draft fix-it messages, top six

Addressed to the builder, shown by SSC-015 when the signal is seen. Re-ranked by what actually
failed in the runtime run; the static-only messages that did not fail (SQLite, localhost bind,
scheduler, native library) drop to a second tier kept in the appendix.

1. **Your lock file does not match your dependency list.** Lovable and Replit exports often ship
   a `bun.lockb` or `poetry.lock` that is older than `package.json` or `pyproject.toml`. We install
   with the lock file frozen, so the build stops. Run `bun install` (or `poetry lock`) locally,
   commit the updated lock file, and deploy again.

2. **Your app reads a `VITE_` or `NEXT_PUBLIC_` variable at build time.** These values are baked
   into the JavaScript when we build your image, so they have to exist before the build starts. Add
   them under "build variables" in your app settings. Anything in such a variable is visible to
   everyone who can open the app, so never put a secret there. Your app will still start without
   them, but every call that uses them will fail.

3. **We could not tell how to start your app.** Streamlit, Gradio and plain-script apps need a start
   command. Add a `Procfile` with one line, for example `web: streamlit run app.py --server.port
   $PORT --server.address 0.0.0.0`, or set the start command in `ssc.toml`.

4. **Your app needs a database or login service we do not provide.** MongoDB, Supabase and Clerk
   connection strings are read from the environment and were missing, so the app crashed on start.
   For a database, ask for an app database (`ssc db create`) and switch the driver to Postgres. For
   login, remove the vendor sign-in and read the user from the `X-SSC-Identity` header we send on
   every request.

5. **Your app writes to its home directory.** We run apps as a non-root user with a read-only file
   system. `npm start` and some Python tools write caches under `$HOME`. Write scratch files under
   `/tmp` only. (Platform note for SSC-015: set `HOME=/tmp` in every image so this class goes away.)

6. **Your repository is not a single app at the root.** A `backend/` and `frontend/` pair, or a
   folder with only prebuilt `dist/` files, has nothing we can build. Deploy each app from its own
   folder (`ssc deploy ./backend`), or add an `index.html` at the root for a static site.

Second tier, kept for the analyzer but not shown by default because none of them stopped an app in
this run: SQLite on disk, localhost-only bind, in-process scheduler, native library, hosting-platform
SDK (Deta, Replit DB), dependency pins too old for the default Python (fix: add a `.python-version`).

## What this means for SSC-015 and SSC-014

- SSC-015 (build service) should run the analyzer before Railpack, on the seven signals above plus
  `PORT`, the lock-file drift check and the start-command check. The static scan is cheap: 20 apps
  in under 5 seconds with stdlib regexes. Lock-file drift is checkable offline (`bun install
  --frozen-lockfile --dry-run`, `poetry check --lock`).
- SSC-015 must set `HOME=/tmp` (or create the user's home) in every image, and SSC-014's `ssc
  doctor` should run the image locally as uid 10001 with a read-only root, because that is the one
  rule Cloud Run does not enforce for us.
- Railpack's Python provider needs a Procfile for Streamlit. `ssc doctor` should write one when it
  sees `import streamlit` and no start command.
- The `PORT` and `0.0.0.0` check belongs in `ssc doctor` (SSC-014) because it is the most common
  problem and the cheapest to fix on the laptop.
- Lovable exports are one shape: Vite SPA plus Supabase, with `VITE_SUPABASE_URL` and the anon key
  in the source. One "Lovable import" fix-it path that covers messages 1, 3 and 5 together will
  handle most of them.
- Nothing in the sample uses an in-process scheduler; keep that message but expect it to be rare.
- Every app lacks a health path. SSC-015's rule "answer a health path" must fall back to "answers
  `/` with any HTTP status within 60 s" or almost nothing will pass.
- Two apps ship a Dockerfile. Decide in SSC-015 whether we honour it (faster) or ignore it (our
  rules win). Recommendation: ignore it in MVP; Railpack owns the image.

## How the runtime half was run

Railpack 0.40.1 installed by the founder. BuildKit ran as `moby/buildkit` in a privileged container
(`BUILDKIT_HOST=docker-container://buildkit`), images built for `linux/amd64` so the same image
served the local run and Cloud Run. Two harness lessons worth keeping:

- `docker run --network none` silently drops `-p` port publishing, so the first local pass probed
  nothing. The probe now runs `curlimages/curl` inside the app container's network namespace.
- Emulated amd64 builds filled the host disk (4 GB free) and crashed the Docker Desktop VM twice
  with ext4 write errors. Clearing caches and Docker's build cache (35 GB free) fixed it. Run this on
  a Linux amd64 box next time.
