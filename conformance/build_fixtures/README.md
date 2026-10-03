# Build fixtures (SSC-015)

Source trees a build is tested against. `packages/ssc_bundle/tests/test_build_fixtures.py`
holds the expected outcome of each and proves what runs without cloud; the rest need a live
build in a cell.

The first fifteen are Delimitus' deploy fixtures, copied unchanged from the `bundle/` folder of
`corpus/artifacts/{arbitrary,builder-export,notebook}/<name>` at Delimitus commit `a31f193a`.
Their `origin.json` says `source: synthetic`, `redistribution: shareable`. The two
`.env.example` files are left out: ssc never uploads a `.env` file. The secrets in
`cs-flask-hello` are Delimitus' redacted stand-ins and stay, so the scans have input.

`sqlite-on-disk` and `dash-app` are SSC's own: SQLite on disk is refused, and a Dash app is a
session app without `sessions = true`.
