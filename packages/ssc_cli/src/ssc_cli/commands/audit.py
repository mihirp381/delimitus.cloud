"""``ssc audit``: download the org's audit log, and check an export's hash chain offline
(SSC-012, decision 012, GA-3.5).

``export`` is for org admins in their own login; an agent's credential is refused, and the export
is itself recorded as ``audit.exported``. ``verify`` needs no login: it recomputes every hash in a
JSON-lines export (:mod:`ssc_cli.audit_chain`), so anyone holding the file can check it.
"""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final

import typer

from ssc_cli.audit_chain import ChainReport, check_lines
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.errors import FILE_EXISTS, ExitCode, local_error
from ssc_cli.output import print_json, say
from ssc_cli.shapes import AuditExportResult, AuditVerifyResult

CAUSES: Final = {
    "unreadable": "the line is not an audit row with hex hashes and base64 canonical bytes",
    "missing": "an event is missing before this line",
    "prev_link": "its prev_hash is not the previous event's hash",
    "hash": "sha256(prev_hash || canonical) is not its hash",
    "fields": "its canonical bytes do not say what the row says",
}

audit_app = typer.Typer(
    name="audit",
    help="The org's audit log: export it, or check an export's hash chain.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


class Format(StrEnum):
    jsonl = "jsonl"
    csv = "csv"


@audit_app.command("export")
def export(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to an option)
    ctx: typer.Context,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Where to write it. Default: audit-<org>.<format> here."),
    ] = None,
    fmt: Annotated[
        Format, typer.Option("--format", help="jsonl can be checked with `ssc audit verify`.")
    ] = Format.jsonl,
    since: Annotated[
        str | None, typer.Option("--since", help="Inclusive, RFC 3339, like 2026-10-01T00:00:00Z.")
    ] = None,
    until: Annotated[str | None, typer.Option("--until", help="Exclusive, RFC 3339.")] = None,
    json_mode: JsonOpt = False,
) -> None:
    """Download the org's audit events, oldest first. Org admins only."""
    with handled(json_mode):
        if out is not None and out.exists():
            raise local_error(
                FILE_EXISTS, "The file already exists.", f"{out} exists; give another --out."
            )
        with session(ctx).client() as client:
            body, suggested = client.export_audit(fmt.value, since=since, until=until)
            api_url = client.api_url
        path = out or Path(suggested)
        try:
            with path.open("xb") as f:
                f.write(body)
        except FileExistsError:
            raise local_error(
                FILE_EXISTS, "The file already exists.", f"{path} exists; give --out."
            ) from None
    rows = body.count(b"\n") - (1 if fmt is Format.csv else 0)
    result = AuditExportResult(
        api_url=api_url, format=fmt.value, path=str(path), events=max(rows, 0), bytes=len(body)
    )
    if json_mode:
        print_json(result)
        return
    say(f"Wrote {result.events} events to {path}.")
    if fmt is Format.jsonl:
        say(f"Check its hash chain with `ssc audit verify {path}`.")


def _result(path: Path, report: ChainReport) -> AuditVerifyResult:
    return AuditVerifyResult(
        path=str(path),
        ok=report.ok,
        checked=report.checked,
        org_id=report.org_id,
        first_seq=report.first_seq,
        last_seq=report.last_seq,
        last_hash=report.last_hash,
        from_genesis=report.from_genesis,
        broken_line=report.broken_line,
        broken_seq=report.broken_seq,
        cause=report.cause,
    )


@audit_app.command("verify")
def verify(
    file: Annotated[
        Path,
        typer.Argument(
            metavar="FILE",
            help="A JSON-lines export.",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    json_mode: JsonOpt = False,
) -> None:
    """Recompute every hash in a JSON-lines export and name the first broken link. No login."""
    with file.open(encoding="utf-8", errors="replace") as lines:
        result = _result(file, check_lines(lines))
    if json_mode:
        print_json(result)
    elif result.ok and result.checked == 0:
        say(f"{file} has no events.")
    elif result.ok:
        span = f"seq {result.first_seq} to {result.last_seq}"
        say(f"Chain intact: {result.checked} events of {result.org_id}, {span}.")
        say(f"Last hash: {result.last_hash}")
        if not result.from_genesis:
            say(
                f"The file starts at seq {result.first_seq}, so the links before it were not "
                "checked. Export with no --since or --until to check the whole chain."
            )
    if not result.ok:
        if not json_mode:
            say(
                f"Chain broken at line {result.broken_line}"
                + ("" if result.broken_seq is None else f" (seq {result.broken_seq})")
                + f": {CAUSES[result.cause or 'unreadable']}."
            )
            say(f"The {result.checked} events before it check out.")
        raise typer.Exit(int(ExitCode.FAILED))
