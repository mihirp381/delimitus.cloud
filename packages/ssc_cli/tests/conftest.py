"""CLI test isolation, plus a live API on the dev stack for the journey tests.

No test touches the real OS keychain or the person's config. Every test gets an in-memory keyring
and a HOME under tmp_path, and fails if a real keychain backend is active. The live API runs
``tools/dev_stack.py serve --port 0 --worker`` (fake builder and runtime) against a postgres:18
testcontainer with no fixed name.
"""

import os

# Before keyring picks a backend: any code path that escapes the fixture fails instead of
# reaching the macOS keychain.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"

import importlib.util
import json
import select
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx2
import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail, null
from keyring.errors import PasswordDeleteError
from typer.testing import CliRunner

from ssc_cli.main import app
from ssc_cli.session import Session

ROOT = Path(__file__).resolve().parents[3]
DEV_STACK = ROOT / "tools" / "dev_stack.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class MemoryKeyring(KeyringBackend):
    priority = -10  # never picked by keyring's own detection

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.store.pop((service, username), None) is None:
            raise PasswordDeleteError("not found")


SAFE_BACKENDS = (MemoryKeyring, fail.Keyring, null.Keyring)


def _assert_safe_backend() -> None:
    active = keyring.get_keyring()
    assert isinstance(active, SAFE_BACKENDS), f"a real keychain backend is active: {active!r}"


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    for name in ("SSC_TOKEN", "SSC_API_URL"):
        monkeypatch.delenv(name, raising=False)
    _assert_safe_backend()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    yield memory
    _assert_safe_backend()
    keyring.set_keyring(fail.Keyring())


# ── running the command in-process ───────────────────────────────────────────


@dataclass(frozen=True)
class Ran:
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout)


Cli = Callable[..., Ran]


def _cli(*args: str, input: str | None = None, session: Session | None = None) -> Ran:
    r = CliRunner().invoke(app, list(args), input=input, obj=session, catch_exceptions=False)
    return Ran(r.exit_code, r.stdout, r.stderr)


@pytest.fixture
def cli() -> Cli:
    return _cli


# ── a scripted fake API for unit tests ───────────────────────────────────────


@dataclass
class FakeApi:
    """Answers requests from a queue of (method, path) -> response, and records every request."""

    routes: dict[tuple[str, str], list[httpx2.Response]] = field(default_factory=dict)
    seen: list[httpx2.Request] = field(default_factory=list)

    def add(self, method: str, path: str, *responses: httpx2.Response) -> None:
        self.routes.setdefault((method, path), []).extend(responses)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            return problem(404, "NOT_FOUND")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def session(self, api: str = "https://api.test") -> Session:
        return Session(
            api_override=api, transport=httpx2.MockTransport(self.handler), sleep=_no_sleep
        )


def _no_sleep(_: float) -> None:
    return None


def problem(status: int, code: str, **headers: str) -> httpx2.Response:
    body = {
        "type": f"https://errors.delimitus.com/{code.lower()}",
        "title": f"title for {code}",
        "status": status,
        "detail": f"detail for {code}",
        "instance": "/v1/x",
        "code": code,
        "request_id": "req_test",
    }
    return httpx2.Response(
        status,
        json=body,
        headers={"content-type": "application/problem+json", "x-request-id": "req_test", **headers},
    )


@pytest.fixture
def fake_api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def fake_problem() -> Callable[..., httpx2.Response]:
    return problem


# ── the live API on the dev stack ────────────────────────────────────────────


def load_dev_stack() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ssc_dev_stack", DEV_STACK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def dev_stack() -> ModuleType:
    return load_dev_stack()


@dataclass(frozen=True)
class Live:
    url: str
    dir: Path
    stack: ModuleType
    org_id: str
    admin_id: str

    def token(self, **claims: Any) -> str:
        return self.stack.mint(self.dir, **claims)


def _wait_for_url(proc: subprocess.Popen[str], log: Path, timeout: float = 60) -> str:
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    line = proc.stdout.readline() if ready else ""
    if not line.startswith("SSC_API_URL="):
        proc.kill()
        raise AssertionError(f"dev_stack serve did not start: {line!r}\n{log.read_text()}")
    return line.strip().removeprefix("SSC_API_URL=")


def _wait_healthy(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if httpx2.get(f"{url}/healthz", timeout=2).status_code == 200:
                return
        except httpx2.TransportError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"{url}/healthz never answered")
        time.sleep(0.2)


@pytest.fixture(scope="session")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Live]:
    from ssc_testkit import control_db

    stack = load_dev_stack()
    state_dir = tmp_path_factory.mktemp("ssc-dev")
    log = state_dir / "serve.log"
    with control_db() as dsns:
        stack.up(dsns.superuser, state_dir)
        state = stack.load_state(state_dir)
        env = {k: v for k, v in os.environ.items() if not k.startswith("SSC_")}
        with log.open("w") as err:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(DEV_STACK),
                    "--dir",
                    str(state_dir),
                    "serve",
                    "--port",
                    "0",
                    "--rate-capacity",
                    "100000",
                    "--rate-refill",
                    "100000",
                    "--worker",
                ],
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
                env=env,
            )
            try:
                url = _wait_for_url(proc, log)
                _wait_healthy(url)
                yield Live(url, state_dir, stack, state["org_id"], state["admin_user_id"])
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
