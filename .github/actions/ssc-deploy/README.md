# ssc-deploy

Deploys a folder to an app's preview environment on Small Software Cloud and prints `Preview: <url>`.

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
- uses: $/.github/actions/ssc-deploy # elsewhere: delimitus/ssc-deploy-action@<sha>, once it exists
  with: { token: "${{ secrets.SSC_PREVIEW_TOKEN }}", app: my-app }
```

- Inputs: `token` and `app` (a slug or an `app_` id) are required. Optional: `path` (default `.`), `api-url`, `commit` (default the workflow's commit, recorded on the release; empty records none) and `cli-spec` (what `uv tool install` installs; default the matching `ssc-cli` release).
- Outputs: `preview-url`, `release-id`, `operation-id`.
- There is no environment input: the Action only deploys to preview. Give it a token with scope `preview`, which the API never lets touch production. Production changes only through `ssc promote`.
- Runs on Linux and macOS runners. It installs uv 0.12.19 with `astral-sh/setup-uv` and Python 3.14 through uv.
