"""The nightly run, end to end against the Cloud Run emulator and a scripted probe job."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

from ssc_agent.cloud_run import CellRuntime, CloudRunDriver
from ssc_conformance import nightly
from ssc_conformance.cloud_run_emulator import PROJECT, REGION, CloudRunEmulator
from ssc_conformance.runtime_probes import PROBES
from ssc_shared.runtime import RuntimeDriver, ServiceObservation, ServiceSpec

DIGEST = "sha256:" + "7" * 64
CELL = CellRuntime(
    project=PROJECT,
    region=REGION,
    network=f"projects/{PROJECT}/global/networks/ssc-cell",
    subnetwork=f"projects/{PROJECT}/regions/{REGION}/subnetworks/apps",
    image_repository=f"{REGION}-docker.pkg.dev/{PROJECT}/ssc-apps/apps",
    invoker=f"ssc-gateway@{PROJECT}.iam.gserviceaccount.com",
)
JOB = f"projects/{PROJECT}/locations/{REGION}/jobs/{nightly.PROBE_JOB}"
EXECUTION = f"{JOB}/executions/ssc-probe-runner-x7k2p"


class Clock:
    def __init__(self, emulator: CloudRunEmulator) -> None:
        self.now = 0.0
        self.emulator = emulator

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.emulator.settle()


@pytest.fixture
def emulator() -> CloudRunEmulator:
    return CloudRunEmulator()


@pytest.fixture
def clock(emulator: CloudRunEmulator) -> Clock:
    return Clock(emulator)


@pytest.fixture
async def driver(emulator: CloudRunEmulator, clock: Clock) -> AsyncIterator[CloudRunDriver]:
    async def token() -> str:
        return "access-token"

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(emulator.handler))
    cloud_run = CloudRunDriver(CELL, token, client=client, sleep=clock.sleep)
    yield cloud_run
    await cloud_run.aclose()


def _results(failed: str | None = None) -> list[dict[str, str]]:
    return [
        {"probe": p, "status": "failed" if p == failed else "passed", "reason": "r"} for p in PROBES
    ]


class ScriptedJob:
    """The Run and Logging APIs as the job's run appears to the nightly account."""

    def __init__(self, results: list[dict[str, str]]) -> None:
        self.results = results
        self.calls: list[httpx2.Request] = []
        self.execution_polls = 0
        self.log_polls = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(request)
        url = str(request.url)
        if url == f"{nightly.RUN_API}/{JOB}:run":
            return httpx2.Response(200, json={"name": "op", "metadata": {"name": EXECUTION}})
        if url == f"{nightly.RUN_API}/{EXECUTION}":
            self.execution_polls += 1
            done = self.execution_polls > 2
            body = {"createTime": "2026-10-01T05:39:00Z"}
            return httpx2.Response(200, json=body | ({"completionTime": "t"} if done else {}))
        if url == f"{nightly.LOGGING_API}/entries:list":
            self.log_polls += 1
            if self.log_polls == 1:
                return httpx2.Response(200, json={})
            if "pageToken" not in json.loads(request.content):
                return httpx2.Response(200, json={"nextPageToken": "p2"})
            lines: list[dict[str, Any]] = [{"ssc_probe": r} for r in self.results]
            lines.append({"ssc_probe_summary": {"total": len(self.results)}})
            entries = [{"jsonPayload": line} for line in lines] + [{"textPayload": "boot"}]
            return httpx2.Response(200, json={"entries": entries})
        return httpx2.Response(404)


def _job(script: ScriptedJob, clock: Clock) -> nightly.ProbeJob:
    async def token() -> str:
        return "nightly-access-token"

    return nightly.ProbeJob(
        PROJECT,
        token,
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(script.handler)),
        sleep=clock.sleep,
        clock=clock,
    )


def test_config_needs_every_variable() -> None:
    full = {name: "x" for name in nightly.ENV.values()}
    assert nightly.config_from_env(full).control_sa is None
    assert nightly.config_from_env(full | {"SSC_CONTROL_SA": "sa"}).control_sa == "sa"
    for name in nightly.ENV.values():
        with pytest.raises(nightly.NightlyError, match=name):
            nightly.config_from_env({k: v for k, v in full.items() if k != name})


def test_probe_apps_are_the_cell_runner_targets() -> None:
    a, b = (nightly.probe_spec(env, DIGEST) for env in nightly.PROBE_ENVS)
    assert (a.service, b.service) == ("ssc-a-probe00000000000000a", "ssc-a-probe00000000000000b")
    assert a.env == {"PORT": "8080", "HOME": "/tmp"}  # noqa: S108
    assert (a.resource_class, a.min_instances, a.max_instances) == ("small", 0, 1)


def test_reconciler_tick() -> None:
    assert nightly.TICK_SECONDS == 15


async def test_nightly_deploys_probes_and_times_the_drift(
    driver: CloudRunDriver, emulator: CloudRunEmulator, clock: Clock
) -> None:
    script = ScriptedJob(_results())
    report = await nightly.nightly(
        driver, _job(script, clock), DIGEST, sleep=clock.sleep, clock=clock
    )

    assert report.failures == []
    assert [r["probe"] for r in report.results] == list(PROBES)
    assert report.drift_seconds is not None
    assert nightly.TICK_SECONDS <= report.drift_seconds <= nightly.DRIFT_LIMIT_SECONDS
    for env in nightly.PROBE_ENVS:
        spec = nightly.probe_spec(env, DIGEST)
        seen = await driver.observe(spec.service)
        assert seen is not None
        live = [r for r in seen.revisions if r.traffic_percent == 100]
        assert [r.spec_fingerprint for r in live] == [spec.spec_fingerprint]
        assert emulator.policy(spec.service)
    assert {r.headers["Authorization"] for r in script.calls} == {"Bearer nightly-access-token"}
    assert script.calls[0].method == "POST"
    logged = json.loads(script.calls[-1].content)
    assert logged["resourceNames"] == [f"projects/{PROJECT}"]
    assert 'labels."run.googleapis.com/execution_name"="ssc-probe-runner-x7k2p"' in logged["filter"]
    assert logged["filter"].endswith('timestamp>="2026-10-01T05:39:00Z"')
    assert logged["pageToken"] == "p2"
    markdown = report.markdown()
    assert "| sse_passthrough | passed | r |" in markdown
    assert f"Drift repaired in: {report.drift_seconds:.0f} s (limit 60 s)" in markdown


async def test_failed_probe_fails_the_night(driver: CloudRunDriver, clock: Clock) -> None:
    script = ScriptedJob(_results(failed="no_dns_exfil")[1:])
    report = await nightly.nightly(
        driver, _job(script, clock), DIGEST, sleep=clock.sleep, clock=clock
    )
    assert report.failures == ["non_root_10001: did not report", "no_dns_exfil: r"]
    assert "- FAILED no_dns_exfil: r" in report.markdown()


def test_verdict_refuses_duplicates_and_strangers() -> None:
    results = [*_results(), {"probe": "health_path", "status": "passed"}, {"probe": "extra"}]
    assert nightly.verdict(results) == ["extra: unknown probe", "health_path: reported 2 times"]


class IgnoresTraffic:
    """A runtime whose traffic cannot be moved back: the drift is never repaired."""

    def __init__(self, inner: RuntimeDriver) -> None:
        self.inner = inner
        self.frozen = False

    async def apply(self, spec: ServiceSpec) -> str:
        return await self.inner.apply(spec)

    async def set_traffic(self, service: str, revision: str) -> None:
        if not self.frozen:
            await self.inner.set_traffic(service, revision)
        self.frozen = True

    async def scale_to_zero(self, service: str) -> None:
        await self.inner.scale_to_zero(service)

    async def observe(self, service: str) -> ServiceObservation | None:
        return await self.inner.observe(service)


async def test_unrepaired_drift_fails_after_a_minute(driver: CloudRunDriver, clock: Clock) -> None:
    spec = nightly.probe_spec(nightly.PROBE_ENVS[0], DIGEST)
    await nightly.converge(driver, spec, every=5, limit=600, sleep=clock.sleep, clock=clock)
    stuck = IgnoresTraffic(driver)
    with pytest.raises(nightly.NightlyError, match="not converged after 75 s"):
        await nightly.drift(stuck, spec, sleep=clock.sleep, clock=clock)


def test_main_writes_the_step_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = nightly.Report(_results(failed="sse_passthrough"), 15.0, ["sse_passthrough: r"])

    async def fake(_: object) -> nightly.Report:
        return report

    summary = tmp_path / "summary.md"
    monkeypatch.setattr(nightly, "main_async", fake)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert nightly.main() == 1
    assert "FAILED sse_passthrough" in capsys.readouterr().out
    assert summary.read_text().startswith("## SSC-017 runtime probes")


def test_main_reports_configuration_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in nightly.ENV.values():
        monkeypatch.delenv(name, raising=False)
    assert nightly.main() == 1
    assert "missing SSC_PROBE_PROJECT" in capsys.readouterr().err
