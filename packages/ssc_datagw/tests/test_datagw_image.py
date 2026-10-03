"""The data gateway image (SSC-050): built from the repository root, it starts with no readable
snapshot, answers its health check and its own ``401`` to a caller without a workload token,
stops cleanly, and exits when its settings are wrong. Without Docker these tests skip locally
and fail in CI."""

import json
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from datagw_world import ENV

ROOT = Path(__file__).resolve().parents[3]


def docker() -> str:
    found = shutil.which("docker")
    if (
        found is None
        or subprocess.run([found, "info"], capture_output=True, check=False).returncode
    ):
        if os.environ.get("CI"):
            pytest.fail("Docker is required in CI for the data gateway image")
        pytest.skip("Docker is not available")
    return found


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    out = subprocess.run([docker(), *args], capture_output=True, text=True, check=False)
    if check and out.returncode:
        raise AssertionError(f"docker {args[0]} failed: {out.stderr[-3000:]}")
    return out


def settings(**changes: str) -> list[str]:
    env = {
        **ENV,
        "STORAGE_EMULATOR_HOST": "http://127.0.0.1:1",
        "GOOGLE_CLOUD_PROJECT": ENV["SSC_PROJECT_ID"],
    }
    env.update(changes)
    return [arg for k, v in env.items() if v for arg in ("-e", f"{k}={v}")]


@pytest.fixture(scope="module")
def image(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    iid = tmp_path_factory.mktemp("image") / "iid"
    run(
        "build", "--quiet", "--iidfile", str(iid), "-f", "packages/ssc_datagw/Dockerfile", str(ROOT)
    )
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
    name = "ssc050-image-" + secrets.token_hex(4)
    run("run", "-d", "--name", name, "-p", "127.0.0.1::8080", *settings(), image)
    try:
        port = run("port", name, "8080/tcp").stdout.splitlines()[0].rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while True:
            try:
                health = httpx2.get(f"{base}/healthz", timeout=2)
                break
            except httpx2.HTTPError:
                if time.monotonic() > deadline:
                    raise AssertionError(run("logs", name).stderr[-3000:]) from None
                time.sleep(0.3)
        assert health.json() == {"status": "ok"}
        query = httpx2.post(
            f"{base}/v1/connections/sales/query", json={"sql": "select 1"}, timeout=5
        )
        assert query.status_code == 401
        assert query.json()["error"]["code"] == "UNAUTHENTICATED"
        assert top_has(name, "ssc_datagw.server")
        logs = run("logs", name).stdout + run("logs", name).stderr
        assert "no snapshot at start" in logs
        assert "select 1" not in logs
        (line,) = [ln for ln in logs.splitlines() if "datagw query " in ln]
        record = json.loads(line.split("datagw query ", 1)[1])
        assert (record["outcome"], record["cold"]) == ("UNAUTHENTICATED", True)
        started = time.monotonic()
        run("stop", "-t", "30", name)
        assert wait_exit(name, 5) == 0
        assert time.monotonic() - started < 5
    finally:
        run("rm", "-f", name, check=False)


def top_has(name: str, needle: str) -> bool:
    return needle in run("top", name).stdout


def test_the_container_exits_when_its_settings_are_wrong(image: str) -> None:
    name = "ssc050-image-" + secrets.token_hex(4)
    run("run", "-d", "--name", name, *settings(SSC_DATAGW_AUDIENCE="http://plain"), image)
    try:
        assert wait_exit(name, 30) == 1
        assert "SSC_DATAGW_AUDIENCE must be an https URL" in run("logs", name).stderr
    finally:
        run("rm", "-f", name, check=False)
