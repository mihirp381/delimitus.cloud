# Fly Machines: cost and speed control only

Not a candidate. Used for two numbers: the cheapest possible cold start for the same three images, and the cheapest possible empty-cell price. Install `flyctl` (`brew install flyctl`).

1. `fly launch --no-deploy` in each app directory, `fly deploy --ha=false`, one shared-cpu-1x machine, `auto_stop_machines = "stop"`, `min_machines_running = 0`.
2. Run `runner/run_checks.py --candidate fly --skip-holds` from a laptop against the public `.fly.dev` address; only the cold-start rows are meaningful. Egress and metadata rows will read "open"; that is expected and not scored.
3. Cost row: one machine stopped plus a 1 GB volume; take the price from the Fly pricing page and record the URL.
4. Destroy the apps afterwards: `fly apps destroy <name> --yes`.

## Run notes 2026-09-29 (org personal, iad)

- `fly launch` was not used. Each app got a hand-written `fly.toml` and `fly deploy <dir> --dockerfile <dir>/Dockerfile --remote-only --ha=false`. A `[build] dockerfile` path inside the toml is resolved relative to the toml, not absolute.
- Cold starts were timed by stopping every machine with `fly machine stop`, then timing the first 200 through the public proxy (5 samples per app). Streamlit used a 1 GB machine, the others 256 MB.
