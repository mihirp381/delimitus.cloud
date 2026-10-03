"""``ssc doctor``."""

from pathlib import Path
from typing import Annotated

import typer

from ssc_cli.commands._common import JsonOpt
from ssc_cli.doctor import run_doctor
from ssc_cli.errors import ExitCode
from ssc_cli.output import print_json, say
from ssc_cli.shapes import DoctorResult

PathArg = Annotated[
    Path,
    typer.Argument(
        help="The app folder.", exists=True, file_okay=False, dir_okay=True, resolve_path=True
    ),
]


def doctor(path: PathArg = Path(), json_mode: JsonOpt = False) -> None:
    """Check a folder for the problems that most often stop an app from running on SSC."""
    findings = run_doctor(path)
    result = DoctorResult(
        path=str(path), blocking=any(f.severity == "block" for f in findings), findings=findings
    )
    if json_mode:
        print_json(result)
    elif not findings:
        say(f"No problems found in {path}.")
    else:
        for i, f in enumerate(findings):
            where = f"{f.path}:{f.line}" if f.line else f.path
            say(f"{f.severity.upper():5}  {f.code}  {where}")
            say(f"       {f.message}")
            if i + 1 == len(findings) or findings[i + 1].fix != f.fix:
                say(f"       Fix: {f.fix}")
        blocks = sum(f.severity == "block" for f in findings)
        notes = sum(f.severity == "info" for f in findings)
        warns = len(findings) - blocks - notes
        summary = f"{blocks} blocking, {warns} {'warning' if warns == 1 else 'warnings'}"
        if notes:
            summary += f", {notes} {'note' if notes == 1 else 'notes'}"
        say(f"\n{summary}.")
    if result.blocking:
        raise typer.Exit(int(ExitCode.BLOCKED))
