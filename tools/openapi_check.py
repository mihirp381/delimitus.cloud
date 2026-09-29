"""Fail if docs/api/openapi.json differs from what the application renders.

uv run python tools/openapi_check.py          # compare, exit 1 on drift
uv run python tools/openapi_check.py --write  # regenerate the committed file
"""

import sys
from pathlib import Path

from ssc_control.api.openapi import spec_json

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "api" / "openapi.json"


def main(argv: list[str]) -> int:
    rendered = spec_json()
    if "--write" in argv:
        SPEC.parent.mkdir(parents=True, exist_ok=True)
        SPEC.write_text(rendered)
        print(f"wrote {SPEC.relative_to(ROOT)}")  # noqa: T201
        return 0
    if not SPEC.exists():
        print(f"{SPEC.relative_to(ROOT)} is missing; run with --write")  # noqa: T201
        return 1
    if SPEC.read_text() != rendered:
        print(f"{SPEC.relative_to(ROOT)} is stale; run with --write and commit")  # noqa: T201
        return 1
    print(f"{SPEC.relative_to(ROOT)} matches the application")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
