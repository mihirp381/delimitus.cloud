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

## Secrets in source

- `.env` and `.env.*` files are never uploaded. [ignore.py:14-23]
- `ssc deploy` scans the folder before upload; a finding uploads nothing. The control plane
  repeats the scan on the stored bundle. [deploy.py:301-309; bundles.py:100-121]
- The build then runs gitleaks 8.30.1: its default rules (except `jwt`, since a Supabase anon key
  is public) plus SSC's rules for Supabase keys and database URLs with a password. A finding stops
  the build before anything is built: no image, no release, no deployment. Findings are printed
  redacted. All three refuse with `SECRET_IN_BUNDLE`. [cloud_build.py:79, 123-127, 200-247]
- Limits: the first two scans skip files over 1 MiB, binary files, lockfiles and `ssc.toml`.
  Public build values are never flagged. A secret with a shape none of these rules knows passes
  all three. [secrets.py:3-5, 101-107]
