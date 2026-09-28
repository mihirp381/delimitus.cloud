# SSC-003 spike: 20 corpus apps

Runs 20 real AI-built apps from the internal corpus through Railpack, Docker with the SSC-001 rules
(non-root 10001, read-only, `PORT`, no network) and Cloud Run in the throwaway project, and ranks
how they fail. Results in `RESULTS.md`, raw data in `results.json`, selection in `selection.json`.

Corpus location: `/Users/mihir/Documents/Internal_App_Platform/corpus-sources/corpus/artifacts`
(read only; bundles are copied to the scratchpad to build). No app source belongs in this repo.

```
uv run python run_corpus.py select    # writes selection.json, seeds results.json
uv run python run_corpus.py scan      # static signals per app
uv run python run_corpus.py build     # railpack build -> linux/amd64 image corpus20/<id>
uv run python run_corpus.py run       # docker run under SSC rules, probe / for 60 s
uv run python run_corpus.py deploy    # push + Cloud Run in delimitus-0926, ingress internal
uv run python run_corpus.py cleanup   # delete services and registry repo, print leftovers
uv run python run_corpus.py report    # results.json -> TABLES.md
```

Requires Docker Desktop, `gcloud` logged in with `delimitus-0926` as the project, the `railpack`
binary on PATH, and a BuildKit daemon: `docker run --rm --privileged -d --name buildkit moby/buildkit`
then `export BUILDKIT_HOST=docker-container://buildkit`. Set `SSC_SCRATCH` to change the build copy
location. `build` is resumable (skips images already built). Run completed 2026-09-28; the Cloud Run
services and the `corpus20` registry repo were deleted and the empty listings recorded in
`results.json`.
