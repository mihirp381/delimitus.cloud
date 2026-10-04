"""``ssc login --org`` and ``ssc logout`` (SSC-019, decision 024).

Login is the RFC 8628 device flow: ssc shows a link and a code, the person signs in with their
company's single sign-on in a browser and confirms the code, and ssc keeps the login in the
keychain. Logout revokes the login at the auth host, then forgets it.

``--agent NAME`` (SSC-048) signs in for a coding agent instead: the person confirms that agent by
name, every call made with the login is recorded as the agent's on their behalf, and the login is
kept apart from the person's own, for ``ssc mcp`` only.
"""

import os
import re
import time
import webbrowser
from typing import Annotated, Final

import typer

from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.credentials import ENV_TOKEN, Login, clear_token, read_login, store_login
from ssc_cli.errors import LOGIN_FAILED, NETWORK_ERROR, CliError, ExitCode, local_error
from ssc_cli.login import AuthClient, auth_url_for
from ssc_cli.output import print_json, say
from ssc_cli.shapes import LoginResult, LogoutResult

SLOW_DOWN: Final = 5
AGENT_NAME: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

OrgOpt = Annotated[
    str,
    typer.Option("--org", metavar="ORG_ID", help="Your org's id, org_ followed by 20 characters."),
]
AuthOpt = Annotated[
    str | None,
    typer.Option(
        "--auth-url",
        metavar="URL",
        help="Auth host. Default: SSC_AUTH_URL, then the API address with api. made auth.",
    ),
]
BrowserOpt = Annotated[
    bool, typer.Option("--no-browser", help="Show the link without opening a browser.")
]
AgentOpt = Annotated[
    str | None,
    typer.Option(
        "--agent",
        metavar="NAME",
        help="Sign in for this coding agent (claude-code, codex, cursor), for `ssc mcp`.",
    ),
]
AgentLogoutOpt = Annotated[bool, typer.Option("--agent", help="End the agent's login instead.")]


def login(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to an option)
    ctx: typer.Context,
    org: OrgOpt,
    auth_url: AuthOpt = None,
    no_browser: BrowserOpt = False,
    agent: AgentOpt = None,
    json_mode: JsonOpt = False,
) -> None:
    """Sign in with your company's single sign-on and keep the login in the keychain."""
    s = session(ctx)
    with handled(json_mode):
        _check_agent(agent)
        api_url = s.config().api_url
        auth = auth_url_for(api_url, auth_url)
        with AuthClient(auth, transport=s.transport) as client:
            start = client.start(org, agent)
            if start is None:
                raise _failed(f"{org} cannot sign in at {auth}. Check the org id.")
            code = f"{start.user_code[:4]}-{start.user_code[4:]}"
            typer.echo(f"Open {start.verification_uri_complete}", err=True)
            typer.echo(f"and check that it shows the code {code}.", err=True)
            interactive = typer.get_text_stream("stdin").isatty()
            if interactive and not no_browser:
                webbrowser.open(start.verification_uri_complete)
            interval = start.interval
            deadline = time.monotonic() + start.expires_in
            while True:
                s.sleep(interval)
                polled = client.poll(start.device_code)
                if not isinstance(polled, str):
                    tokens = polled
                    break
                if polled == "slow_down":
                    interval += SLOW_DOWN
                elif polled == "access_denied":
                    raise _failed("The sign-in was refused.")
                elif polled == "expired_token" or time.monotonic() > deadline:
                    raise _failed("The code expired before the sign-in finished. Run it again.")
        kept = Login.of(auth, org, tokens, time.time(), agent)
        with s.client(token=kept.access_token) as api:
            me = api.whoami()
        if me.is_agent != (agent is not None) or me.client_id != agent:
            raise _failed(f"{auth} did not issue the login that was asked for. Run it again.")
        store_login(api_url, kept)
        result = LoginResult(
            api_url=api_url,
            auth_url=auth,
            org_id=me.org_id,
            subject=me.subject,
            stored_in="keychain",
            agent=agent,
        )
    _report(result, json_mode)


def _check_agent(agent: str | None) -> None:
    if agent is not None and not AGENT_NAME.fullmatch(agent):
        raise _failed(
            f"{agent!r} is not an agent name: up to 64 lowercase letters, digits, '.', '_' or "
            "'-', such as claude-code."
        )


def _report(result: LoginResult, json_mode: bool) -> None:
    if result.agent is None and os.environ.get(ENV_TOKEN, "").strip():
        typer.echo(f"Note: {ENV_TOKEN} is set and is used instead of the login.", err=True)
    if json_mode:
        print_json(result)
    elif result.agent is not None:
        say(
            f"Signed in {result.agent} as {result.subject} in {result.org_id} "
            f"({result.api_url}). `ssc mcp` uses this login; your own is unchanged."
        )
    else:
        say(f"Signed in as {result.subject} in {result.org_id} ({result.api_url}).")


def logout(ctx: typer.Context, agent: AgentLogoutOpt = False, json_mode: JsonOpt = False) -> None:
    """End the login at the auth host and remove it from the keychain."""
    s = session(ctx)
    with handled(json_mode):
        api_url = s.config().api_url
        kept = read_login(api_url, agent=agent)
        revoked = False
        if kept is not None:
            try:
                with AuthClient(kept.auth_url, transport=s.transport) as client:
                    client.revoke(kept.refresh_token)
                revoked = True
            except CliError as e:
                if e.body.code != NETWORK_ERROR:
                    raise
                typer.echo(
                    "Warning: the auth host could not be reached; the login ends on its own "
                    "within 12 hours.",
                    err=True,
                )
        cleared = clear_token(api_url, agent=agent)
        result = LogoutResult(api_url=api_url, revoked=revoked, cleared=cleared, agent=agent)
    if json_mode:
        print_json(result)
    elif result.cleared:
        say(f"Signed {'the agent ' if agent else ''}out of {api_url}.")
    else:
        say(f"Nothing was kept for {api_url}.")


def _failed(detail: str) -> CliError:
    return local_error(LOGIN_FAILED, "The sign-in did not finish.", detail, ExitCode.AUTH)
