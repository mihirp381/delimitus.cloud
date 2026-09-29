"""Pieces every command shares."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Annotated

import typer

from ssc_cli.errors import CliError
from ssc_cli.output import print_error
from ssc_cli.session import Session

JsonOpt = Annotated[bool, typer.Option("--json", help="Print one JSON object on stdout.")]


def session(ctx: typer.Context) -> Session:
    return ctx.ensure_object(Session)


@contextmanager
def handled(json_mode: bool) -> Generator[None]:
    """Report a :class:`CliError` and exit with its code."""
    try:
        yield
    except CliError as e:
        print_error(e, json_mode)
        raise typer.Exit(int(e.exit_code)) from None
