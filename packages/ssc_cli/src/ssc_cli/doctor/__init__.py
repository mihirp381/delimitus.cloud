"""``ssc doctor``: offline checks for the failures seen most often before an app runs."""

from pathlib import Path
from typing import Final

from ssc_cli.doctor.finding import Finding, Severity
from ssc_cli.doctor.rules import RULES, load_tree

ORDER: Final[dict[Severity, int]] = {"block": 0, "warn": 1, "info": 2}


def run_doctor(root: Path) -> list[Finding]:
    """Every finding for the folder at ``root``: blocking ones, then warnings, then notes."""
    tree = load_tree(root)
    found = [f for rule in RULES for f in rule(tree)]
    if any(f.code == "NOT_SINGLE_APP" for f in found):
        found = [f for f in found if f.code not in {"NO_START_COMMAND", "MANIFEST_MISSING"}]
    if any(f.code in {"NOT_SINGLE_APP", "NO_START_COMMAND"} for f in found):
        found = [f for f in found if f.code != "SESSION_FRAMEWORK"]
    return sorted(found, key=lambda f: (ORDER[f.severity], f.code, f.path, f.line or 0))


__all__ = ["Finding", "run_doctor"]
