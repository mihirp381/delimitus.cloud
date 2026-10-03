"""SSC-015: the build fixtures (``conformance/build_fixtures``) and what SSC does with each.

Each fixture goes the way ``ssc deploy`` sends it: packed, scanned on the client, then read back
as the server reads the stored bundle (tar check, secret scan, analysis). ``LOCAL`` outcomes end
here; ``LIVE`` ones pass every local check and their outcome needs a build in a cell (the
build step, or the deploy's health check).
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from ssc_bundle.analyze import MAX_ANALYZE_BYTES, Analysis, analyze
from ssc_bundle.client import SecretFoundError, prepare
from ssc_bundle.limits import DEFAULT_LIMITS
from ssc_bundle.pack import pack
from ssc_bundle.secrets import MAX_SCAN_BYTES, allowed_values, scan
from ssc_bundle.tarcheck import inspect, iter_entries, iter_files
from ssc_contracts.manifest import default_manifest, load_manifest

FIXTURES = Path(__file__).resolve().parents[3] / "conformance" / "build_fixtures"
LOCAL = "runs locally"
LIVE = "needs a live build"


@dataclass(frozen=True)
class Expected:
    delimitus: str
    outcome: str
    where: str
    framework: str | None = None
    notices: tuple[str, ...] = ()


EXPECTED = {
    "cs-static-html": Expected("must-succeed", "builds", LIVE),
    "cs-express-hello": Expected("must-succeed", "builds", LIVE),
    "cs-fastapi-hello": Expected("must-succeed", "builds", LIVE, notices=("DOCKERFILE_IGNORED",)),
    "cs-vite-app": Expected("must-succeed", "builds", LIVE),
    "cs-flask-hello": Expected(
        "must-succeed", "SECRET_IN_BUNDLE", LOCAL, notices=("DOCKERFILE_IGNORED",)
    ),
    "cs-notebook-trivial": Expected("must-succeed", "BUILD_NO_ENTRYPOINT", LOCAL),
    "cf-private-registry": Expected("must-fail", "BUILD_PRIVATE_REGISTRY", LOCAL),
    "cf-unresolvable-dep": Expected("must-fail", "BUILD_DEPENDENCY_UNRESOLVED", LIVE),
    "cf-exits-nonzero": Expected(
        "must-fail", "HEALTH_CHECK_FAILED", LIVE, notices=("DOCKERFILE_IGNORED",)
    ),
    "cf-no-port-bound": Expected(
        "must-fail", "HEALTH_CHECK_FAILED", LIVE, notices=("DOCKERFILE_IGNORED",)
    ),
    "cf-startup-hang": Expected(
        "must-fail", "HEALTH_CHECK_FAILED", LIVE, notices=("DOCKERFILE_IGNORED",)
    ),
    "cf-boots-serves-500": Expected(
        "must-fail", "HEALTH_CHECK_FAILED", LIVE, notices=("DOCKERFILE_IGNORED",)
    ),
    "cn-adopt-supabase-migrations": Expected("live", "builds", LIVE),
    "cn-preexisting-connstring": Expected("must-not-adopt", "builds", LIVE),
    "cn-shared-warehouse-tables": Expected(
        "must-not-adopt", "BUILD_NO_ENTRYPOINT", LOCAL, framework="streamlit"
    ),
    "sqlite-on-disk": Expected("ssc", "STATE_SQLITE_EPHEMERAL", LOCAL),
    "dash-app": Expected("ssc", "builds", LIVE, framework="dash"),
}
DELIMITUS = sorted(n for n, e in EXPECTED.items() if e.delimitus != "ssc")
BUILD_FAILURES = {"BUILD_DEPENDENCY_UNRESOLVED", "HEALTH_CHECK_FAILED", "builds"}


def server_side(bundle: Path) -> tuple[bool, Analysis]:
    """What the control plane finds in the stored bundle: a blocking secret, and the analysis."""
    with bundle.open("rb") as f:
        report = inspect(f, DEFAULT_LIMITS)
        manifest = (
            default_manifest()
            if report.manifest_text is None
            else load_manifest(report.manifest_text)
        )
        f.seek(0)
        found = scan(iter_files(f, DEFAULT_LIMITS, MAX_SCAN_BYTES), allowed_values(manifest))
        f.seek(0)
        analysis = analyze(iter_entries(f, DEFAULT_LIMITS, MAX_ANALYZE_BYTES), manifest)
    return any(x.blocking for x in found), analysis


def test_every_fixture_has_an_expected_outcome() -> None:
    assert sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()) == sorted(EXPECTED)
    assert len(DELIMITUS) == 15


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_outcome(name: str, tmp_path: Path) -> None:
    expected = EXPECTED[name]
    dest = tmp_path / "bundle.tar.gz"
    if expected.outcome == "SECRET_IN_BUNDLE":
        with pytest.raises(SecretFoundError):
            prepare(FIXTURES / name, dest)
        pack(FIXTURES / name, dest)
        blocked, analysis = server_side(dest)
        assert blocked
    else:
        prepare(FIXTURES / name, dest)
        blocked, analysis = server_side(dest)
        assert not blocked
        refused = None if analysis.refusal is None else analysis.refusal.code
        if expected.outcome in BUILD_FAILURES:
            assert refused is None, analysis.refusal
        else:
            assert refused == expected.outcome, analysis
    assert analysis.framework == expected.framework
    assert analysis.notices == expected.notices
    assert (expected.where == LOCAL) is (expected.outcome not in BUILD_FAILURES)
