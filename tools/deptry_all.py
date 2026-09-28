"""Run deptry once per workspace package, from that package's directory."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = sorted(p for p in (ROOT / "packages").iterdir() if (p / "pyproject.toml").exists())
PACKAGES.append(ROOT / "conformance")

failed = 0
for pkg in PACKAGES:
    result = subprocess.run(
        [sys.executable, "-m", "deptry", ".", "--config", "pyproject.toml"],
        cwd=pkg,
        check=False,
    )
    if result.returncode:
        failed += 1
        print(f"deptry failed in {pkg.name}")  # noqa: T201
raise SystemExit(1 if failed else 0)
