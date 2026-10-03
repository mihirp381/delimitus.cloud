"""Fakes for the kit's offline tests: subprocesses, HTTP, clocks and sockets. Nothing here reaches
a network or needs cloud credentials."""

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from proofrun.common import Done, Fetched


class FakeRun:
    """Answers each command from the first rule whose words all appear in it, and records it."""

    def __init__(self, rules: Sequence[tuple[Sequence[str], Done]] = ()) -> None:
        self.rules = list(rules)
        self.calls: list[list[str]] = []
        self.envs: list[Mapping[str, str] | None] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Done:
        self.calls.append(list(argv))
        self.envs.append(env)
        for words, done in self.rules:
            if all(w in argv for w in words):
                return done
        raise AssertionError(f"unexpected command: {argv}")


def ok(payload: Any) -> Done:
    """A successful command printing ``payload`` as JSON."""
    return Done(0, json.dumps(payload), "")


class FakeHttp:
    """Answers each URL from the first rule whose fragment it contains, and records it."""

    def __init__(self, rules: Sequence[tuple[str, Fetched | Callable[[], Fetched]]]) -> None:
        self.rules = list(rules)
        self.calls: list[tuple[str, Mapping[str, str] | None]] = []

    def __call__(
        self, url: str, headers: Mapping[str, str] | None = None, timeout: float = 90.0
    ) -> Fetched:
        self.calls.append((url, headers))
        for fragment, answer in self.rules:
            if fragment in url:
                return answer() if callable(answer) else answer
        raise AssertionError(f"unexpected request: {url}")


def answer(status: int | None, body: Any = b"", seconds: float = 0.1) -> Fetched:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return Fetched(status, seconds, None if status else "URLError: refused", raw)


class Clock:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own cookie jar and results folder."""
    monkeypatch.setenv("PROOFRUN_COOKIES", str(tmp_path / "jar" / "cookies.json"))
    monkeypatch.setenv("PROOFRUN_RESULTS", str(tmp_path / "results"))
    return tmp_path


@pytest.fixture
def fake_digest(monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the fence at a harmless 16-character value, so it can be exercised."""
    import hashlib

    from proofrun import common

    value = "fencedvalue12345"
    monkeypatch.setattr(common, "FENCE_SHA256", hashlib.sha256(value.encode()).hexdigest())
    return value
