"""The packages ``release-cli.yml`` publishes share one version and pin each other to it exactly,
a ``cli-v<version>`` tag names that version, and the ssc-deploy Action installs that ``ssc-cli``
by default (decision 017). Exits 1 on any problem.

    uv run --no-project --python 3.14 python tools/release_versions.py [--tag cli-v0.0.1]
"""

import argparse
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = Path(".github") / "workflows" / "release-cli.yml"
ACTION = Path(".github") / "actions" / "ssc-deploy" / "action.yml"
TAG_PREFIX = "cli-v"
_ACTION_CLI = re.compile(r'^    default: "ssc-cli==([^"]*)"$', re.M)
_BUILD = re.compile(r"uv build --package ([A-Za-z0-9._-]+)")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def published(root: Path) -> list[str]:
    """The distributions the release workflow builds, in its order."""
    return [canonical(n) for n in _BUILD.findall((root / WORKFLOW).read_text())]


def members(root: Path) -> dict[str, dict[str, Any]]:
    """Every workspace package's ``[project]`` table, by canonical name."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("packages/*/pyproject.toml")):
        project = tomllib.loads(path.read_text())["project"]
        out[canonical(project["name"])] = project
    return out


def _requirements(project: dict[str, Any]) -> list[str]:
    extras = project.get("optional-dependencies", {})
    return [*project.get("dependencies", []), *(r for group in extras.values() for r in group)]


def check(root: Path, tag: str | None = None) -> list[str]:
    names = published(root)
    projects = members(root)
    problems = [
        f"{n} is published but is not a workspace package" for n in names if n not in projects
    ]
    if problems or not names:
        return problems or ["release-cli.yml builds no package"]
    versions = {n: str(projects[n]["version"]) for n in names}
    if len(set(versions.values())) > 1:
        shown = ", ".join(f"{n} {v}" for n, v in versions.items())
        problems.append(f"the published packages must share one version: {shown}")
    for name in names:
        for req in _requirements(projects[name]):
            m = _NAME.match(req)
            dep = canonical(m.group(0)) if m else ""
            if dep not in projects:
                continue
            if dep not in versions:
                problems.append(f"{name} depends on {dep}, which release-cli.yml does not publish")
                continue
            spec = req[m.end() :].strip() if m else ""
            if spec != f"=={versions[dep]}":
                problems.append(f"{name} must pin {dep}=={versions[dep]}, not {req!r}")
    version = versions[names[-1]]
    if "ssc-cli" in versions:
        action = root / ACTION
        pinned = _ACTION_CLI.findall(action.read_text()) if action.exists() else []
        if pinned != [versions["ssc-cli"]]:
            problems.append(f"{ACTION} must install ssc-cli=={versions['ssc-cli']} by default")
    if tag and tag != f"{TAG_PREFIX}{version}":
        problems.append(f"tag {tag} does not name the version {version} ({TAG_PREFIX}{version})")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", default="", help="the pushed tag; empty skips the tag check")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    problems = check(args.root, args.tag or None)
    for p in problems:
        print(f"release_versions: {p}", file=sys.stderr)  # noqa: T201
    if not problems:
        print(f"release_versions: {', '.join(published(args.root))} ok")  # noqa: T201
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
