# SSC-003 results: 20 real apps against the SSC runtime rules

Date: 2026-09-28. Status: **static scan complete, runtime half pending Railpack.**

The permission classifier on this machine refused to install Railpack (install script from the
internet) and Homebrew has no formula. Everything below that is marked *static* is measured. Every
column marked *pending railpack* fills in when the finishing command at the bottom is run.

Corpus apps are internal testing only. No app source is in this repo. Secret-like strings are
counted, never recorded.

## Selection

20 apps, chosen by hand from `corpus-sources/corpus/artifacts` (`selection.json` has the reasons):

- 8 builder-export: 4 Lovable, 4 Replit
- 8 agent-repo: Cursor, Copilot, Lovable-marked and unmarked repos
- 4 arbitrary: two Lovable-style repos, a Replit Discord bot, a static site

Stacks: 6 Vite SPAs, 2 Next.js, 2 Express, 2 FastAPI, 2 Streamlit, 2 with a Dockerfile, 1 Flask,
1 Discord bot, 1 static HTML, 1 docker-compose pair.

## Twenty apps

| id | class | source | stack | latent buckets (static) | built | started locally | Cloud Run ready | outcome | final bucket |
|---|---|---|---|---|---|---|---|---|---|
| ab-attend-ops | arbitrary | lovable | docker-compose | uses SQLite on disk, binds to localhost only, needs build-time public variable | pending railpack | pending railpack | pending railpack | pending railpack | uses SQLite on disk |
| ab-cloudy-the-discord-bot | arbitrary | replit | python discord | uses SQLite on disk | pending railpack | pending railpack | pending railpack | pending railpack | uses SQLite on disk |
| ab-codesphinx-freshlaundry-withadmin-and-monthlypla | arbitrary | lovable | vite spa | needs Supabase auth, hard-coded key or password, needs build-time public variable | pending railpack | pending railpack | pending railpack | pending railpack | needs Supabase auth |
| ab-flat-calculator | arbitrary | unknown | static html | - | pending railpack | pending railpack | pending railpack | pending railpack | - |
| ar-broken-link-website | agent-repo | lovable | dockerfile | needs build-time public variable, needs a native library | pending railpack | pending railpack | pending railpack | pending railpack | needs build-time public variable |
| ar-budget-tracker | agent-repo | unknown | next.js | - | pending railpack | pending railpack | pending railpack | pending railpack | - |
| ar-draw-a-ui | agent-repo | copilot | next.js | - | pending railpack | pending railpack | pending railpack | pending railpack | - |
| ar-flask-expense-app | agent-repo | unknown | python flask | uses SQLite on disk, binds to localhost only | pending railpack | pending railpack | pending railpack | pending railpack | uses SQLite on disk |
| ar-habit-tracker | agent-repo | unknown | express | - | pending railpack | pending railpack | pending railpack | pending railpack | - |
| ar-nutri-agent-bot | agent-repo | cursor | python fastapi | needs Supabase auth, uses SQLite on disk | pending railpack | pending railpack | pending railpack | pending railpack | needs Supabase auth |
| ar-ohmytodolist | agent-repo | cursor | vite spa | - | pending railpack | pending railpack | pending railpack | pending railpack | - |
| ar-poi-extraction-tool | agent-repo | cursor | python streamlit | - | pending railpack | pending railpack | pending railpack | pending railpack | - |
| bx-clinical-evidence-synthesizer | builder-export | replit | python streamlit | - | pending railpack | pending railpack | pending railpack | pending railpack | - |
| bx-db-buddy | builder-export | lovable | vite spa | needs Supabase auth, hard-coded key or password, needs build-time public variable | pending railpack | pending railpack | pending railpack | pending railpack | needs Supabase auth |
| bx-eat-what-today | builder-export | lovable | vite spa | needs build-time public variable | pending railpack | pending railpack | pending railpack | pending railpack | needs build-time public variable |
| bx-fastapi-deta-on-replit | builder-export | replit | python fastapi | - | pending railpack | pending railpack | pending railpack | pending railpack | - |
| bx-fin-bloom-dash | builder-export | lovable | vite spa | needs build-time public variable, needs a native library | pending railpack | pending railpack | pending railpack | pending railpack | needs build-time public variable |
| bx-freecodecamp-project-exercise-tracker | builder-export | replit | express | - | pending railpack | pending railpack | pending railpack | pending railpack | - |
| bx-my-rest-api | builder-export | replit | dockerfile | - | pending railpack | pending railpack | pending railpack | pending railpack | - |
| bx-secure-shopper | builder-export | lovable | vite spa | needs build-time public variable | pending railpack | pending railpack | pending railpack | pending railpack | - |

"Latent bucket" means the source carries the signal; whether it fails at build or run is the
runtime half. Where an app carries several, the final bucket is the first that would stop it under
our rules (Supabase auth before SQLite before public variable).

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

## Ranked causes, runtime

Pending railpack. `run_corpus.py report` fills `TABLES.md` from `results.json` after `build`,
`run` and `deploy`; paste the two runtime tables here and re-rank.

## Draft fix-it messages, top six

Addressed to the builder, shown by the analyzer in SSC-015 when the signal is seen.

1. **Your app reads a `VITE_` or `NEXT_PUBLIC_` variable at build time.** These values are baked
   into the JavaScript when we build your image, so they have to exist before the build starts.
   Add them under "build variables" in your app settings, or move the value to a server-side call.
   Anything in a `VITE_`/`NEXT_PUBLIC_` variable is visible to everyone who can open the app, so
   never put a secret there.

2. **Your app uses a SQLite file on disk.** Our containers run read-only and are replaced on every
   deploy, so the file would be lost or unwritable. Ask for an app database (`ssc db create`) and
   point your ORM at the `DATABASE_URL` we hand you. If you only need a scratch file, write it under
   `/tmp`; it will not survive a restart.

3. **Your app has its own Supabase login.** Everyone reaching your app has already signed in with
   the company login, and we pass who they are in the `X-SSC-Identity` header on every request.
   Remove the Supabase sign-in screen and read the user from that header instead. If you still need
   Supabase as a database, keep the client but drop `supabase.auth`.

4. **Your app only listens on `localhost` or a fixed port.** Inside our runtime the app must listen
   on `0.0.0.0` and on the port in the `PORT` environment variable. Change the listen call to
   `host="0.0.0.0", port=int(os.environ["PORT"])` (Python) or `app.listen(process.env.PORT)` (Node).

5. **Your code contains something that looks like a key or password.** We block the deploy when we
   find one. Move it to an app secret (`ssc secret set NAME`) and read it from the environment. If it
   is a Supabase anon key, note that anon keys are public by design but still belong in a variable,
   not in the source.

6. **Your app runs its own scheduler or needs a native library.** In-process schedulers
   (`node-cron`, `APScheduler`) run once per replica and stop when we scale to zero. Declare a timer
   in `ssc.toml` and give it a path to call instead. Native modules (`sharp`, `bcrypt`, `canvas`)
   must have a prebuilt binary for Linux x86-64; if the build fails on them, switch to the pure
   JavaScript or wheel-based alternative (`bcryptjs`, `Pillow`).

Message 6 covers two low-count buckets together; split it once the runtime run says which one
actually fails more.

## What this means for SSC-015 and SSC-014

- SSC-015 (build service) should run the analyzer before Railpack, on the seven signals above plus
  `PORT`. The static scan is cheap: 20 apps in under 5 seconds with stdlib regexes.
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

## Finishing the runtime half

Install Railpack yourself (the classifier will not let the agent do it), then from this directory:

```
uv run python run_corpus.py build && uv run python run_corpus.py run && \
uv run python run_corpus.py deploy && uv run python run_corpus.py cleanup && \
uv run python run_corpus.py report
```

`deploy` uses project `delimitus-0926` only, Artifact Registry repo `corpus20` in `us-central1`,
Cloud Run services `corpus20-1..20` with ingress internal and no unauthenticated access.
`cleanup` deletes them and prints what is left. Then paste `TABLES.md` into this file.
