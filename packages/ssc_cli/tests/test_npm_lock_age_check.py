"""tools/npm_lock_age_check.py, with the registry replaced by a dict."""

import importlib.util
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

TOOLS = Path(__file__).resolve().parents[3] / "tools"
REGISTRY = "https://registry.npmjs.org/"


@pytest.fixture
def tool(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.syspath_prepend(str(TOOLS))
    path = TOOLS / "npm_lock_age_check.py"
    spec = importlib.util.spec_from_file_location("npm_lock_age_check", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("lock_age_check", None)


def days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).isoformat().replace("+00:00", "Z")


def write_lock(tmp_path: Path, packages: dict[str, dict]) -> Path:
    lock = {"name": "x", "lockfileVersion": 3, "packages": {"": {"name": "x"}, **packages}}
    path = tmp_path / "package-lock.json"
    path.write_text(json.dumps(lock))
    return path


def entry(name: str, version: str) -> dict:
    return {"version": version, "resolved": f"{REGISTRY}{name}/-/{name}-{version}.tgz"}


def run(tool: ModuleType, tmp_path: Path, packages: dict, times: dict, exceptions: str = "") -> int:
    exc = tmp_path / "lock-exceptions.toml"
    exc.write_text(exceptions)

    def fetch(name: str) -> dict[str, str]:
        if isinstance(times[name], Exception):
            raise times[name]
        return times[name]

    return tool.main(write_lock(tmp_path, packages), exc, fetch)


def test_old_versions_pass_and_nested_copies_are_checked_once(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packages = {
        "node_modules/a": entry("a", "1.0.0"),
        "node_modules/b/node_modules/a": entry("a", "1.0.0"),
        "node_modules/@s/c": entry("@s/c", "2.0.0"),
    }
    times = {"a": {"1.0.0": days_ago(30)}, "@s/c": {"2.0.0": days_ago(8)}}
    assert run(tool, tmp_path, packages, times) == 0
    assert "2 package(s), 0 failure(s)" in capsys.readouterr().out


def test_a_version_younger_than_seven_days_fails(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packages = {"node_modules/a": entry("a", "1.0.1")}
    assert run(tool, tmp_path, packages, {"a": {"1.0.1": days_ago(2)}}) == 1
    assert "FAIL a@1.0.1 published" in capsys.readouterr().out


def test_an_exception_admits_a_young_version_until_it_expires(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packages = {"node_modules/a": entry("a", "1.0.1")}
    times = {"a": {"1.0.1": days_ago(2)}}
    later = (datetime.now(UTC) + timedelta(days=5)).date().isoformat()
    earlier = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    rule = '[[exception]]\nname = "a"\nversion = "1.0.1"\nreason = "r"\nexpires = "{}"\n'
    assert run(tool, tmp_path, packages, times, rule.format(later)) == 0
    assert "EXCEPTION a@1.0.1" in capsys.readouterr().out
    assert run(tool, tmp_path, packages, times, rule.format(earlier)) == 1
    assert "EXPIRED exception for a==1.0.1" in capsys.readouterr().out


def test_a_package_from_outside_the_registry_fails(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packages = {
        "node_modules/g": {"version": "1.0.0", "resolved": "git+ssh://git@example.invalid/g.git"},
        "node_modules/w": {"resolved": "packages/w", "link": True},
    }
    assert run(tool, tmp_path, packages, {}) == 1
    out = capsys.readouterr().out
    assert "FAIL node_modules/g (git+ssh://git@example.invalid/g.git)" in out
    assert "node_modules/w" not in out


def test_an_unlisted_version_fails_and_a_registry_error_warns(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packages = {"node_modules/a": entry("a", "9.9.9"), "node_modules/b": entry("b", "1.0.0")}
    times = {"a": {"1.0.0": days_ago(30)}, "b": OSError("offline")}
    assert run(tool, tmp_path, packages, times) == 1
    out = capsys.readouterr().out
    assert "FAIL a@9.9.9: the registry lists no publish time" in out
    assert "WARN b@1.0.0: could not query the registry (offline)" in out


def test_an_aliased_package_is_checked_under_its_real_name(
    tool: ModuleType, tmp_path: Path
) -> None:
    packages = {"node_modules/alias": {**entry("real", "1.0.0"), "name": "real"}}
    assert run(tool, tmp_path, packages, {"real": {"1.0.0": days_ago(2)}}) == 1
