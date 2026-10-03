# Browser isolation suite (SSC-029)

Playwright on Chromium and WebKit, proving that two apps in one cell cannot attack each other
through the browser, and that the gateway (SSC-018) behaves as its done-when says. It tests the
gateway, not the console, so it lives here rather than in `console/e2e`; it uses the console's
Playwright, TypeScript and Node versions and the same `run.mjs` pattern.

It covers two rows of the isolation matrix: **public entry and TLS** and **login sessions**.

## Two targets

The suite reads everything from `SSC_ISO_*` variables, which `run.mjs` sets. Nothing in it names a
real host.

### Fast: the gateway and real Envoy in Docker (pre-merge, CI job `isolation`)

```sh
cd e2e/isolation
npm ci
PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install chromium webkit
npm test                       # both browsers; extra arguments go to `playwright test`
npm test -- --project=webkit cookies.spec.ts
```

It needs Docker, uv (`uv sync --all-packages` at the repository root) and Node 22.22+, 24.15+ or 26+.
`run.mjs fast` starts `rig.py fast` and stops it afterwards. The rig runs one cell on this machine:

- the gateway's authorisation service and stream relay in-process, with a snapshot of one org that
  has `alpha` (prod) and `bravo` (prod and preview), and three people: one granted everything, one
  granted the two prod environments, and one granted nothing;
- real Envoy in Docker with the config the gateway ships (`ssc_edge.envoy`);
- `apps/server.mjs` as apps A (`alpha`) and B (`bravo`), reached by Envoy under their `run.app` names;
- stand-ins for what a cell has and Docker does not: a TLS front with a throwaway certificate for the
  load balancer (so the browsers run with `ignoreHTTPSErrors`), an auth host that issues one-time
  login codes and answers `/internal/redeem` (the gateway redeems through its real `HttpRedeemer`),
  a CONNECT proxy that resolves the cell's names for the browsers, and a cut of every stream at its
  environment's limit, as Cloud Run ends a request. Bravo's preview has a limit of 6 seconds
  (`SSC_ISO_LIMIT_SECONDS` changes it), so the limit case takes seconds;
- switches the fail-closed cases use: stop the authorisation service as Envoy sees it, and make the
  snapshot too old to use.

A pass here is not a pass of SSC-029: the ticket's done-when is the live run against a staging cell.

### Live: a staging cell (nightly, `.github/workflows/isolation-nightly.yml`)

```sh
SSC_ISO_CELL1_BASE=... SSC_ISO_AUTH_URL=https://... SSC_ISO_USER=usr_... ... npm run live
```

| Setting | Kind | What |
| --- | --- | --- |
| `SSC_ISO_CELL1_BASE` | variable, required | cell 1's app base host, e.g. `<label>.<apps domain>`; apps are `alpha.<base>`, `bravo.<base>`, `bravo--preview.<base>` |
| `SSC_ISO_AUTH_URL` | variable, required | the auth host's origin, `https://...` |
| `SSC_ISO_CELL1_ORG` | variable | the org that owns the test apps in cell 1 (for sealing) |
| `SSC_ISO_USER` | variable | a person granted alpha prod, bravo prod and bravo preview |
| `SSC_ISO_OTHER_USER` | variable | any other active person of the org |
| `SSC_ISO_OUTSIDER` | variable | an active person of the org granted none of the test apps |
| `SSC_ISO_CELL2_BASE` | variable, optional | cell 2's app base host; the two-cell cases skip without it |
| `SSC_ISO_GATEWAY_RUN_APP` | variable, optional | cell 1's gateway service `run.app` host; its case skips without it |
| `SSC_ISO_LIMIT_SECONDS` | variable, optional | bravo preview's request limit; default 3600 |
| `SSC_ISO_CELL1_KEYRING` | secret | cell 1's session keyring JSON: sessions are sealed with it (SSC-086 allows this while the auth host is not deployed). `run.mjs` passes it only to `rig.py sealer`, never to the browsers |
| `SSC_ISO_AUTH_STATE` | secret, optional | a Playwright storage state signed in to the auth host as `SSC_ISO_USER`; the cases that need a real login skip without it |
| `SSC_ISO_NIGHTLY=1` | set by the workflow | runs the 60-minute limit case and two workers |

The nightly job is inert until `SSC_ISO_CELL1_BASE` is set as a repository variable, and runs only
on `main`. It uses no cloud credentials.

In the staging cell, before the first run:

1. Copy the reconnect helper next to the test app and deploy it twice from `apps/` (it is a session
   app, one instance, so its in-memory log sees every request):
   ```sh
   mkdir -p apps/ssc-reconnect && cp ../../helpers/node/ssc-reconnect/{index.js,browser.js} apps/ssc-reconnect/
   ssc apps create alpha && ssc deploy apps --app alpha --wait && ssc promote alpha
   ssc apps create bravo && ssc deploy apps --app bravo --wait && ssc promote bravo
   ```
   Bravo's preview stays deployed: it is the limit case's app.
2. With `ssc share`, grant `SSC_ISO_USER` all three environments; grant `SSC_ISO_OUTSIDER` none.
3. For cell 2, nothing needs deploying: the cases only knock on `alpha.<cell 2 base>`.

## The cases

"Fast" is the result of the pre-merge run on both browsers at the commit that added the suite.

| Case (spec) | What it proves | Fast | Row |
| --- | --- | --- | --- |
| A POST from app A to app B is refused before it reaches B (`cross-app`) | Fetch-Metadata: a same-site `fetch` and form `POST` from A get 403 and never reach B; B's own `POST` works | pass | login sessions |
| A script, an image or a frame on A cannot load from B (`cross-app`) | Fetch-Metadata: same-site subresources and frames are refused before B | pass | login sessions |
| From another site a form POST to B is refused and a link opens it (`cross-app`) | a cross-site `POST` is 403; a cross-site top-level `GET` navigation is allowed | pass | public entry and TLS |
| A link from A opens B (`cross-app`) | a same-site top-level `GET` navigation is allowed, signed in | pass | login sessions |
| A WebSocket from A to B is refused; from B it opens (`cross-app`) | the upgrade's `Origin` must be the app's own; the session cookie never reaches the app over a socket | pass | login sessions |
| `/.ssc/logout` from A does not sign the person out of B (`cross-app`) | logout from another origin (`fetch`, `<img>`, a link) is 404 and changes nothing; B's own logout works | pass | login sessions |
| Cookie tossing from A cannot replace B's session (`cookies`) | `__Host-`/`__host-`/`__Secure-` session cookies set by A's script or A's `Set-Cookie` for the parent domain never become B's session | pass | login sessions |
| Cookie tossing from A cannot sign a person in to B as someone else (`cookies`) | with no session on B, a tossed one does not open B | pass | login sessions |
| `document.cookie` cannot see the `__Host-` session cookie (`cookies`) | after a real login on two apps, neither page's script sees a platform cookie | pass | login sessions |
| An app's platform-named `Set-Cookie` never reaches the browser, and the session cookie never reaches the app (`cookies`) | Envoy strips both directions; the app's own cookies pass | pass | login sessions |
| Login round trip (`login`) | no session goes to the auth host with a 43-character binding; the callback sets `__Host-ssc-session` host-only, `Path=/; Secure; HttpOnly; SameSite=Lax`, no `Domain`, clears the login cookie; copied onto B it opens nothing | pass | login sessions |
| A used login code does not sign anyone in again (`login`) | replay by the same browser and by another one with its own login nonce is 400 | pass | login sessions |
| A login code taken before its owner uses it signs in nobody (`login`) | a thief with its own nonce gets 400 and no session; the code is then spent for the owner too | pass | login sessions |
| A person not granted A gets exactly what an address with no app gets (`login`) | same status, body and headers (less date and trace headers) | pass | login sessions |
| Server-sent events arrive as sent (`streams`) | events a second apart arrive a second apart through Envoy and the relay | pass | public entry and TLS |
| At the limit a WebSocket and an event stream are cut, the deadline was announced, and a page with the helpers reconnects (`streams`) | both cut within seconds of the limit; `X-SSC-Request-Deadline` matched it; `sscSocket` and `EventSource` reconnect without a lost or repeated count | pass (6 s limit); nightly at 60 minutes | public entry and TLS |
| A page load to a cold app gets the waking page, then the app (`wake`) | the 503 "Waking up" page, then the app at the same address by itself | pass | public entry and TLS |
| A script call to a cold app waits instead (`wake`) | no waking page for `fetch` | pass | public entry and TLS |
| A WebSocket upgrade to a cold app waits instead (`wake`) | no waking page for an upgrade | pass | public entry and TLS |
| With the authoriser down every request is 503 (`edge`) | fail closed: apps, an unknown host and the callback are 503 and nothing reaches an app | pass | public entry and TLS |
| With no current snapshot a signed-in request is 503 (`edge`) | fail closed; a request without a session still goes to sign in, and nothing reaches an app | pass | public entry and TLS |
| The gateway's `run.app` host, called from the internet, is refused (`edge`) | the service accepts only the load balancer | skip: Docker has no `run.app` address | public entry and TLS |
| A cell-1 session cookie opens nothing in cell 2 (`edge`) | sessions are per cell | skip: one cell in Docker | login sessions |
| A login code for a cell-1 host is refused by cell 2 (`edge`) | codes are bound to their host | skip: one cell in Docker | login sessions |

The cold app is the test app's `/cold/` path and `/cold-ws`, which answer after 3 seconds, longer
than the gateway's 2-second wake route, in both targets; a real cold start is not forced.

## Browser notes

- WebKit does not report `Set-Cookie` on a redirect, so the round trip's raw header checks run on
  Chromium; on WebKit the stored cookie's attributes are checked.
- WebKit does not report a redirect that crosses sites, so "sent to sign in" is checked by the
  browser's next request (the auth host's `/login` asked to return to the address), on both.
- WebKit reports every WebSocket close the server starts as code 1005, so `sscSocket` treats 1005 as
  the restart code 1012 (SSC-090 helper, fixed with this suite).
- Chromium blocks a refused cross-site script or image before reporting its status, so those cases
  check the app's log rather than the status.
- Until the apps domain is on the Public Suffix List, A and B are the same site; the cases treat
  them so.
