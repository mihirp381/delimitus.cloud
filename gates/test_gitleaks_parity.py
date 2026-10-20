"""The bundle scan and the build's gitleaks agree on every provider key shape (GA-12 F22).

``ssc_bundle``'s test_secrets.py holds the local scan to ``ssc_conformance.secret_shapes``; this
runs gitleaks with the build's own config (``ssc_agent.cloud_build.gitleaks_config``) over the
same lines, so a shape one scan flags and the other does not fails here. It needs the gitleaks
binary on PATH and skips, saying so, without it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ssc_agent.cloud_build import gitleaks_config
from ssc_bundle.secrets import scan
from ssc_conformance.secret_shapes import SHAPES, line

GITLEAKS = shutil.which("gitleaks")

@pytest.mark.skipif(
    GITLEAKS is None, reason="gitleaks is not on PATH: the parity check with the build's scan did not run"
)
def test_gitleaks_flags_every_positive_shape_and_no_placeholder(tmp_path: Path) -> None:
    assert GITLEAKS is not None
    src = tmp_path / "src"
    src.mkdir()
    for n, shape in enumerate(SHAPES):
        (src / f"{n}-positive.js").write_text(line(shape.positive))
        (src / f"{n}-negative.js").write_text(line(shape.negative))
    config = tmp_path / "gitleaks.toml"
    config.write_text(gitleaks_config(()))
    report = tmp_path / "report.json"
    subprocess.run(
        [
            GITLEAKS, "detect", "--no-git", "--no-banner", "--source", str(src),
            "--config", str(config), "--report-format", "json", "--report-path", str(report),
            "--exit-code", "0",
        ],
        check=True,
        capture_output=True,
    )
    flagged = {Path(f["File"]).name for f in json.loads(report.read_text())}
    assert flagged == {f"{n}-positive.js" for n in range(len(SHAPES))}


def test_the_local_scan_flags_the_same_files() -> None:
    files = [(f"{n}-positive.js", line(s.positive).encode()) for n, s in enumerate(SHAPES)]
    files += [(f"{n}-negative.js", line(s.negative).encode()) for n, s in enumerate(SHAPES)]
    assert {f.path for f in scan(files)} == {f"{n}-positive.js" for n in range(len(SHAPES))}
