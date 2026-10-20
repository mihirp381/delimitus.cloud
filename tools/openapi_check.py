"""Fail if docs/api/openapi.json, or the route index in docs/api/README.md, differs from what the
application renders.

uv run python tools/openapi_check.py          # compare, exit 1 on drift
uv run python tools/openapi_check.py --write  # regenerate the committed file and the index
"""

import json
import sys
from pathlib import Path
from typing import Any

from ssc_control.api.openapi import spec_json

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "api" / "openapi.json"
README = ROOT / "docs" / "api" / "README.md"
START, END = "<!-- route-index -->", "<!-- /route-index -->"


def route_index(spec: dict[str, Any]) -> str:
    rows = [
        f"| `{method.upper()}` | `{path}` | {op['summary']} |"
        for path, ops in sorted(spec["paths"].items())
        for method, op in ops.items()
    ]
    return "\n".join([START, "| Method | Path | What |", "|---|---|---|", *rows, END])


def with_index(readme: str, index: str) -> str:
    head, rest = readme.split(START, 1)
    return head + index + rest.split(END, 1)[1]


def main(argv: list[str]) -> int:
    rendered = spec_json()
    readme = Path(argv[argv.index("--readme") + 1]) if "--readme" in argv else README
    index = route_index(json.loads(rendered))
    if "--write" in argv:
        SPEC.parent.mkdir(parents=True, exist_ok=True)
        SPEC.write_text(rendered)
        readme.write_text(with_index(readme.read_text(), index))
        print(f"wrote {SPEC.relative_to(ROOT)} and the route index")  # noqa: T201
        return 0
    if not SPEC.exists():
        print(f"{SPEC.relative_to(ROOT)} is missing; run with --write")  # noqa: T201
        return 1
    if SPEC.read_text() != rendered:
        print(f"{SPEC.relative_to(ROOT)} is stale; run with --write and commit")  # noqa: T201
        return 1
    current = readme.read_text()
    if START not in current or with_index(current, index) != current:
        print(f"the route index in {readme.name} is stale; run with --write and commit")  # noqa: T201
        return 1
    print(f"{SPEC.relative_to(ROOT)} and the route index match the application")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
