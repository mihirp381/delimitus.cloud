"""``ssc token set`` and ``ssc token clear``: the stopgap until ``ssc login`` (SSC-019).

The token is read from stdin, never from the command line, so it stays out of shell history
and process listings. It is checked against the API before it is kept.
"""

import os
from typing import Final

import typer

from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.credentials import ENV_TOKEN, clear_token, store_token
from ssc_cli.errors import BAD_TOKEN_INPUT, CliError, ExitCode, local_error
from ssc_cli.output import print_json, say
from ssc_cli.shapes import TokenClearResult, TokenSetResult

MAX_TOKEN_CHARS: Final = 16 * 1024

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
