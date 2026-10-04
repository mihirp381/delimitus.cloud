"""The egress proxy image (SSC-053): built from the repository root and run as the machine runs
it, read-only with memory for ``/tmp``. Before any snapshot it listens and refuses everything;
killing Envoy stops the container non-zero, so its unit restarts it. Without Docker these tests
skip locally and fail in CI."""

import os
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from egress_world import ORG

ROOT = Path(__file__).resolve().parents[3]
KILL_ENVOY = """
import os, signal
for pid in filter(str.isdigit, os.listdir("/proc")):
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        if f.read().split(b"\\0")[0].endswith(b"envoy"):
            os.kill(int(pid), signal.SIGKILL)
"""


def docker() -> str:
    found = shutil.which("docker")
    if (
        found is None
        or subprocess.run([found, "info"], capture_output=True, check=False).returncode
    ):
        if os.environ.get("CI"):
            pytest.fail("Docker is required in CI for the egress proxy image")
        pytest.skip("Docker is not available")
    return found


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    out = subprocess.run([docker(), *args], capture_output=True, text=True, check=False)
    if check and out.returncode:
        raise AssertionError(f"docker {args[0]} failed: {out.stderr[-3000:]}")
    return out


def settings(**changes: str) -> list[str]:
    env = {
        "SSC_ORG_ID": ORG,
        "SSC_CELL_BUCKET": "ssc-c-test-cell",
        "STORAGE_EMULATOR_HOST": "http://127.0.0.1:1",  # no bucket: the feed keeps no view
    }
    env.update(changes)
    return [arg for k, v in env.items() if v for arg in ("-e", f"{k}={v}")]


@pytest.fixture(scope="module")
def image(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    iid = tmp_path_factory.mktemp("image") / "iid"
    run(
        "build", "--quiet", "--iidfile", str(iid), "-f", "packages/ssc_egress/Dockerfile",
        str(ROOT),
    )  # fmt: skip
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


def first_line(port: int) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"CONNECT api.stripe.com:443 HTTP/1.1\r\nHost: api.stripe.com:443\r\n\r\n")
        return sock.recv(64).split(b"\r\n")[0]


def published(name: str) -> int:
    """The host port Docker gave the container's proxy port, read again after a restart."""
    return int(run("port", name, "3128/tcp").stdout.splitlines()[0].rsplit(":", 1)[1])


def wait_answer(port: int, seconds: float) -> bytes:
    """The proxy's first status line once it answers, or ``AssertionError`` after ``seconds``."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            if line := first_line(port):
                return line
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"the proxy did not answer within {seconds} s")
        time.sleep(0.1)


def test_killing_envoy_brings_the_proxy_back_by_itself_and_records_how_long(
    image: str, record_property: Callable[[str, object], None]
) -> None:
    """The machine's unit restarts the container whenever it exits; Docker's ``always`` policy
    stands in for it. The seconds from the kill to the next answer are recorded as the test's
    ``recovery_seconds`` property (``--junitxml``) and printed."""
    name = "ssc053-image-" + secrets.token_hex(4)
    run(
        "run", "-d", "--name", name, "--restart", "always", "--read-only", "--tmpfs", "/tmp",
        "-p", "127.0.0.1::3128", *settings(), image,
    )  # fmt: skip
    try:
        assert wait_answer(published(name), 60) == b"HTTP/1.1 407 Proxy Authentication Required"
        killed = time.monotonic()
        run("exec", name, "python", "-c", KILL_ENVOY)
        deadline = killed + 10
        while run("inspect", "-f", "{{.RestartCount}}", name).stdout.strip() == "0":
            assert time.monotonic() < deadline, "the container did not restart"
            time.sleep(0.1)
        assert wait_answer(published(name), 60) == b"HTTP/1.1 407 Proxy Authentication Required"
        seconds = round(time.monotonic() - killed, 2)
        record_property("recovery_seconds", seconds)
        print(f"egress proxy recovery_seconds={seconds}")  # noqa: T201
        assert seconds < 30  # noqa: PLR2004
    finally:
        run("rm", "-f", name, check=False)


def test_the_image_refuses_everything_until_a_snapshot_and_dies_with_envoy(image: str) -> None:
    name = "ssc053-image-" + secrets.token_hex(4)
    run(
        "run", "-d", "--name", name, "--read-only", "--tmpfs", "/tmp", "-p", "127.0.0.1::3128",
        *settings(), image,
    )  # fmt: skip
    try:
        port = int(run("port", name, "3128/tcp").stdout.splitlines()[0].rsplit(":", 1)[1])
        deadline = time.monotonic() + 60
        while True:
            try:
                line = first_line(port)
                if line:
                    break
                raise ConnectionResetError
            except OSError:
                if time.monotonic() > deadline:
                    raise AssertionError(run("logs", name).stderr[-3000:]) from None
                time.sleep(0.3)
        assert line == b"HTTP/1.1 407 Proxy Authentication Required"
        top = run("top", name).stdout
        assert "ssc_egress" in top
        assert "envoy -c" in top
        assert "--drain-time-s" in top
        run("exec", name, "python", "-c", KILL_ENVOY)
        assert wait_exit(name, 10) == 1
    finally:
        run("rm", "-f", name, check=False)


def test_the_image_stops_cleanly_on_sigterm(image: str) -> None:
    name = "ssc053-image-" + secrets.token_hex(4)
    run("run", "-d", "--name", name, "--read-only", "--tmpfs", "/tmp", *settings(), image)
    try:
        time.sleep(2)
        started = time.monotonic()
        run("stop", "-t", "30", name)
        assert wait_exit(name, 5) == 0
        assert time.monotonic() - started < 10  # noqa: PLR2004
    finally:
        run("rm", "-f", name, check=False)


def test_the_container_exits_without_its_settings(image: str) -> None:
    name = "ssc053-image-" + secrets.token_hex(4)
    run("run", "-d", "--name", name, *settings(SSC_ORG_ID=""), image)
    try:
        assert wait_exit(name, 30) == 2  # noqa: PLR2004
        assert "SSC_ORG_ID is required" in run("logs", name).stderr
    finally:
        run("rm", "-f", name, check=False)
