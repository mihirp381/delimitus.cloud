"""``ssc login --org`` and ``ssc logout`` (SSC-019, decision 024).

Login is the RFC 8628 device flow: ssc shows a link and a code, the person signs in with their
company's single sign-on in a browser and confirms the code, and ssc keeps the login in the
keychain. Logout revokes the login at the auth host, then forgets it.
"""

import os
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


def login(
    ctx: typer.Context,
    org: OrgOpt,
    auth_url: AuthOpt = None,
    no_browser: BrowserOpt = False,
    json_mode: JsonOpt = False,
) -> None:
    """Sign in with your company's single sign-on and keep the login in the keychain."""
    s = session(ctx)
    with handled(json_mode):
        api_url = s.config().api_url
        auth = auth_url_for(api_url, auth_url)
        with AuthClient(auth, transport=s.transport) as client:
            start = client.start(org)
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
        kept = Login.of(auth, org, tokens, time.time())
        with s.client(token=kept.access_token) as api:
            me = api.whoami()
        store_login(api_url, kept)
        result = LoginResult(
            api_url=api_url,
            auth_url=auth,
            org_id=me.org_id,
            subject=me.subject,
            stored_in="keychain",
        )
    if os.environ.get(ENV_TOKEN, "").strip():
        typer.echo(f"Note: {ENV_TOKEN} is set and is used instead of the login.", err=True)
    if json_mode:
        print_json(result)
    else:
        say(f"Signed in as {result.subject} in {result.org_id} ({result.api_url}).")


def logout(ctx: typer.Context, json_mode: JsonOpt = False) -> None:
    """End the login at the auth host and remove it from the keychain."""
    s = session(ctx)
    with handled(json_mode):
        api_url = s.config().api_url
        kept = read_login(api_url)
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
        result = LogoutResult(api_url=api_url, revoked=revoked, cleared=clear_token(api_url))
    if json_mode:
        print_json(result)
    elif result.cleared:
        say(f"Signed out of {api_url}.")
    else:
        say(f"Nothing was kept for {api_url}.")


def _failed(detail: str) -> CliError:
    return local_error(LOGIN_FAILED, "The sign-in did not finish.", detail, ExitCode.AUTH)
