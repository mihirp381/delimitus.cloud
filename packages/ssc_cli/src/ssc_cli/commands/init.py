"""``ssc init``."""

from pathlib import Path
from typing import Annotated

import typer
from typer.core import TyperCommand, TyperGroup

from ssc_cli.agentpack import write_agent_pack
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.errors import ExitCode
from ssc_cli.output import print_json, say
from ssc_cli.shapes import FileAction, InitResult

PathArg = Annotated[
    Path,
    typer.Argument(
        help="The app folder.", exists=True, file_okay=False, dir_okay=True, resolve_path=True
    ),
]


def init(
    ctx: typer.Context,
    path: PathArg = Path(),
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Replace the ssc block, the skill file and the ssc MCP server if they differ.",
        ),
    ] = False,
    json_mode: JsonOpt = False,
) -> None:
    """Write the agent pack: an AGENTS.md block, a Claude Code skill, MCP settings for Claude Code
    and Cursor, and a starter ssc.toml."""
    with handled(json_mode):
        mcp_url = f"{session(ctx).config().api_url}/mcp"
    written = write_agent_pack(path, command_lines(ctx), mcp_url, force=force)
    result = InitResult(
        path=str(path),
        files=[FileAction(path=w.path, action=w.action, note=w.note) for w in written],
    )
    if json_mode:
        print_json(result)
    else:
        for f in result.files:
            say(f"{f.action:9}  {f.path}" + (f"  ({f.note})" if f.note else ""))
    if any(w.failed for w in written):
        raise typer.Exit(int(ExitCode.FAILED))


def command_lines(ctx: typer.Context) -> list[str]:
    """One Markdown line per registered command, so the pack never names a missing one."""
    root = ctx.find_root().command
    lines: list[str] = []
    if not isinstance(root, TyperGroup):
        return lines
    for name, cmd in root.commands.items():
        if isinstance(cmd, TyperCommand):
            lines.append(_entry(f"ssc {name}", cmd))
        elif isinstance(cmd, TyperGroup):
            if cmd.invoke_without_command:
                lines.append(_entry(f"ssc {name}", cmd))
            for sub_name, sub in cmd.commands.items():
                if isinstance(sub, TyperCommand):
                    lines.append(_entry(f"ssc {name} {sub_name}", sub))
    lines.append("")
    lines.append("Run `ssc <command> --help` for each command's options.")
    return lines


def _entry(prefix: str, cmd: TyperCommand | TyperGroup) -> str:
    args = [
        p.human_readable_name.upper() if p.required else f"[{p.human_readable_name.upper()}]"
        for p in cmd.params
        if p.param_type_name == "argument"
    ]
    usage = " ".join([prefix, *args])
    return f"- `{usage}`: {cmd.get_short_help_str(limit=100)}"
