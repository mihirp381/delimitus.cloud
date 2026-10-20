"""A stop that arrives while the entry point still imports ends the process cleanly, without
starting Envoy. In the image the process is the container's first one, which ignores a signal it
has no handler for, so before the early handler such a stop waited out the unit's stop timeout
and ended in ``SIGKILL`` (exit 137); outside a container the same stop killed it (-15)."""

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from egress_world import ORG

UNIT_STOP_SECONDS = 15  # the cell unit's ``docker stop -t 15``
# The first module the entry point imports after its early handler and before the slow ones
# (the storage client): ``-X importtime`` prints its line once it is in.
FIRST_IMPORT = "ssc_contracts.egress"


@pytest.mark.parametrize("attempt", range(5))  # a race before the fix; it must hold every time
def test_a_stop_during_the_imports_exits_zero_without_starting_envoy(
    tmp_path: Path, attempt: int
) -> None:
    del attempt
    envoy = tmp_path / "envoy"  # never started; there only so a late stop would not fail on it
    envoy.write_text("#!/bin/sh\nexec sleep 60\n")
    envoy.chmod(0o755)
    env = os.environ | {
        "SSC_ORG_ID": ORG,
        "SSC_CELL_BUCKET": "ssc-c-test-cell",
        "STORAGE_EMULATOR_HOST": "http://127.0.0.1:1",
        "SSC_ENVOY_BIN": str(envoy),
        "SSC_PROXY_PORT": "0",
    }
    child = subprocess.Popen(  # noqa: S603  (this interpreter and our own module)
        [sys.executable, "-X", "importtime", "-m", "ssc_egress"],
        cwd=tmp_path,
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: list[str] = []
    importing = threading.Event()

    def read() -> None:
        assert child.stderr is not None
        for line in child.stderr:
            lines.append(line)
            if line.rstrip().endswith(" " + FIRST_IMPORT):
                importing.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert importing.wait(30), "".join(lines[-20:])
        child.send_signal(signal.SIGTERM)
        code = child.wait(timeout=UNIT_STOP_SECONDS)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    reader.join(5)
    log = "".join(line for line in lines if not line.startswith("import time:"))
    assert code == 0, log
    assert "stopped before the proxy started" in log


@pytest.mark.parametrize("value", ["soon", "0", "-5", "nan"])
def test_a_snapshot_max_age_that_is_not_a_positive_number_is_a_settings_error(
    tmp_path: Path, value: str
) -> None:
    env = os.environ | {
        "SSC_ORG_ID": ORG,
        "SSC_CELL_BUCKET": "ssc-c-test-cell",
        "SSC_SNAPSHOT_MAX_AGE": value,
        "SSC_ENVOY_BIN": str(tmp_path / "no-envoy"),
    }
    done = subprocess.run(  # noqa: S603  (this interpreter and our own module)
        [sys.executable, "-m", "ssc_egress"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 2, done.stderr  # noqa: PLR2004
    assert "SSC_SNAPSHOT_MAX_AGE" in done.stderr
