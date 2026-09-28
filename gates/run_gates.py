"""Prove every CI gate fires: each fixture must FAIL its gate. Exit 1 if any gate stays silent."""

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FX = ROOT / "gates" / "fixtures"
PY = sys.executable


def run(cmd: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None) -> int:
    e = dict(os.environ, **(env or {}))
    return subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, check=False).returncode


def refresh_lockage() -> None:
    with urllib.request.urlopen("https://pypi.org/pypi/pyjwt/json", timeout=20) as r:  # noqa: S310
        version = json.load(r)["info"]["version"]
    (FX / "lockage" / "uv.lock").write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.14"\n\n[[package]]\nname = "pyjwt"\n'
        f'version = "{version}"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
    )


GATES = {
    "ruff": lambda: run([PY, "-m", "ruff", "check", "--no-cache", "--isolated", "--select", "F,S,T20", str(FX / "ruff")]),
    "pyright": lambda: run([PY, "-m", "pyright", "-p", str(FX / "pyright")]),
    "import-linter": lambda: run(
        [str(Path(PY).parent / "lint-imports"), "--config", "setup.cfg"],
        cwd=FX / "importlinter",
        env={"PYTHONPATH": str(FX / "importlinter")},
    ),
    "deptry": lambda: run([PY, "-m", "deptry", ".", "--config", "pyproject.toml"], cwd=FX / "deptry"),
    "zizmor": lambda: run([PY, "-m", "zizmor", "--no-online-audits", str(FX / "zizmor")]),
    "pytest": lambda: run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(FX / "pytest"), str(FX / "pytest")]),
    "hypothesis": lambda: run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(FX / "hypothesis"), str(FX / "hypothesis")]),
    "lock-age": lambda: run([PY, str(ROOT / "tools" / "lock_age_check.py"), str(FX / "lockage" / "uv.lock"), str(FX / "lockage" / "none.toml")]),
    "gitleaks": lambda: run(["gitleaks", "detect", "--no-git", "--source", str(FX / "gitleaks"), "--exit-code", "1", "--no-banner"]) if shutil.which("gitleaks") else None,
}


def main() -> int:
    if "--refresh-lockage" in sys.argv:
        refresh_lockage()
    silent = []
    for name, gate in GATES.items():
        code = gate()
        if code is None:
            print(f"SKIP  {name} (tool not installed)")  # noqa: T201
            continue
        status = "FIRED" if code != 0 else "SILENT"
        print(f"{status:6} {name} (exit {code})")  # noqa: T201
        if code == 0:
            silent.append(name)
    if silent:
        print(f"gates that did not fire: {', '.join(silent)}")  # noqa: T201
        return 1
    print("every gate fired on its planted violation")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
