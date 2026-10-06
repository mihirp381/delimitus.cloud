"""SSC-017 runtime probes against the probe image in local Docker.

The four local probes pass; the ten cell probes report ``skipped``, never ``passed``; and broken
images make the gates fire: one that runs as root fails ``non_root_10001``, one that ignores
``PORT`` fails ``listens_on_PORT``. Without Docker these tests skip locally and fail in CI.
"""

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx2
import pytest

from ssc_conformance.runtime_probes import (
    CELL_PROBES,
    LOCAL_PROBES,
    PROBES,
    STAGING_CELL,
    ProbeResult,
    run,
)

PROBE_APP = Path(__file__).resolve().parents[1] / "runtime" / "probe_app"
PORT = "8321"  # inside the container only; the host port is picked by Docker


def docker() -> str:
    found = shutil.which("docker")
    if (
        found is None
        or subprocess.run([found, "info"], capture_output=True, check=False).returncode
    ):
        if os.environ.get("CI"):
            pytest.fail("Docker is required in CI for the runtime probes")
        pytest.skip("Docker is not available")
    return found


def build(context: Path, iidfile: Path) -> str:
    subprocess.run(
        [docker(), "build", "--quiet", "--iidfile", str(iidfile), str(context)],
        check=True,
        capture_output=True,
    )
    return iidfile.read_text().strip()


@contextmanager
def serve(image: str) -> Iterator[str]:
    """Run ``image`` as the platform would and yield its base URL."""
    cmd = docker()
    started = subprocess.run(
        [
            cmd,
            "run",
            "--detach",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--env",
            f"PORT={PORT}",
            "--env",
            "HOME=/tmp",
            "--publish",
            f"127.0.0.1::{PORT}",
            image,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    container = started.stdout.strip()
    try:
        mapped = subprocess.run(
            [cmd, "port", container, f"{PORT}/tcp"], check=True, capture_output=True, text=True
        ).stdout.split()[0]
        base = f"http://{mapped}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                httpx2.get(base + "/probe/uid", timeout=1.0)
                break
            except httpx2.HTTPError:
                time.sleep(0.2)
        yield base
    finally:
        subprocess.run([cmd, "rm", "--force", container], capture_output=True, check=False)


def variant(tmp: Path, edit_dockerfile: object = None, edit_app: object = None) -> Path:
    shutil.copytree(PROBE_APP, tmp, dirs_exist_ok=True)
    for name, edit in (("Dockerfile", edit_dockerfile), ("app.py", edit_app)):
        if callable(edit):
            path = tmp / name
            path.write_text(edit(path.read_text()))
    return tmp


@pytest.fixture(scope="module")
def probe_image(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    image = build(PROBE_APP, tmp_path_factory.mktemp("iid") / "iid")
    yield image
    subprocess.run([docker(), "rmi", "--force", image], capture_output=True, check=False)


def by_name(results: list[ProbeResult]) -> dict[str, ProbeResult]:
    assert [r.name for r in results] == list(PROBES)
    return {r.name: r for r in results}


def test_probe_list_is_seventeen_with_four_local() -> None:
    assert len(PROBES) == len(set(PROBES)) == 17
    assert len(LOCAL_PROBES) == 4 and len(CELL_PROBES) == 13
    assert {"cannot_reach_peer_cell", "deny_peer_cell", "datagw_read_only"} <= set(CELL_PROBES)


def test_probe_image_passes_the_local_probes(probe_image: str) -> None:
    with serve(probe_image) as base:
        results = by_name(run(base))
        for name in LOCAL_PROBES:
            assert results[name].status == "passed", results[name]
        for name in CELL_PROBES:
            assert results[name] == ProbeResult(name, "skipped", STAGING_CELL)


def test_root_image_fails_non_root(probe_image: str, tmp_path: Path) -> None:
    context = variant(tmp_path / "root", edit_dockerfile=lambda s: s.replace("USER 10001\n", ""))
    assert "USER" not in (context / "Dockerfile").read_text()
    image = build(context, tmp_path / "iid")
    try:
        with serve(image) as base:
            results = by_name(run(base))
            assert results["non_root_10001"].status == "failed"
            assert "uid 0" in results["non_root_10001"].reason
            # Even root cannot write outside memory: the read-only root filesystem refuses it.
            assert results["no_write_outside_memory"].status == "passed"
    finally:
        subprocess.run([docker(), "rmi", "--force", image], capture_output=True, check=False)


def test_app_ignoring_port_fails_listens_on_port(probe_image: str, tmp_path: Path) -> None:
    context = variant(
        tmp_path / "port", edit_app=lambda s: s.replace('int(os.environ["PORT"])', "8080")
    )
    image = build(context, tmp_path / "iid")
    try:
        with serve(image) as base:
            results = by_name(run(base))
            assert results["listens_on_PORT"].status == "failed"
            assert all(results[n].status != "passed" for n in LOCAL_PROBES)
    finally:
        subprocess.run([docker(), "rmi", "--force", image], capture_output=True, check=False)


def test_cell_probes_never_pass_whatever_the_app_says() -> None:
    def everything_ok(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"uid": 10001, "home": "/tmp", "writable": {}})

    client = httpx2.Client(
        base_url="http://probe.test", transport=httpx2.MockTransport(everything_ok)
    )
    results = by_name(run("http://probe.test", client=client))
    assert {results[n].status for n in CELL_PROBES} == {"skipped"}
    assert results["no_write_outside_memory"].status == "failed"  # silence is not a refusal
