"""``ssc doctor``: offline checks for the failures seen most often before an app runs."""

from pathlib import Path

from ssc_cli.doctor.finding import Finding
from ssc_cli.doctor.rules import RULES, load_tree


def run_doctor(root: Path) -> list[Finding]:
    """Every finding for the folder at ``root``, blocking ones first."""
    tree = load_tree(root)
    found = [f for rule in RULES for f in rule(tree)]
    if any(f.code == "NOT_SINGLE_APP" for f in found):
        found = [f for f in found if f.code != "NO_START_COMMAND"]
    return sorted(found, key=lambda f: (f.severity != "block", f.code, f.path, f.line or 0))


__all__ = ["Finding", "run_doctor"]
