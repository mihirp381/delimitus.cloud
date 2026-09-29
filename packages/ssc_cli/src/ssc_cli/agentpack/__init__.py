"""``ssc init``: write the agent pack that teaches coding agents how to build for SSC.

Files ssc shares with the person (``AGENTS.md``, ``CLAUDE.md``) get a marked block, and text
outside the markers is never touched. The skill file belongs to ssc. The starter ``ssc.toml`` and
``.sscignore`` are written only when absent.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from ssc_cli.agentpack.content import (
    BEGIN,
    CLAUDE_IMPORT,
    END,
    ENV_NAMES,
    GUIDE,
    SKILL_FRONTMATTER,
    STARTER_IGNORE,
    STARTER_MANIFEST,
)

Action = Literal["created", "updated", "unchanged", "skipped"]

AGENTS: Final = "AGENTS.md"
CLAUDE: Final = "CLAUDE.md"
SKILL: Final = ".claude/skills/ssc/SKILL.md"
MANIFEST: Final = "ssc.toml"
IGNORE: Final = ".sscignore"
_BLOCK = re.compile(r"<!-- ssc:begin v\d+ -->.*?<!-- ssc:end -->\n?", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Written:
    path: str
    action: Action
    note: str | None = None
    failed: bool = False


def guide(command_lines: list[str]) -> str:
    return GUIDE.format(commands="\n".join(command_lines), **ENV_NAMES)


def write_agent_pack(root: Path, command_lines: list[str], *, force: bool) -> list[Written]:
    text = guide(command_lines)
    steps: list[tuple[str, Callable[[], Written]]] = [
        (AGENTS, lambda: _block(root / AGENTS, AGENTS, text, force=force, create=True)),
        (SKILL, lambda: _owned(root / SKILL, SKILL, f"{SKILL_FRONTMATTER}\n{text}", force=force)),
        (CLAUDE, lambda: _block(root / CLAUDE, CLAUDE, CLAUDE_IMPORT, force=force, create=False)),
        (MANIFEST, lambda: _starter(root / MANIFEST, MANIFEST, STARTER_MANIFEST)),
        (IGNORE, lambda: _starter(root / IGNORE, IGNORE, STARTER_IGNORE)),
    ]
    out: list[Written] = []
    for rel, step in steps:
        try:
            written = step()
        except (OSError, UnicodeDecodeError) as e:
            written = Written(rel, "skipped", f"cannot update: {e}", failed=True)
        if written.action != "skipped" or written.note is not None:
            out.append(written)
    return out


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _block(path: Path, rel: str, body: str, *, force: bool, create: bool) -> Written:
    block = f"{BEGIN}\n{body}{END}\n"
    if not path.exists():
        if not create:
            return Written(rel, "skipped")
        _write(path, block)
        return Written(rel, "created")
    current = _read(path)
    action, note, text = _merge(current, block, force=force)
    if text is not None:
        _write(path, text)
    return Written(rel, action, note)


def _merge(current: str, block: str, *, force: bool) -> tuple[Action, str | None, str | None]:
    """What to do with an existing file: the action, a note, and the new text if it changes."""
    found = list(_BLOCK.finditer(current))
    marks = (current.count("<!-- ssc:begin "), current.count(END))
    if len(found) > 1 or marks != (len(found), len(found)):
        return "skipped", "the ssc markers are broken or repeated; fix them by hand", None
    if not found:
        if not current or current.endswith("\n\n"):
            sep = ""
        elif current.endswith("\n"):
            sep = "\n"
        else:
            sep = "\n\n"
        return "updated", None, f"{current}{sep}{block}"
    m = found[0]
    if m.group(0).rstrip("\n") == block.rstrip("\n"):
        return "unchanged", None, None
    if not force:
        return "skipped", "the ssc block differs; run `ssc init --force` to replace it", None
    return "updated", None, current[: m.start()] + block + current[m.end() :]


def _owned(path: Path, rel: str, text: str, *, force: bool) -> Written:
    if not path.exists():
        _write(path, text)
        return Written(rel, "created")
    if _read(path) == text:
        return Written(rel, "unchanged")
    if not force:
        return Written(rel, "skipped", "the file differs; run `ssc init --force` to replace it")
    _write(path, text)
    return Written(rel, "updated")


def _starter(path: Path, rel: str, text: str) -> Written:
    if path.exists():
        return Written(rel, "unchanged")
    _write(path, text)
    return Written(rel, "created")
