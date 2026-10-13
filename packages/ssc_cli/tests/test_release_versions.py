"""tools/release_versions.py: the published packages share one version and pin each other."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
PUBLISHED = ["ssc-contracts", "ssc-app", "ssc-shared", "ssc-bundle", "ssc-cli"]
HELPERS = ["ssc-identity", "ssc-reconnect", "ssc-files", "ssc-data"]


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
    assert tool.packed(ROOT) == HELPERS
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
    listed: list[str] | None = None,
) -> Path:
    workflow = root / tool_workflow()
    workflow.parent.mkdir(parents=True)
    matrix = ", ".join(n.removeprefix("ssc-") for n in (builds if listed is None else listed))
    workflow.write_text(
        "".join(f"      - run: uv build --package {n} --out-dir dist\n" for n in builds)
        + f"      matrix:\n        package: [{matrix}]\n"
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


def test_the_publish_job_lists_what_is_built_in_its_order(tool, tmp_path):
    packages = {"ssc-app": ("0.0.1", []), "ssc-cli": ("0.0.1", [])}
    builds = ["ssc-app", "ssc-cli"]
    forgotten = _tree(tmp_path / "forgotten", packages, builds, listed=["ssc-cli"])
    assert tool.check(forgotten) == [
        "release-cli.yml builds ssc-app, ssc-cli but its PyPI job publishes ssc-cli"
    ]
    swapped = _tree(tmp_path / "swapped", packages, builds, listed=["ssc-cli", "ssc-app"])
    assert tool.check(swapped) == [
        "release-cli.yml builds ssc-app, ssc-cli but its PyPI job publishes ssc-cli, ssc-app"
    ]


def _helpers(root: Path, packages: dict[str, dict[str, object]], listed: list[str]) -> Path:
    """Adds node helpers to a tree: the workflow packs each one and its npm job lists some."""
    workflow = root / tool_workflow()
    workflow.write_text(
        workflow.read_text()
        + "".join(
            f"          npm pack ./helpers/node/{n} --pack-destination npm-dist\n" for n in packages
        )
        + f"      matrix:\n        helper: [{', '.join(listed)}]\n"
    )
    for name, package in packages.items():
        if package:
            folder = root / "helpers" / "node" / name
            folder.mkdir(parents=True)
            (folder / "package.json").write_text(json.dumps(package))
    return root


def test_a_node_helper_is_ready_to_publish_at_the_shared_version(tool, tmp_path):
    cli = {"ssc-cli": ("0.0.2", [])}
    public = {"publishConfig": {"access": "public"}}
    good = {"name": "@delimitus/ssc-files", "version": "0.0.2", **public}
    ok = _tree(tmp_path / "ok", cli, ["ssc-cli"], action_cli="0.0.2")
    assert tool.check(_helpers(ok, {"ssc-files": good}, ["ssc-files"]), "cli-v0.0.2") == []
    bad = _tree(tmp_path / "bad", cli, ["ssc-cli"], action_cli="0.0.2")
    packages = {
        "ssc-files": {"name": "ssc-files", "version": "0.1.0", "private": True},
        "ssc-data": good | {"name": "@delimitus/ssc-data"},
        "ssc-gone": {},
    }
    path = "helpers/node/ssc-files/package.json"
    assert tool.check(_helpers(bad, packages, ["ssc-files", "ssc-gone"])) == [
        "release-cli.yml packs ssc-files, ssc-data, ssc-gone "
        "but its npm job publishes ssc-files, ssc-gone",
        f"{path} must be named @delimitus/ssc-files",
        f"{path} must be version 0.0.2, not 0.1.0",
        f"{path} must not be private",
        f"{path} must set publishConfig.access to public",
        "ssc-gone is packed but helpers/node/ssc-gone/package.json does not exist",
    ]


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
