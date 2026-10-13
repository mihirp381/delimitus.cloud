"""The helper runs on the app runtime's Python, which is older than the platform's: Railpack
installs Python 3.13 for a deployed app (spikes/proofrun/RESULTS.md, F4). Every source file of the
helper and of ``ssc_contracts``, which it imports, must parse and import as 3.13 does, and both
packages must say they install on it, or the app fails at install or import and its health check
with it."""

import ast
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

RUNTIME = (3, 13)
PACKAGES = Path(__file__).parents[2]
APP = PACKAGES / "ssc_app"
CONTRACTS = PACKAGES / "ssc_contracts"
SOURCES = sorted((APP / "src" / "ssc_app").glob("*.py")) + sorted(
    (CONTRACTS / "src" / "ssc_contracts").rglob("*.py")
)
IMPORT_ALL = """
import importlib, pathlib, sys
sys.path[:0] = sys.argv[1:]
for src in sys.argv[1:]:
    for path in sorted(pathlib.Path(src).rglob("*.py")):
        parts = path.relative_to(src).with_suffix("").parts
        importlib.import_module(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
print("imported", sys.version_info[:2])
"""


def _project(package: Path) -> dict[str, Any]:
    with (package / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]


def test_the_sources_are_found() -> None:
    names = {f"{p.parent.name}/{p.name}" for p in SOURCES}
    assert names >= {"ssc_app/data.py", "ssc_app/files.py", "ssc_contracts/identity.py"}


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_source_parses_as_the_app_runtime_python(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=RUNTIME)


@pytest.mark.parametrize("package", [APP, CONTRACTS], ids=lambda p: p.name)
def test_both_packages_install_on_the_app_runtime_python(package: Path) -> None:
    # An installer refuses a package whose requires-python leaves 3.13 out, and ssc-app
    # depends on ssc-contracts, so both must let it in.
    assert _project(package)["requires-python"] == ">=3.13,<3.15"


def test_the_helper_imports_under_a_real_python_313(tmp_path: Path) -> None:
    # Parsing misses what only runs at import, such as an annotation naming its own class
    # before the class exists, which 3.14 defers and 3.13 evaluates.
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH, so no Python 3.13 can be found to import the helper with")
    found = subprocess.run(
        [uv, "python", "find", "3.13"], capture_output=True, text=True, check=False
    )
    if found.returncode != 0:
        pytest.skip("`uv python find 3.13` found no Python 3.13; run `uv python install 3.13`")
    venv = tmp_path / "venv"
    third_party = [d for d in _project(APP)["dependencies"] if not d.startswith("ssc-")]
    for step in (
        [uv, "venv", "--quiet", "--python", found.stdout.strip(), str(venv)],
        [uv, "pip", "install", "--quiet", "--python", str(venv / "bin" / "python"), *third_party],
    ):
        made = subprocess.run(step, cwd=tmp_path, capture_output=True, text=True, check=False)
        if made.returncode != 0:
            pytest.skip(
                f"could not set up Python 3.13 with the helper's dependencies: {made.stderr}"
            )
    ran = subprocess.run(
        [str(venv / "bin" / "python"), "-I", "-c", IMPORT_ALL]
        + [str(APP / "src"), str(CONTRACTS / "src")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == "imported (3, 13)"
