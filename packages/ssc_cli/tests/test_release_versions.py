"""tools/release_versions.py: the published packages share one version and pin each other."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
PUBLISHED = ["ssc-contracts", "ssc-shared", "ssc-bundle", "ssc-cli"]


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_versions", ROOT / "tools" / "release_versions.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_repository_is_ready_to_release(tool):
    assert tool.published(ROOT) == PUBLISHED
    assert tool.check(ROOT) == []
    version = tool.members(ROOT)["ssc-cli"]["version"]
    assert tool.check(ROOT, f"cli-v{version}") == []
    assert tool.main(["--tag", f"cli-v{version}"]) == 0


def test_a_tag_must_name_the_version(tool):
    (problem,) = tool.check(ROOT, "cli-v99.0.0")
    assert problem.startswith("tag cli-v99.0.0 does not name the version")
    assert tool.main(["--tag", "cli-v99.0.0"]) == 1


def _tree(
    root: Path,
    packages: dict[str, tuple[str, list[str]]],
    builds: list[str],
    action_cli: str = "0.0.1",
) -> Path:
    workflow = root / tool_workflow()
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "".join(f"      - run: uv build --package {n} --out-dir dist\n" for n in builds)
    )
    action = root / ".github" / "actions" / "ssc-deploy" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text(f'inputs:\n  cli-spec:\n    default: "ssc-cli=={action_cli}"\n')
    for name, (version, deps) in packages.items():
        folder = root / "packages" / name.replace("-", "_")
        folder.mkdir(parents=True)
        listed = ", ".join(f'"{d}"' for d in deps)
        text = f'[project]\nname = "{name}"\nversion = "{version}"\ndependencies = [{listed}]\n'
        (folder / "pyproject.toml").write_text(text)
    return root


def tool_workflow() -> Path:
    return Path(".github") / "workflows" / "release-cli.yml"


def test_problems_are_named(tool, tmp_path):
    root = _tree(
        tmp_path,
        {
            "ssc-contracts": ("0.0.2", ["pydantic==2.13.5"]),
            "ssc-shared": ("0.0.1", ["ssc-contracts"]),
            "ssc-cli": ("0.0.1", ["ssc_shared==0.0.1", "ssc-control==0.0.1", "typer==0.27.2"]),
            "ssc-control": ("0.0.1", []),
        },
        ["ssc-contracts", "ssc-shared", "ssc-cli"],
    )
    assert tool.check(root, "cli-v0.0.1") == [
        "the published packages must share one version: "
        "ssc-contracts 0.0.2, ssc-shared 0.0.1, ssc-cli 0.0.1",
        "ssc-shared must pin ssc-contracts==0.0.2, not 'ssc-contracts'",
        "ssc-cli depends on ssc-control, which release-cli.yml does not publish",
    ]


def test_a_published_name_must_be_a_workspace_package(tool, tmp_path):
    root = _tree(tmp_path, {"ssc-cli": ("0.0.1", [])}, ["ssc-cli", "ssc-typo"])
    assert tool.check(root) == ["ssc-typo is published but is not a workspace package"]


def test_the_action_installs_the_released_cli(tool, tmp_path):
    packages = {"ssc-cli": ("0.0.2", [])}
    stale = _tree(tmp_path / "stale", packages, ["ssc-cli"], action_cli="0.0.1")
    assert tool.check(stale) == [
        ".github/actions/ssc-deploy/action.yml must install ssc-cli==0.0.2 by default"
    ]
    assert tool.check(_tree(tmp_path / "ok", packages, ["ssc-cli"], action_cli="0.0.2")) == []
    missing = _tree(tmp_path / "missing", packages, ["ssc-cli"])
    (missing / ".github" / "actions" / "ssc-deploy" / "action.yml").unlink()
    assert tool.check(missing) == [
        ".github/actions/ssc-deploy/action.yml must install ssc-cli==0.0.2 by default"
    ]
