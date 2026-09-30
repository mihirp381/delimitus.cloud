"""The three done-when tools, without a cloud."""

import subprocess
from pathlib import Path

import pytest

from ssc_infra import deny_probe, run, snapshot_rtt
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.clock import SystemClock


def test_gcloud_refuses_the_live_delimitus_project() -> None:
    with pytest.raises(run.CommandError):
        run.gcloud("projects", "describe", run.FORBIDDEN_PROJECT)


def _probe(
    monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, list[tuple[int, str]]]
) -> list[str]:
    calls: list[str] = []

    def fake(
        *args: str, ok_codes: tuple[int, ...], quiet: bool
    ) -> subprocess.CompletedProcess[str]:
        assert quiet, "a secret value must never be captured"
        identity = next(a for a in args if a.startswith("--impersonate-service-account="))
        account = identity.split("=", 1)[1].split("@")[0]
        calls.append(account)
        code, err = outcomes[account].pop(0) if len(outcomes[account]) > 1 else outcomes[account][0]
        return subprocess.CompletedProcess(list(args), code, None, err)

    monkeypatch.setattr(deny_probe, "gcloud", fake)
    return calls


def test_the_probe_waits_for_the_allowed_read_then_expects_a_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _probe(
        monkeypatch,
        {
            "ssc-a-probe": [(1, "ERROR: PERMISSION_DENIED"), (0, "")],
            "ssc-deny-probe": [
                (1, "ERROR: (gcloud.secrets.versions.access) PERMISSION_DENIED: denied")
            ],
        },
    )
    allowed, denied = deny_probe.run("testcell01", deadline=float("inf"), sleep=0)
    assert calls == ["ssc-a-probe", "ssc-a-probe", "ssc-deny-probe"]
    assert allowed.read
    assert not denied.read and deny_probe.is_denial(denied.error)


def test_the_probe_fails_when_the_denied_identity_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    _probe(monkeypatch, {"ssc-a-probe": [(0, "")], "ssc-deny-probe": [(0, "")]})
    assert deny_probe.main(["testcell01"]) == 1


def test_the_probe_passes_only_on_a_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _probe(monkeypatch, {"ssc-a-probe": [(0, "")], "ssc-deny-probe": [(1, "ERROR: NOT_FOUND")]})
    assert deny_probe.main(["testcell01"]) == 1


async def test_the_round_trip_is_measured_and_cleaned_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snapshot_rtt.secrets, "randbelow", lambda _n: 0)
    signer = UrlSigner({"k1": b"k" * 32}, active="k1", clock=SystemClock())
    store = FsBlobStore(tmp_path, signer=signer, base_url="http://blobs.test")
    times = await snapshot_rtt.measure(store, 2)
    assert len(times) == 2
    assert max(times) < snapshot_rtt.LIMIT_SECONDS
    assert [info async for info in store.list("snapshots/")] == []
