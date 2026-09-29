"""Printing. ``--json`` output goes to stdout with sorted keys; human text is plain columns.

A failure under ``--json`` prints ``{"error": {...}}`` to stdout so a script reads one stream.
Without ``--json`` it prints title, detail, request id and any fix line to stderr.
"""

import json
from collections.abc import Sequence

import typer
from pydantic import BaseModel

from ssc_cli.errors import CliError
from ssc_cli.shapes import ErrorResult


def print_json(model: BaseModel) -> None:
    typer.echo(json.dumps(model.model_dump(mode="json"), sort_keys=True, indent=2))


def print_error(error: CliError, json_mode: bool) -> None:
    if json_mode:
        print_json(ErrorResult(error=error.body))
        return
    body = error.body
    typer.echo(f"Error: {body.title}", err=True)
    if body.detail:
        typer.echo(body.detail, err=True)
    typer.echo(f"Code: {body.code}", err=True)
    if body.request_id:
        typer.echo(f"Request id: {body.request_id}", err=True)
    if error.fix:
        typer.echo(f"Fix: {error.fix}", err=True)


def say(text: str = "") -> None:
    typer.echo(text)


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Left-aligned columns separated by two spaces."""
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row, strict=True)]
    lines = [headers, *rows]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip()
        for line in lines
    )


def dash(value: str | None) -> str:
    return value if value else "-"
