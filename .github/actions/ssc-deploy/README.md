# ssc-deploy

Deploys a folder to an app's preview environment on Small Software Cloud and prints `Preview: <url>`.

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
- uses: $/.github/actions/ssc-deploy # elsewhere: delimitus/ssc-deploy-action@<sha>, once it exists
  with: { token: "${{ secrets.SSC_PREVIEW_TOKEN }}", app: my-app }
```

- Inputs: `token` and `app` (a slug or an `app_` id) are required. Optional: `path` (default `.`), `api-url`, `commit` (default the workflow's commit, recorded on the release; empty records none) and `cli-spec` (what `uv tool install` installs; default the matching `ssc-cli` release). Every dependency is held to `constraints.txt`, the versions in `uv.lock`; after a lock change, regenerate it with `uv export -q --package ssc-cli --all-extras --no-dev --no-emit-workspace --no-hashes --frozen --no-header --no-annotate -o .github/actions/ssc-deploy/constraints.txt`.
- Outputs: `preview-url`, `release-id`, `operation-id`.
- There is no environment input: the Action only deploys to preview. Give it a token with scope `preview`, which the API never lets touch production. Production changes only through `ssc promote`.
- Runs on Linux and macOS runners. It installs uv 0.12.19 with `astral-sh/setup-uv` and Python 3.14 through uv.

## The token

- Create it with your own login: `ssc login --org <org id>`, then `ssc token create-ci --label <owner/repo>`. Store what it prints as a repository secret, `SSC_PREVIEW_TOKEN` in the example above. It is shown once and never kept. An agent's login cannot create one.
- It lasts 90 days at most: `--days N` for less (1 to 90), never more, and there is no refresh. Create a new one and replace the secret before it ends.
- It can do what you can do on preview and nothing on production: deploy preview of any app you can build, read what you can read, and ask to share preview. Promote, any request naming prod and any `DELETE` are refused. It acts as you and is audited as you.
- It ends when you revoke it (`ssc token list-ci`, then `ssc token revoke-ci <id>`; an org admin can revoke anyone's), when it expires, or when your account is deactivated. Logging out does not end it.
- A repository connected through the GitHub App (`ssc repo connect`) needs no token: SSC deploys its pushes itself.
