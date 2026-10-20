"""tools/lock_age_check.py and tools/npm_lock_age_check.py when the registry does not answer.

A package whose age cannot be learnt is a failure, after three retries 1, 2 and 4 seconds apart:
a gate that passes whenever PyPI or npm is down admits anything. The npm checker's other tests
are in packages/ssc_cli/tests/test_npm_lock_age_check.py.
"""

import importlib.util
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
REGISTRY = "https://registry.npmjs.org/"
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _load(monkeypatch: pytest.MonkeyPatch, name: str) -> Iterator[ModuleType]:
    monkeypatch.syspath_prepend(str(TOOLS))
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("lock_age_check", None)


@pytest.fixture
def pypi(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    yield from _load(monkeypatch, "lock_age_check")


@pytest.fixture
def npm(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    yield from _load(monkeypatch, "npm_lock_age_check")


class Flaky:
    """Raises ``failures`` times, then answers ``answer``; records each call."""

    def __init__(self, failures: int, answer: object) -> None:
        self.failures, self.answer, self.calls = failures, answer, 0

    def __call__(self, *_: str) -> object:
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError("offline")
        return self.answer


def uv_lock(tmp_path: Path) -> Path:
    path = tmp_path / "uv.lock"
    path.write_text(
        'version = 1\n\n[[package]]\nname = "a"\nversion = "1.0.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    return path


def npm_lock(tmp_path: Path) -> Path:
    entry = {"version": "1.0.0", "resolved": f"{REGISTRY}a/-/a-1.0.0.tgz"}
    lock = {"lockfileVersion": 3, "packages": {"": {"name": "x"}, "node_modules/a": entry}}
    path = tmp_path / "package-lock.json"
    path.write_text(json.dumps(lock))
    return path


def test_pypi_unreachable_is_a_failure_after_three_retries(
    pypi: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    uploads, slept = Flaky(99, None), []
    code = pypi.main(
        uv_lock(tmp_path), tmp_path / "none.toml", now=NOW, uploads=uploads, sleep=slept.append
    )
    assert code == 1
    assert (uploads.calls, slept) == (4, [1, 2, 4])
    out = capsys.readouterr().out
    assert out.count("WARN a==1.0.0: could not query PyPI (offline)") == 4
    assert "FAIL a==1.0.0: no answer from PyPI after 4 attempts" in out
    assert "1 failure(s)" in out


def test_pypi_answering_on_a_retry_passes(
    pypi: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    uploads, slept = Flaky(2, NOW - timedelta(days=30)), []
    code = pypi.main(
        uv_lock(tmp_path), tmp_path / "none.toml", now=NOW, uploads=uploads, sleep=slept.append
    )
    assert code == 0, capsys.readouterr().out
    assert (uploads.calls, slept) == (3, [1, 2])


def test_pypi_listing_no_upload_is_a_failure(
    pypi: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = pypi.main(uv_lock(tmp_path), tmp_path / "none.toml", now=NOW, uploads=lambda *_: None)
    assert code == 1
    assert "FAIL a==1.0.0: PyPI lists no upload" in capsys.readouterr().out


def test_npm_unreachable_is_a_failure_after_three_retries(
    npm: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetch, slept = Flaky(99, {}), []
    assert npm.main(npm_lock(tmp_path), tmp_path / "none.toml", fetch, sleep=slept.append) == 1
    assert (fetch.calls, slept) == (4, [1, 2, 4])
    out = capsys.readouterr().out
    assert out.count("WARN a@1.0.0: could not query the registry (offline)") == 4
    assert "FAIL a@1.0.0: no answer from the registry after 4 attempts" in out
    assert "1 package(s), 1 failure(s)" in out


def test_npm_answering_on_a_retry_passes(
    npm: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    fetch, slept = Flaky(1, {"1.0.0": old}), []
    code = npm.main(npm_lock(tmp_path), tmp_path / "none.toml", fetch, sleep=slept.append)
    assert code == 0, capsys.readouterr().out
    assert (fetch.calls, slept) == (2, [1])
