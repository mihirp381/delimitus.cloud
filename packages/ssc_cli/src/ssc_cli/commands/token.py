"""``ssc token set`` and ``ssc token clear``: keep a token issued another way (an agent's, or a
break-glass one); people sign in with ``ssc login`` (SSC-019).

The token is read from stdin, never from the command line, so it stays out of shell history
and process listings. It is checked against the API before it is kept.

``ssc token create-ci``, ``list-ci`` and ``revoke-ci`` (GA-7.7): a CI token is a ``preview``-scoped
token for a repository secret (the ``ssc-deploy`` Action), made at the auth host from the
person's own login, up to 90 days. It is printed once and never kept.
"""

import os
from datetime import UTC, datetime
from typing import Annotated, Final

import typer

from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.credentials import ENV_TOKEN, bearer, clear_token, store_token
from ssc_cli.errors import BAD_TOKEN_INPUT, CI_TOKEN_REFUSED, CliError, ExitCode, local_error
from ssc_cli.login import AuthClient, auth_url_for
from ssc_cli.output import print_json, say, table
from ssc_cli.shapes import (
    CiTokenCreated,
    CiTokenRevoked,
    CiTokenRow,
    CiTokensResult,
    TokenClearResult,
    TokenSetResult,
)

MAX_TOKEN_CHARS: Final = 16 * 1024
CI_MAX_DAYS: Final = 90

LabelOpt = Annotated[
    str,
    typer.Option("--label", metavar="TEXT", help="What it is for, such as the repository name."),
]
DaysOpt = Annotated[
    int,
    typer.Option("--days", min=1, max=CI_MAX_DAYS, help="Days until it ends, 1 to 90."),
]
AuthOpt = Annotated[
    str | None,
    typer.Option(
        "--auth-url",
        metavar="URL",
        help="Auth host. Default: SSC_AUTH_URL, then the API address with api. made auth.",
    ),
]

token_app = typer.Typer(
    name="token",
    help="Keep the API token in the OS keychain.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


@token_app.command("set")
def set_token(ctx: typer.Context, json_mode: JsonOpt = False) -> None:
    """Read a token from stdin, check it with the API, and keep it in the keychain."""
    with handled(json_mode):
        token = _read_token_input()
        with session(ctx).client(token=token) as client:
            me = client.whoami()
            store_token(client.api_url, token)
            result = TokenSetResult(
                api_url=client.api_url, stored_in="keychain", org_id=me.org_id, subject=me.subject
            )
    if os.environ.get(ENV_TOKEN, "").strip():
        typer.echo(f"Note: {ENV_TOKEN} is set and is used instead of the keychain.", err=True)
    if json_mode:
        print_json(result)
    else:
        say(f"Saved the token for {result.subject} in {result.org_id} ({result.api_url}).")


@token_app.command("clear")
def clear(ctx: typer.Context, json_mode: JsonOpt = False) -> None:
    """Remove the stored token for this API."""
    with handled(json_mode):
        api_url = session(ctx).config().api_url
        result = TokenClearResult(api_url=api_url, cleared=clear_token(api_url))
    if json_mode:
        print_json(result)
    elif result.cleared:
        say(f"Removed the token for {api_url}.")
    else:
        say(f"No token was stored for {api_url}.")


@token_app.command("create-ci")
def create_ci(
    ctx: typer.Context,
    label: LabelOpt,
    days: DaysOpt = CI_MAX_DAYS,
    auth_url: AuthOpt = None,
    json_mode: JsonOpt = False,
) -> None:
    """Create a preview-only CI token for a repository secret; it is shown once."""
    s = session(ctx)
    with handled(json_mode):
        api_url = s.config().api_url
        found = bearer(api_url, transport=s.transport)
        access = found if isinstance(found, str) else found()
        with s.client(token=access) as client:
            me = client.whoami()
        if me.is_agent:
            raise local_error(
                CI_TOKEN_REFUSED,
                "This login cannot create CI tokens.",
                "It is an agent's. Run it with your own `ssc login`.",
                ExitCode.AUTH,
            )
        auth = auth_url_for(api_url, auth_url)
        with AuthClient(auth, transport=s.transport, sleep=s.sleep) as host:
            issued = host.create_ci_token(access, label, days)
        result = CiTokenCreated(
            api_url=api_url,
            auth_url=auth,
            id=issued.id,
            label=issued.label,
            expires_at=issued.expires_at,
            token=issued.token,
        )
    if json_mode:
        print_json(result)
        return
    say(result.token)
    typer.echo(
        f"This token is shown once. Store it as a repository secret (for example "
        f"SSC_PREVIEW_TOKEN). It can deploy preview of any app you can build and never touches "
        f"prod. It ends {result.expires_at}; revoke it sooner with "
        f"`ssc token revoke-ci {result.id}`.",
        err=True,
    )


def _state(row: CiTokenRow, now: datetime) -> str:
    if row.revoked_at is not None:
        return "revoked"
    expires = datetime.fromisoformat(row.expires_at)
    return "expired" if expires <= now else "live"


@token_app.command("list-ci")
def list_ci(ctx: typer.Context, json_mode: JsonOpt = False) -> None:
    """List CI tokens: yours, or the whole org's for an org admin."""
    with handled(json_mode), session(ctx).client() as client:
        listed = client.list_ci_tokens()
        result = CiTokensResult(
            api_url=client.api_url,
            ci_tokens=[
                CiTokenRow(
                    id=t.id,
                    user_id=t.user_id,
                    label=t.label,
                    created_at=t.created_at,
                    expires_at=t.expires_at,
                    revoked_at=t.revoked_at,
                )
                for t in listed.ci_tokens
            ],
        )
    if json_mode:
        print_json(result)
        return
    if not result.ci_tokens:
        say("No CI tokens. Create one with `ssc token create-ci --label TEXT`.")
        return
    now = datetime.now(UTC)
    say(
        table(
            ("ID", "LABEL", "OWNER", "CREATED", "EXPIRES", "STATE"),
            [
                (t.id, t.label, t.user_id, t.created_at, t.expires_at, _state(t, now))
                for t in result.ci_tokens
            ],
        )
    )


@token_app.command("revoke-ci")
def revoke_ci(
    ctx: typer.Context,
    ci_token_id: Annotated[str, typer.Argument(metavar="ID", help="From `ssc token list-ci`.")],
    json_mode: JsonOpt = False,
) -> None:
    """Revoke a CI token; its next call is refused."""
    with handled(json_mode), session(ctx).client() as client:
        done = client.revoke_ci_token(ci_token_id)
        result = CiTokenRevoked(
            api_url=client.api_url, id=done.id, revoked_at=done.revoked_at or ""
        )
    if json_mode:
        print_json(result)
    else:
        say(f"Revoked {result.id}. Its next call is refused.")


def _read_token_input() -> str:
    stdin = typer.get_text_stream("stdin")
    if stdin.isatty():
        raw = typer.prompt("API token", hide_input=True, default="", show_default=False)
    else:
        raw = stdin.read(MAX_TOKEN_CHARS + 2)
    token = raw.strip()
    if not token or len(token) > MAX_TOKEN_CHARS:
        raise _bad_input("Pipe exactly one token into `ssc token set`.")
    if any(c.isspace() for c in token) or not (token.isascii() and token.isprintable()):
        raise _bad_input("A token is one line of printable characters with no spaces.")
    return token


def _bad_input(detail: str) -> CliError:
    return local_error(BAD_TOKEN_INPUT, "That is not an API token.", detail, ExitCode.USAGE)
