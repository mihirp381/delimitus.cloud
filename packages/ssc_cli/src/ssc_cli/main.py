"""The ``ssc`` command. Only commands that work against the live API are registered."""

from typing import Annotated

import typer

from ssc_cli import __version__
from ssc_cli.commands._common import session
from ssc_cli.commands.access import access_app
from ssc_cli.commands.apps import apps_app
from ssc_cli.commands.deploy import deploy
from ssc_cli.commands.doctor import doctor
from ssc_cli.commands.init import init
from ssc_cli.commands.lifecycle import disable, enable
from ssc_cli.commands.login import login, logout
from ssc_cli.commands.mcp import mcp
from ssc_cli.commands.promote import promote
from ssc_cli.commands.releases import releases
from ssc_cli.commands.rollback import rollback
from ssc_cli.commands.share import share, unshare
from ssc_cli.commands.status import status
from ssc_cli.commands.token import token_app
from ssc_cli.commands.whoami import whoami

app = typer.Typer(
    name="ssc",
    help="Small Software Cloud: check, deploy, share and inspect internal apps.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"ssc {__version__}")
        raise typer.Exit


@app.callback()
def root(
    ctx: typer.Context,
    api: Annotated[
        str | None,
        typer.Option(
            "--api",
            metavar="URL",
            help="API address. Default: SSC_API_URL, then the config file, then the public API.",
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Print the version."),
    ] = False,
) -> None:
    """Small Software Cloud: check, deploy, share and inspect internal apps."""
    if api is not None:
        session(ctx).api_override = api


app.command()(login)
app.command()(logout)
app.command()(whoami)
app.add_typer(token_app)
app.add_typer(apps_app)
app.command()(status)
app.command()(share)
app.command()(unshare)
app.command()(doctor)
app.command()(init)
app.command()(deploy)
app.command()(releases)
app.command()(rollback)
app.command()(mcp)
app.command()(promote)
app.command()(disable)
app.command()(enable)
app.add_typer(access_app)
