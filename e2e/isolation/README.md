# Browser isolation suite (SSC-029)

Playwright on Chromium and WebKit, proving that two apps in one cell cannot attack each other
through the browser, and that the gateway (SSC-018) behaves as its done-when says. It tests the
gateway, not the console, so it lives here rather than in `console/e2e`; it uses the console's
Playwright, TypeScript and Node versions and the same `run.mjs` pattern.

It covers three rows of the isolation matrix: **public entry and TLS**, **login sessions** and, on a
night with two cells, **cross-cell sessions**; the nightly page (`ssc_conformance.matrix`) reads its
report.

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
  login codes and answers `/internal/redeem` (the gateway redeems through its real `HttpRedeemer`)
  and, for the nightly sign-in, stands in for WorkOS and Okta (the device flow, and a two-step
  sign-in form with Okta Identity Engine's field names), a CONNECT proxy that resolves the cell's names for the browsers, and a cut of every stream at its
  environment's limit, as Cloud Run ends a request. Bravo's preview has a limit of 6 seconds
  (`SSC_ISO_LIMIT_SECONDS` changes it), so the limit case takes seconds;
- switches the fail-closed cases use: stop the authorisation service as Envoy sees it, and make the
  snapshot too old to use.

A pass here is not a pass of SSC-029: the ticket's done-when is the live run against a staging cell.

### Live: a staging cell (nightly, `.github/workflows/isolation-nightly.yml`)

```sh
SSC_ISO_CELL1_BASE=... SSC_ISO_AUTH_URL=https://... SSC_ISO_AUTH_STATE=... npm run live
```

There are no sealed sessions live. The night signs in as the cell's test admin through the real
auth host, WorkOS and the org's Okta, in a browser (`night-login.ts`, below), and the cases that
need a person use that sign-in. The cases that need a second person skip with `needs a second test
user`, which the nightly page lists as such; the fast run still runs them.

| Setting | Kind | What |
| --- | --- | --- |
| `SSC_ISO_CELL1_BASE` | variable, required | the cell's app base host, e.g. `<label>.<apps domain>`; apps are `alpha.<base>`, `bravo.<base>`, `bravo--preview.<base>` |
| `SSC_ISO_AUTH_URL` | variable, required | the auth host's origin, `https://...` |
| `SSC_ISO_AUTH_STATE` | path, required for the cases that need a person | the file `night-login.ts` writes: a Playwright storage state of the auth host's cookies, and the user id the tokens name. It is a secret: never uploaded, deleted when the job ends |
| `SSC_ISO_USER` | variable, optional | the test admin's user id; the suite reads it from `SSC_ISO_AUTH_STATE` when unset |
| `SSC_ISO_PEER_BASE` | variable, optional | the other cell's app base host; the two-cell cases skip with `no peer cell` without it |
| `SSC_ISO_GATEWAY_RUN_APP` | variable, optional | the cell's gateway service `run.app` host; its case skips without it |
| `SSC_ISO_LIMIT_SECONDS` | variable, optional | bravo preview's request limit; default 3600 |
| `SSC_ISO_NIGHTLY=1` | set by the workflow | runs the 60-minute limit case and two workers |
| `PLAYWRIGHT_JSON_OUTPUT_NAME` | path, optional | where the JSON report goes, beside the console list; the nightly page reads it |

`SSC_ISO_OTHER_USER` and `SSC_ISO_OUTSIDER` are used by the fast run only (the rig sets them).

### The nightly sign-in: `node night-login.ts`

Signs in as the cell's test admin, once per night and cell, and writes the files the later steps use.
The admin is one Okta test user per cell org, with no MFA; its password is a repository secret.

1. The device flow, as `ssc login` does: the code is approved in the browser through the real auth
   host, WorkOS and Okta, and the tokens are polled for.
2. A browser sign-in on each host of `SSC_NIGHT_HOSTS`, which leaves a session at the auth host.

| Setting | Kind | What |
| --- | --- | --- |
| `SSC_NIGHT_AUTH_URL` | required | the auth host's origin |
| `SSC_NIGHT_ORG` | required | the org the cell serves, `org_...` |
| `SSC_NIGHT_USERNAME` | required | the test admin's Okta user name |
| `SSC_NIGHT_PASSWORD` | secret, required | its password: step-level only, never in a file or a log |
| `SSC_NIGHT_HOSTS` | required | comma-separated app hosts to sign in on; the last is the drill's host when the drill's file is asked for |
| `SSC_NIGHT_OKTA` | optional | `classic` answers Okta's classic sign-in page (`#okta-signin-username` and so on); unset answers Identity Engine's (`input[name="identifier"]`, then `input[name="credentials.passcode"]`) |
| `SSC_ISO_AUTH_STATE` | required | where to write the storage state, mode 0600: the auth host's cookies only |
| `SSC_DRILL_CREDENTIALS_FILE` | optional | where to write the drill's file, mode 0600: the access and refresh tokens and the drill host's session cookie, as `ssc_conformance.kill_drill` reads them |

It is tested offline against the rig (`signin.spec.ts`); the identity provider's pages are
the one part only a night proves, which is why `SSC_NIGHT_OKTA` exists.

The nightly job is inert until `SSC_NIGHT_CELLS` is set as a repository variable, and runs only on
`main`. It uses no cloud credentials.

In the staging cell, before the first run:

1. Copy the reconnect helper next to the test app and deploy it twice from `apps/` (it is a session
   app, one instance, so its in-memory log sees every request):
   ```sh
   mkdir -p apps/ssc-reconnect && cp ../../helpers/node/ssc-reconnect/{index.js,browser.js} apps/ssc-reconnect/
   ssc apps create alpha && ssc deploy apps --app alpha --wait && ssc promote alpha
   ssc apps create bravo && ssc deploy apps --app bravo --wait && ssc promote bravo
   ```
   Bravo's preview stays deployed: it is the limit case's app.
2. With `ssc share`, grant the test admin all three environments.
3. For the peer cell, nothing needs deploying: the cases only knock on `alpha.<peer base>`.

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
| A cell-1 session cookie opens nothing in cell 2 (`edge`) | sessions are per cell | skip: one cell in Docker | cross-cell sessions |
| A login code for a cell-1 host is refused by cell 2 (`edge`) | codes are bound to their host | skip: one cell in Docker | cross-cell sessions |
| The device code is approved through the identity provider and its tokens name the person; a wrong password signs in nobody; the identity session spares the next host; the classic Okta page is answered; `night-login.ts` writes private files (`signin`) | the nightly sign-in, against the rig's stand-in | pass (Chromium for the script) | none: rig only |

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
