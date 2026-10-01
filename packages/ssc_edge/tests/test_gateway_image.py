"""The gateway image (SSC-018): built from the repository root, Envoy and the authoriser both
start, and the container stops when either cannot run. Without Docker these tests skip locally
and fail in CI."""

import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from edge_world import LABEL, ORG

from ssc_edge.keys import new_keyring

ROOT = Path(__file__).resolve().parents[3]
APP_HOST = f"ledger.{LABEL}.delimitusapps.com"


def docker() -> str:
    found = shutil.which("docker")
    if (
        found is None
        or subprocess.run([found, "info"], capture_output=True, check=False).returncode
    ):
        if os.environ.get("CI"):
            pytest.fail("Docker is required in CI for the gateway image")
        pytest.skip("Docker is not available")
    return found


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    out = subprocess.run([docker(), *args], capture_output=True, text=True, check=False)
    if check and out.returncode:
        raise AssertionError(f"docker {args[0]} failed: {out.stderr[-3000:]}")
    return out


def settings(**changes: str) -> list[str]:
    env = {
        "SSC_ENV": "test",
        "SSC_GATEWAY_KEYRING_PLAIN": new_keyring().decode(),
        "SSC_ORG_ID": ORG,
        "SSC_CELL_LABEL": LABEL,
        "SSC_PROJECT_NUMBER": "123456789012",
        "SSC_CELL_BUCKET": f"ssc-c-{LABEL}-cell",
        "STORAGE_EMULATOR_HOST": "http://127.0.0.1:1",  # no bucket: the feed keeps no view
    }
    env.update(changes)
    return [arg for k, v in env.items() if v for arg in ("-e", f"{k}={v}")]


@pytest.fixture(scope="module")
def image(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    iid = tmp_path_factory.mktemp("image") / "iid"
    run("build", "--quiet", "--iidfile", str(iid), "-f", "packages/ssc_edge/Dockerfile", str(ROOT))
    image = iid.read_text().strip()
    yield image
    run("rmi", "--force", image, check=False)


def wait_exit(name: str, seconds: float) -> int | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = run("inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", name).stdout.split()
        if state[0] == "false":
            return int(state[1])
        time.sleep(0.2)
    return None


def test_the_image_serves_and_stops_cleanly(image: str) -> None:
    name = "ssc018-image-" + secrets.token_hex(4)
    run("run", "-d", "--name", name, "-p", "127.0.0.1::8080", *settings(), image)
    try:
        port = run("port", name, "8080/tcp").stdout.splitlines()[0].rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{port}/"
        deadline = time.monotonic() + 60
        while True:
            try:
                login = httpx2.get(url, headers={"host": APP_HOST}, timeout=2)
                break
            except httpx2.HTTPError:
                if time.monotonic() > deadline:
                    raise AssertionError(run("logs", name).stderr[-3000:]) from None
                time.sleep(0.3)
        assert login.status_code == 302
        assert login.headers["location"].startswith("https://auth.delimitus.com/login?")
        assert httpx2.get(url, headers={"host": "example.com"}, timeout=2).status_code == 404
        top = run("top", name).stdout
        assert "ssc_edge.server" in top and "envoy -c" in top
        logs = run("logs", name).stdout + run("logs", name).stderr
        assert "code=" not in logs and "GET /authz" not in logs  # no access log
        started = time.monotonic()
        run("stop", "-t", "30", name)
        assert wait_exit(name, 5) == 0
        assert time.monotonic() - started < 5
    finally:
        run("rm", "-f", name, check=False)


def test_the_container_exits_when_the_authoriser_cannot_start(image: str) -> None:
    name = "ssc018-image-" + secrets.token_hex(4)
    run("run", "-d", "--name", name, *settings(SSC_ENV="prod"), image)
    try:
        assert wait_exit(name, 30) == 1
        assert "dev or test" in run("logs", name).stderr
    finally:
        run("rm", "-f", name, check=False)
