"""Prove every CI gate fires: each fixture must FAIL its gate. Exit 1 if any gate stays silent."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FX = ROOT / "gates" / "fixtures"
PY = sys.executable


def run(cmd: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None) -> tuple[int, str]:
    e = dict(os.environ, **(env or {}))
    r = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, encoding="utf-8", errors="replace", check=False)
    return r.returncode, r.stdout + r.stderr


LOCKAGE_NOW = "2026-09-30T00:00:00+00:00"
"""Two days after the fixture's recorded upload, so the 7-day rule always fires on it."""

# Each gate with what its output must name: the planted violation, so a tool that fails to start
# (a missing module, a bad flag) is SILENT, not FIRED.
GATES = {
    "ruff": (lambda: run([PY, "-m", "ruff", "check", "--no-cache", "--isolated", "--select", "F,S,T20", str(FX / "ruff")]), "T201"),
    "pyright": (lambda: run([PY, "-m", "pyright", "-p", str(FX / "pyright")]), "reportArgumentType"),
    "import-linter": (lambda: run(
        [str(Path(PY).parent / "lint-imports"), "--config", "setup.cfg"],
        cwd=FX / "importlinter",
        env={"PYTHONPATH": str(FX / "importlinter")},
    ), "Cell services never import the control plane BROKEN"),
    "deptry": (lambda: run([PY, "-m", "deptry", ".", "--config", "pyproject.toml"], cwd=FX / "deptry"), "DEP001"),
    "zizmor": (lambda: run([str(Path(PY).parent / "zizmor"), "--no-online-audits", str(FX / "zizmor")]), "template-injection"),
    "pytest": (lambda: run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(FX / "pytest"), str(FX / "pytest")]), "test_bad.py::test_planted_failure - assert 1 == 2"),
    "hypothesis": (lambda: run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(FX / "hypothesis"), str(FX / "hypothesis")]), "Failing test case: test_planted_property_failure"),
    "lock-age": (lambda: run([PY, str(ROOT / "tools" / "lock_age_check.py"), str(FX / "lockage" / "uv.lock"), str(FX / "lockage" / "none.toml"), "--uploads", str(FX / "lockage" / "uploads.json"), "--now", LOCKAGE_NOW]), "FAIL pyjwt=="),
    "openapi-breaking": (lambda: run([PY, str(ROOT / "tools" / "openapi_breaking.py"), str(FX / "openapi" / "old.json"), str(FX / "openapi" / "new.json")]), "BREAKING POST /v1/things: operation removed"),
    "gitleaks": (lambda: run(["gitleaks", "detect", "--no-git", "--source", str(FX / "gitleaks"), "--exit-code", "1", "--no-banner"]) if shutil.which("gitleaks") else None, "leaks found"),
}


def main() -> int:
    silent = []
    for name, (gate, marker) in GATES.items():
        result = gate()
        if result is None:
            print(f"SKIP  {name} (tool not installed)")  # noqa: T201
            continue
        code, output = result
        fired = code != 0 and marker in output
        status = "FIRED" if fired else "SILENT"
        print(f"{status:6} {name} (exit {code}{'' if fired else f', no {marker!r} in its output'})")  # noqa: T201
        if not fired:
            silent.append(name)
    if silent:
        print(f"gates that did not fire: {', '.join(silent)}")  # noqa: T201
        return 1
    print("every gate fired on its planted violation")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
