# Builds: known limits

Part of the trust pack (GA-11.4). Each line is a fact from the code, decision 014 (SSC-015
amendment) or a measured check; none is a promise beyond what is written here. Decided for MVP V1
by the founder, 2026-10-08 (GA-6.5).

## Build network

- Builds run on the cell's Cloud Build default pool with open internet egress: a build step can
  reach any public host. This is how dependencies install from public registries. Locking build
  egress down (SSC-076) is not in V1. [cloud_build.py:25, 279-287; decision 014]
- A build runs as the cell's `ssc-build` account, which holds no storage role: it reads its own
  bundle only through a signed URL for that one bundle, valid at most 10 minutes.
  [cloud_build.py:8-9; decision 014]
- A build receives only its bundle URL and digest, the manifest's public build values, the start
  command and the system package list. App secrets set with `ssc secret set` are not among them.
  [cloud_build.py:252-267]
- So a dependency's install script runs with open egress and can read the app's source and public
  build values, but no app secret. Nothing in a build can reach the cell's database.
  [decision 014]
- Build logs go to Cloud Logging only. [cloud_build.py:283]

## Who can read source

- An org's source bundles rest under `bundles/` in its own cell's bucket; the control plane keeps
  no bucket for them. [decision 015, amendments 2026-10-07 and 2026-10-19]
- The control plane's API account: `bundles/` only, to sign upload URLs and check the upload.
  The control plane's worker: `bundles/`, `snapshots/` and `audit-anchors/` only, to build,
  collect, publish snapshots and write audit anchors. A build's `ssc-build` account: the one
  signed URL for its bundle. [cell.py `bucket-control-api-bundles`, `WORKER_PREFIXES`]
- The cell's gateway, data gateway and proxy read `snapshots/` only. [cell.py `bucket-gateway`,
  `bucket-data`, `bucket-proxy`]
- The cell agent can list and delete under `files/` only, to remove a deleted environment's
  files, and reads nothing. [cell.py `bucket-agent-files`, `FILES_PERMISSIONS`;
  ssc_agent/files.py]
- The operator's just-in-time grant on the cells folder can read every cell bucket. [decision 012]

## Secrets in source

- `.env` and `.env.*` files are never uploaded. [ignore.py:14-23]
- `ssc deploy` scans the folder before upload and uploads nothing if the local scan finds a
  secret; the build runs a wider scan and refuses what it finds. The control plane repeats the
  local scan on the stored bundle. Besides credential-like names, the local scan knows GitHub,
  GitLab, Slack, Stripe live, Anthropic, OpenAI, Google API, npm and SendGrid keys by their own
  shape under any name, as the build's gitleaks does (GA-12 F22). [deploy.py:316-324;
  bundles.py:142-166; secrets.py `_PROVIDER`]
- The build then runs gitleaks 8.30.1: its default rules (except `jwt`, since a Supabase anon key
  is public) plus SSC's rules for Supabase keys and database URLs with a password. A finding stops
  the build before anything is built: no image, no release, no deployment. Findings are printed
  redacted. All three refuse with `SECRET_IN_BUNDLE`. [cloud_build.py:79, 123-127, 200-247]
- Measured on cell 1, 2026-10-09 00:58 UTC: a GitHub token shape made of random characters (no
  live token) passed the CLI's scan and the control plane's re-scan, and the build refused it:
  gitleaks rule `github-pat`, `SECRET_IN_BUNDLE`. The scan step failed 19 s after the build was
  created, the later steps never ran, no image was pushed, and no release or deployment was made.
  The value appeared in no command output, build log line or audit row. The stored bundle that
  holds the random value stays in the cell's bundle store. Since GA-12 the first two scans find
  that shape themselves and nothing is uploaded. [GA-6.5, `proofrun buildsecret`]
- Limits: the first two scans skip files over 1 MiB, binary files, lockfiles and `ssc.toml`.
  Their name-keyed rule (a credential-like variable name assigned a random-looking value) reads
  only the first 4096 characters of a line; a longer line is minified code or data. Every rule
  runs in time linear in the line, and the control plane's repeat has 30 seconds per bundle,
  past which it refuses the bundle as `BUNDLE_MALFORMED`. Public build values are never flagged.
  A secret with a shape none of these rules knows passes all three. [secrets.py:3-19, 139-145;
  bundles.py:103-118]
