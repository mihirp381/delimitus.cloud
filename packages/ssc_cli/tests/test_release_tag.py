"""tools/release_versions.py refuses to check a release without the tag it publishes.

The tool's other tests are in test_release_versions.py beside this file.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_versions", ROOT / "tools" / "release_versions.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_missing_tag_is_refused(tool: ModuleType) -> None:
    with pytest.raises(SystemExit) as exited:
        tool.main([])
    assert exited.value.code == 2


def test_an_empty_tag_is_refused(tool: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert tool.main(["--tag", ""]) == 1
    assert "--tag must name the release tag" in capsys.readouterr().err


def test_the_tag_still_names_the_version(tool: ModuleType) -> None:
    version = tool.members(ROOT)["ssc-cli"]["version"]
    assert tool.main(["--tag", f"cli-v{version}"]) == 0
    assert tool.main(["--tag", "cli-v99.0.0"]) == 1
