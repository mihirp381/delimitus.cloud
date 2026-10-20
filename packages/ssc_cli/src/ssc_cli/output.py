"""Printing. ``--json`` output goes to stdout with sorted keys; human text is plain columns.

A failure under ``--json`` prints ``{"error": {...}}`` to stdout so a script reads one stream.
Without ``--json`` it prints title, detail, request id, any log tail and any fix line to stderr.
"""

import json
import re
from collections.abc import Sequence
from typing import Final

import typer
from pydantic import BaseModel

from ssc_cli.errors import CliError
from ssc_cli.shapes import ErrorResult

_CONTROL: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def visible(text: str) -> str:
    """App-written text as a terminal may show it: every C0 and C1 control but tab and newline,
    ESC and carriage return included, written as its escape (``\\x1b``), so a log line cannot
    move the cursor, recolour, retitle the window or write the clipboard. A newline stays one, so
    a traceback still reads as lines."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def print_json(model: BaseModel) -> None:
    typer.echo(json.dumps(model.model_dump(mode="json"), sort_keys=True, indent=2))


def print_error(error: CliError, json_mode: bool) -> None:
    if json_mode:
        shown = {"fix": error.fix, "log_tail": error.log_tail or None}
        print_json(ErrorResult(error=error.body.model_copy(update=shown)))
        return
    # The title, detail and request id may be the server's words: shown escaped (B-5).
    body = error.body
    typer.echo(f"Error: {visible(body.title)}", err=True)
    if body.detail:
        typer.echo(visible(body.detail), err=True)
    typer.echo(f"Code: {visible(body.code)}", err=True)
    if body.request_id:
        typer.echo(f"Request id: {visible(body.request_id)}", err=True)
    if error.log_tail:
        typer.echo("Last log lines:", err=True)
        for line in error.log_tail:
            typer.echo(f"  {visible(line)}", err=True)
    if error.fix:
        typer.echo(f"Fix: {visible(error.fix)}", err=True)


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
