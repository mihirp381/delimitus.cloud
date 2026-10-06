"""The nightly run, end to end against the Cloud Run emulator and a scripted probe job."""

import asyncio
import datetime
import json
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ssc_agent.cloud_run import CellRuntime, CloudRunDriver
from ssc_conformance import evidence, nightly
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


def test_config_takes_a_peer_cell_whole_or_not_at_all() -> None:
    full = {name: "x" for name in nightly.ENV.values()}
    assert nightly.config_from_env(full).peer_cell is None
    values = ("https://a", "https://g", "10.30.0.0/22", "ssc-c-peer")
    peer = dict(zip(nightly.PEER_CELL_ENV, values, strict=True))
    assert nightly.config_from_env(full | peer).peer_cell == {
        "PROBE_PEER_CELL_APP_URL": "https://a",
        "PROBE_PEER_CELL_GATEWAY_URL": "https://g",
        "PROBE_PEER_CELL_RANGE": "10.30.0.0/22",
        "PROBE_PEER_CELL_PROJECT": "ssc-c-peer",
    }
    with pytest.raises(nightly.NightlyError, match="or none"):
        nightly.config_from_env(full | {"SSC_PROBE_PEER_RANGE": "10.30.0.0/22"})
    with pytest.raises(nightly.NightlyError, match="or none"):
        nightly.config_from_env(full | peer | {"SSC_PROBE_PEER_PROJECT": ""})


CONNECTION = "con_" + "a1b2c3d4e5f6g7h8i9j0"
DATAGW = {
    "SSC_PROBE_DATAGW_URL": "https://datagw.run.app",
    "SSC_PROBE_DATAGW_CONNECTION": CONNECTION,
}


def test_config_takes_an_optional_data_gateway() -> None:
    full = {name: "x" for name in nightly.ENV.values()}
    assert nightly.config_from_env(full).datagw_url is None
    assert nightly.config_from_env(full | {"SSC_PROBE_DATAGW_URL": ""}).datagw_url is None
    named = nightly.config_from_env(full | DATAGW)
    assert (named.datagw_url, named.datagw_connection) == ("https://datagw.run.app", CONNECTION)
    with pytest.raises(nightly.NightlyError, match="both"):
        nightly.config_from_env(full | {"SSC_PROBE_DATAGW_URL": "https://datagw.run.app"})
    with pytest.raises(nightly.NightlyError, match="both"):
        nightly.config_from_env(full | {"SSC_PROBE_DATAGW_CONNECTION": CONNECTION})


def test_probe_apps_are_the_cell_runner_targets() -> None:
    a, b = (nightly.probe_spec(env, DIGEST) for env in nightly.PROBE_ENVS)
    assert (a.service, b.service) == ("ssc-a-probe00000000000000a", "ssc-a-probe00000000000000b")
    assert a.env == {"PORT": "8080", "HOME": "/tmp"}  # noqa: S108
    assert (a.resource_class, a.min_instances, a.max_instances) == ("small", 0, 1)
    assert (a.billing, a.timeout_seconds, a.concurrency) == ("request", 300, 80)


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


def _skipped(probes: frozenset[str], reason: str) -> list[dict[str, str]]:
    return [
        r | {"status": "skipped", "reason": reason} if r["probe"] in probes else r
        for r in _results()
    ]


def _peer_cell_skipped() -> list[dict[str, str]]:
    return _skipped(nightly.PEER_CELL_PROBES, "no peer cell")


async def test_no_peer_cell_is_a_skip_not_a_failure(driver: CloudRunDriver, clock: Clock) -> None:
    script = ScriptedJob(_peer_cell_skipped())
    report = await nightly.nightly(
        driver, _job(script, clock), DIGEST, sleep=clock.sleep, clock=clock
    )
    assert report.failures == []
    assert json.loads(script.calls[0].content) == {}
    assert "| cannot_reach_peer_cell | skipped | no peer cell |" in report.markdown()
    assert "| deny_peer_cell | skipped | no peer cell |" in report.markdown()


async def test_a_named_peer_cell_reaches_the_job_and_must_pass(
    driver: CloudRunDriver, clock: Clock
) -> None:
    script = ScriptedJob(_peer_cell_skipped())
    peer = {"PROBE_PEER_CELL_APP_URL": "https://a", "PROBE_PEER_CELL_RANGE": "10.30.0.0/22"}
    report = await nightly.nightly(
        driver, _job(script, clock), DIGEST, peer_cell=peer, sleep=clock.sleep, clock=clock
    )
    assert report.failures == [
        "cannot_reach_peer_cell: no peer cell",
        "deny_peer_cell: no peer cell",
    ]
    assert json.loads(script.calls[0].content) == {
        "overrides": {
            "containerOverrides": [
                {
                    "env": [
                        {"name": "PROBE_PEER_CELL_APP_URL", "value": "https://a"},
                        {"name": "PROBE_PEER_CELL_RANGE", "value": "10.30.0.0/22"},
                    ]
                }
            ]
        }
    }


async def test_a_data_gateway_reaches_the_job_and_must_pass(
    driver: CloudRunDriver, clock: Clock
) -> None:
    waiting = _skipped(frozenset({"datagw_read_only"}), "waits for the data gateway")
    script = ScriptedJob(waiting)
    report = await nightly.nightly(
        driver,
        _job(script, clock),
        DIGEST,
        sleep=clock.sleep,
        clock=clock,
        datagw_url="https://datagw.run.app",
        datagw_connection=CONNECTION,
    )
    assert report.failures == ["datagw_read_only: waits for the data gateway"]
    env = json.loads(script.calls[0].content)["overrides"]["containerOverrides"][0]["env"]
    assert {e["name"]: e["value"] for e in env} == {
        "PROBE_DATAGW_URL": "https://datagw.run.app",
        "PROBE_DATAGW_CONNECTION": CONNECTION,
    }


async def test_without_a_data_gateway_its_probe_may_wait(
    driver: CloudRunDriver, clock: Clock
) -> None:
    waiting = _skipped(frozenset({"datagw_read_only"}), "waits for the data gateway")
    report = await nightly.nightly(
        driver, _job(ScriptedJob(waiting), clock), DIGEST, sleep=clock.sleep, clock=clock
    )
    assert report.failures == []
    assert "| datagw_read_only | skipped | waits for the data gateway |" in report.markdown()


def test_only_the_named_probes_may_skip_and_only_for_their_own_reason() -> None:
    skipped = [r | {"status": "skipped", "reason": "no peer cell"} for r in _results()]
    failures = nightly.verdict(skipped)
    assert len(failures) == len(PROBES) - len(nightly.PEER_CELL_PROBES)
    assert not any(f.startswith(tuple(nightly.PEER_CELL_PROBES)) for f in failures)
    assert "datagw_read_only: no peer cell" in failures
    waiting = _skipped(nightly.PEER_CELL_PROBES, "waits for the data gateway")
    assert "deny_peer_cell: waits for the data gateway" in nightly.verdict(waiting)
    datagw = _skipped(frozenset({"datagw_read_only"}), "waits for the data gateway")
    assert nightly.verdict(datagw) == []
    assert nightly.verdict(datagw, datagw=True) == ["datagw_read_only: waits for the data gateway"]
    peer = _peer_cell_skipped()
    assert nightly.verdict(peer, peer_cell=True) == [
        "cannot_reach_peer_cell: no peer cell",
        "deny_peer_cell: no peer cell",
    ]


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


def test_config_takes_an_optional_tls_host() -> None:
    full = {
        "SSC_PROBE_PROJECT": "p",
        "SSC_PROBE_AGENT_URL": "https://agent.test",
        "SSC_PROBE_DIGEST": DIGEST,
    }
    assert nightly.config_from_env(full).tls_host is None
    assert nightly.config_from_env(full | {"SSC_PROBE_TLS_HOST": ""}).tls_host is None
    named = nightly.config_from_env(full | {"SSC_PROBE_TLS_HOST": "tls.cell.example"})
    assert named.tls_host == "tls.cell.example"


def days(left: float) -> Any:
    async def read(_host: str) -> float:
        return left

    return read


@pytest.mark.parametrize(("left", "fails"), [(60.0, False), (21.0, False), (20.9, True)])
async def test_a_certificate_under_21_days_fails_the_night(
    driver: CloudRunDriver, clock: Clock, left: float, fails: bool
) -> None:
    script = ScriptedJob(_results())
    report = await nightly.nightly(
        driver,
        _job(script, clock),
        DIGEST,
        sleep=clock.sleep,
        clock=clock,
        tls_host="x.cell.example",
        days_left=days(left),
    )
    if fails:
        assert len(report.failures) == 1
        assert report.failures[0].startswith("certificate: x.cell.example expires in 21 days")
        assert "renewal has failed" in report.failures[0]
    else:
        assert report.failures == []


async def test_a_certificate_that_does_not_verify_fails_the_night() -> None:
    async def refuse(host: str) -> float:
        raise nightly.NightlyError(f"{host} does not verify: SSLCertVerificationError")

    failure = await nightly.certificate_failure("x.cell.example", refuse)
    assert failure == "certificate: x.cell.example does not verify: SSLCertVerificationError"


async def test_without_a_tls_host_no_certificate_is_read(
    driver: CloudRunDriver, clock: Clock
) -> None:
    async def never(_host: str) -> float:
        raise AssertionError("read a certificate")

    script = ScriptedJob(_results())
    report = await nightly.nightly(
        driver, _job(script, clock), DIGEST, sleep=clock.sleep, clock=clock, days_left=never
    )
    assert report.failures == []


def certificate(tmp_path: Path, *, expires: datetime.datetime) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    issued = expires - datetime.timedelta(days=90)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(issued)
        .not_valid_after(expires)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


async def serve_tls(cert_file: Path, key_file: Path) -> asyncio.Server:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)

    async def hold(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    return await asyncio.start_server(hold, "127.0.0.1", 0, ssl=context)


async def test_the_days_left_come_from_the_certificate_the_host_serves(tmp_path: Path) -> None:
    expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=10, hours=12)
    cert_file, key_file = certificate(tmp_path, expires=expires)
    server = await serve_tls(cert_file, key_file)
    port = server.sockets[0].getsockname()[1]
    trusting = ssl.create_default_context(cafile=str(cert_file))
    try:
        left = await nightly.certificate_days_left("localhost", port=port, context=trusting)
        assert 10.4 < left < 10.6
        with pytest.raises(nightly.NightlyError, match="does not verify"):
            await nightly.certificate_days_left(
                "localhost", port=port, context=ssl.create_default_context()
            )
    finally:
        server.close()
        await server.wait_closed()


async def test_the_run_leaves_evidence_for_the_one_page_result(
    driver: CloudRunDriver, clock: Clock
) -> None:
    script = ScriptedJob(_skipped(nightly.PEER_CELL_PROBES, "no peer cell"))
    report = await nightly.nightly(
        driver,
        _job(script, clock),
        DIGEST,
        sleep=clock.sleep,
        clock=clock,
        tls_host="x.cell.example",
        days_left=days(60.0),
    )
    found = report.as_evidence(PROJECT, peer=False)
    assert (found.cell, found.peer) == (PROJECT, False)
    by_proof = {r.proof: r for r in found.results}
    assert [r.proof for r in found.results[: len(PROBES)]] == list(PROBES)
    assert by_proof["deny_peer_cell"] == evidence.Result(
        "deny_peer_cell", "skipped", "no peer cell"
    )
    assert by_proof["sse_passthrough"].status == "pass"
    assert by_proof["drift"].status == "pass"
    assert by_proof["certificate"].status == "pass"


async def test_the_evidence_says_what_failed_and_what_was_not_checked(
    driver: CloudRunDriver, clock: Clock
) -> None:
    script = ScriptedJob(_results())
    report = await nightly.nightly(
        driver, _job(script, clock), DIGEST, sleep=clock.sleep, clock=clock
    )
    by_proof = {r.proof: r for r in report.as_evidence(PROJECT, peer=False).results}
    assert by_proof["certificate"] == evidence.Result(
        "certificate", "skipped", "SSC_PROBE_TLS_HOST not set"
    )
    stuck = nightly.Report(_results(), None, ["drift: x"], (evidence.Result("drift", "fail", "x"),))
    assert stuck.as_evidence(PROJECT, peer=True).results[-1] == evidence.Result(
        "drift", "fail", "x"
    )


def test_main_async_adds_its_results_to_the_evidence_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = nightly.Report(_results(), 15.0, [], (evidence.Result("drift", "pass", "ok"),))

    async def fake(*_args: object, **_kwargs: object) -> nightly.Report:
        return report

    class Closable:
        def __init__(self, *_args: object) -> None:
            pass

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(nightly, "nightly", fake)
    monkeypatch.setattr(nightly, "CellAgentDriver", Closable)
    monkeypatch.setattr(nightly, "ProbeJob", Closable)
    path = tmp_path / "e.json"
    environ = {name: "x" for name in nightly.ENV.values()} | {"SSC_EVIDENCE_FILE": str(path)}
    assert asyncio.run(nightly.main_async(environ)) is report
    written = evidence.read_file(path)
    assert (written.cell, written.peer) == ("x", False)
    assert {r.proof for r in written.results} >= {"drift", "datagw_read_only"}
    path.unlink()
    asyncio.run(nightly.main_async({k: v for k, v in environ.items() if k != "SSC_EVIDENCE_FILE"}))
    assert not path.exists()
