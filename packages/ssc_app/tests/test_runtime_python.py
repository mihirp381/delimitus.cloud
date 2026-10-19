"""The helper runs on the app's own Python, 3.11 and later, which CI job python-helper also checks
end to end. Every source file of the helper must parse and import as 3.11 does, and the package
must say it installs on it, or the app fails at install or import and its health check with it."""

import ast
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

RUNTIME = (3, 11)
APP = Path(__file__).parents[1]
SOURCES = sorted((APP / "src" / "ssc_app").rglob("*.py"))
IMPORT_ALL = """
import importlib, pathlib, sys
sys.path[:0] = sys.argv[1:]
for src in sys.argv[1:]:
    for path in sorted(pathlib.Path(src).rglob("*.py")):
        parts = path.relative_to(src).with_suffix("").parts
        importlib.import_module(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
print("imported", sys.version_info[:2])
"""


def _project() -> dict[str, Any]:
    with (APP / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]


def test_the_sources_are_found() -> None:
    names = {f"{p.parent.name}/{p.name}" for p in SOURCES}
    assert names >= {
        "ssc_app/data.py",
        "ssc_app/files.py",
        "ssc_app/identity.py",
        "ssc_app/note.py",
    }


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_source_parses_as_the_app_runtime_python(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=RUNTIME)


def test_the_helper_installs_on_the_app_runtime_python() -> None:
    # An installer refuses a package whose requires-python leaves 3.11 out.
    assert _project()["requires-python"] == ">=3.11"


def test_the_helper_imports_under_a_real_python_311(tmp_path: Path) -> None:
    # Parsing misses what only runs at import, such as an annotation naming its own class
    # before the class exists, which 3.14 defers and 3.11 evaluates.
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH, so no Python 3.11 can be found to import the helper with")
    found = subprocess.run(
        [uv, "python", "find", "3.11"], capture_output=True, text=True, check=False
    )
    if found.returncode != 0:
        pytest.skip("`uv python find 3.11` found no Python 3.11; run `uv python install 3.11`")
    venv = tmp_path / "venv"
    python = str(venv / "bin" / "python")
    for step in (
        [uv, "venv", "--quiet", "--python", found.stdout.strip(), str(venv)],
        [uv, "pip", "install", "--quiet", "--python", python, *_project()["dependencies"]],
    ):
        made = subprocess.run(step, cwd=tmp_path, capture_output=True, text=True, check=False)
        if made.returncode != 0:
            pytest.skip(
                f"could not set up Python 3.11 with the helper's dependencies: {made.stderr}"
            )
    ran = subprocess.run(
        [python, "-I", "-c", IMPORT_ALL, str(APP / "src")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == "imported (3, 11)"
