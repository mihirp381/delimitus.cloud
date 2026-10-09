# Remote MCP: connecting a coding agent with OAuth

How a person connects a remote MCP client (Claude Code, Codex, Cursor, any client that speaks the MCP authorization spec) to `https://api.delimitus.com/mcp`, and what to check when it does not work (decision 029). Nothing here hands anyone a token: the client registers itself, the person signs in with their work account in a browser, and the client gets a credential that only the agent interface takes.

## For the person connecting

```sh
claude mcp add --transport http ssc https://api.delimitus.com/mcp
```

In Codex:

```sh
codex mcp add ssc --url https://api.delimitus.com/mcp --oauth-resource https://api.delimitus.com/mcp
```

It opens the browser to sign in the same way. In an app folder, `ssc init` writes the Claude Code (`.mcp.json`) and Cursor (`.cursor/mcp.json`) settings for this server, at the CLI's API address plus `/mcp`; Claude Code asks to approve the project's `ssc` server the first time.

Then use the server once (in Claude Code, `/mcp` and pick `ssc`). The client:

1. reads `https://api.delimitus.com/.well-known/oauth-protected-resource/mcp`, which names `https://auth.delimitus.com` as the authorization server;
2. registers itself at `https://auth.delimitus.com/register` (no secret; a loopback redirect such as `http://127.0.0.1:<port>/callback`);
3. opens a browser at `/authorize`. If this browser already signed in to an app host in the last 12 hours, the org is known; otherwise the page asks for a **work email** and finds the company's sign-in from its domain;
4. sends you through your company's single sign-on;
5. shows a consent page naming the client, your org and where it sends you back. Approve only if you just connected it yourself.

The client then holds a 5-minute access token for `https://api.delimitus.com/mcp`, refreshed for up to 12 hours from sign-in. After that it opens the browser again. Every call is recorded as the agent's (its `client_id` is the client's name, made into an agent name, else `mcp-client`) on your behalf, and every agent rule applies: preview only for deploys, no approvals, no secret values.

Credentials do not cross over:

- Remote `/mcp` refuses a token from `ssc login` or `ssc login --agent`; those are for `/v1` and for the local `ssc mcp`.
- `/v1` refuses the token an MCP client got here, whoever sends it. The agent interface reaches `/v1` only inside the API.
- The local server, `ssc mcp` over stdio with the `ssc login --agent <name>` credential, is unchanged.

## When it does not work

| What the person sees | Why | What to do |
|---|---|---|
| "We couldn't find a sign-in for that email" | One page for every miss, so it says nothing about which orgs exist. The domain is not a **verified** domain of a WorkOS organisation; or that organisation is not connected to an SSC org; or the org's directory connection is frozen. | Operator: in the WorkOS dashboard check the organisation's domains are verified (`legacy_verified` also counts). Check the org's `directory_connection` is `active` (`docs/runbooks/ssc-019-login.md`). |
| "Too many tries" (429) | Twenty work-email tries an hour from one address, or for one domain, per auth-host instance. | Wait an hour. Counted per instance, so the real bound is twenty times the instance count. |
| "Sign-in is unavailable" (503) | WorkOS did not answer the domain lookup. | Retry; check WorkOS status. The log has `WorkOS organisation lookup failed`. |
| "This sign-in request is not valid" (400) | The client id is unknown (clients unused for 30 days are deleted daily) or the redirect URI is not one the client registered. Never redirected, so a wrong redirect cannot leak a code. | Remove and add the server again so the client registers afresh. |
| The client reports `invalid_target` | It asked for a resource other than `https://api.delimitus.com/mcp`. | Check the URL given to `claude mcp add`: exactly `https://api.delimitus.com/mcp`. |
| The client reports `invalid_grant` on `/token` | The code was over a minute old, used already, or sent with another verifier, client or redirect. A code presented twice also ends the session it opened (`auth.code_reused` in the audit log, a `WARNING` in the auth host log). | Connect again. Repeated `auth.code_reused` for one client means its codes are being intercepted: tell the person. |
| `401` from `/mcp` with a token that works on `/v1` | Expected: that is a `/v1` credential (see above). | Use the OAuth flow, or `ssc mcp` locally. |

## For an operator

- Settings on the auth host (infra `control.py`, `auth_env`): `SSC_MCP_RESOURCE=https://api.delimitus.com/mcp`, `SSC_CONSOLE_URL=https://console.delimitus.com`, `SSC_AUTH_TRUSTED_HOPS=2` (the control load balancer and its front end append to `X-Forwarded-For`; the client is the second entry from the right). The API derives its resource from `SSC_API_PUBLIC_URL` plus `/mcp`; set `SSC_MCP_RESOURCE` on the API only if the two must differ, and then to the same value as the auth host's.
- Database: revision `0033_oauth` (`ssc.oauth_client`, `ssc.oauth_code`, the `console` session kind, `code_reuse`, `ssc.org_for_workos_organization`).
- Audit (the org's chain): `auth.authorize_approved` and `auth.authorize_denied` (target `oauth_client`, with the client's name and redirect host), `token.issued` with `via: authorization_code`, `auth.code_reused`. A registration is not in any org's chain (no org yet); the auth host logs `OAuth client <id> registered` at `INFO`.
- Ending an agent's access: deactivating the person in the directory revokes every session, MCP ones included. A client that calls `/revoke` with its refresh token ends its own session. There is no admin screen listing MCP sessions yet.
- The worker deletes clients unused for 30 days (`identity:oauth_client_prune`, daily at 03:17 UTC).

## Live check (after deploy)

1. `curl -sS https://auth.delimitus.com/.well-known/oauth-authorization-server` names `/authorize`, `/token`, `/register`, `/revoke`, `S256` only.
2. `curl -sS https://api.delimitus.com/.well-known/oauth-protected-resource/mcp` names `https://api.delimitus.com/mcp` and `https://auth.delimitus.com`.
3. From a clean browser profile, `claude mcp add --transport http ssc https://api.delimitus.com/mcp`, connect, type a work email of a connected org, sign in, approve. Claude Code lists the SSC tools; `list_apps` answers.
4. The org's audit log has `auth.authorize_approved` naming the client and `token.issued` with `via: authorization_code`.
5. An email at an unknown domain and one at a connected org's unverified domain give the same page.
6. `curl -sS -H "Authorization: Bearer <the ssc login --agent token>" -X POST https://api.delimitus.com/mcp` is `401` with a `WWW-Authenticate` naming the metadata.
