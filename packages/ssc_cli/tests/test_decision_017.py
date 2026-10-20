"""Decision 017 lists exactly the commands ``ssc`` registers and the tools ``ssc mcp`` serves."""

import re
from pathlib import Path

from typer.core import TyperGroup
from typer.main import get_command

from ssc_cli.main import app
from ssc_cli.mcp_local import TOOLS

DECISIONS = Path(__file__).resolve().parents[3] / "docs" / "decisions" / "README.md"
SECTION = DECISIONS.read_text(encoding="utf-8").split("## 017 ", 1)[1].split("\n## ", 1)[0]


def registered() -> set[str]:
    root = get_command(app)
    assert isinstance(root, TyperGroup)
    out: set[str] = set()
    for name, cmd in root.commands.items():
        if not isinstance(cmd, TyperGroup) or cmd.invoke_without_command:
            out.add(name)
        if isinstance(cmd, TyperGroup):
            out |= {f"{name} {sub}" for sub in cmd.commands}
    return out


def _bullet(start: str) -> str:
    return next(line for line in SECTION.splitlines() if line.startswith(start))


def test_the_command_list_is_the_registered_set() -> None:
    listed = _bullet("- Commands registered now").split(":", 1)[1].split(".", 1)[0]
    assert set(re.findall(r"`([a-z -]+)`", listed)) == registered()


def test_the_local_tool_list_is_the_served_set() -> None:
    listed = _bullet("- `mcp [--json]`").split("result shapes (", 1)[1].split(")", 1)[0]
    assert re.findall(r"`([a-z_]+)`", listed) == list(TOOLS)
