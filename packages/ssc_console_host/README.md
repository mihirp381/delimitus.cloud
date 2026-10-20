# Console host

`python -m ssc_console_host` serves the built admin console at `console.delimitus.com`
(decision 031), read-only and from memory. The load balancer sends `/v1` on that host to the
API; this server answers everything else:

- `/healthz`: `ok`.
- `/v1`, `/mcp` and anything under them, and any path with a `.` or `..` segment, a backslash or
  a NUL: 404.
- A file of the build: that file. `/assets/*` are content-hashed and kept a year; a missing asset
  is a 404.
- Any other missing path whose last segment has a dot (`/robots.txt`, `/x.map`): 404.
- Any other path: `index.html` with `no-store`, for the router.

Every answer carries a content security policy that allows only this origin and the auth host.
It imports no platform code (import-linter enforces it).

## Settings

| Variable | Default | What |
|---|---|---|
| `SSC_CONSOLE_AUTH_ORIGIN` | required | The auth host's origin, `https://auth.delimitus.com`, named in the policy's `connect-src` and `form-action`. Scheme and host only; `https` outside dev and test. |
| `SSC_CONSOLE_DIST` | required; `/app/dist` in the image | The console's `dist/` folder. |
| `SSC_CONSOLE_ENV` | `prod` | `dev` or `test` allow an `http` auth origin. |
| `PORT` | `8080` | The port to listen on. |

## Build arguments

The image builds the console from the repository root with
`docker build -f packages/ssc_console_host/Dockerfile .`; `Dockerfile.dockerignore` limits what
is sent and leaves out `console/.env` and `console/.env.*`. The runtime stage holds only the
installed environment and `dist/`. Its build arguments are the console's settings
([console guide](../../console/README.md#settings)):

| Variable | Default | What |
|---|---|---|
| `VITE_SSC_AUTH_URL` | `https://auth.delimitus.com` | The auth host's origin. |
| `VITE_SSC_API_AUDIENCE` | `https://api.delimitus.com` | The API's user audience. |
| `VITE_SSC_SUPPORT_EMAIL` | empty | The support address on the Help page. |
| `VITE_SSC_TRUST_URL` | empty | The trust pack's `https` address on the Help page. |

`VITE_SSC_DEV_LOGIN` is never passed; `npm run build` fails if the build holds the dev login or
a local address.
