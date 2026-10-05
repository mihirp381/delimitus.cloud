import argparse
from typing import Any

import pytest
from conftest import FakeHttp, FakeRun, answer, ok

from proofrun import t3, t4, t5
from proofrun.common import CookieJar, Done

GATEWAY_MIN0 = {"spec": {"template": {"metadata": {"annotations": {}}, "spec": {}}}}
GATEWAY_MIN1 = {"metadata": {"annotations": {"run.googleapis.com/minScale": "1"}}, **GATEWAY_MIN0}


def rows(n_passed: int, extra: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    out = [{"probe": f"p{i}", "status": "passed", "reason": "ok"} for i in range(n_passed)]
    return out + (extra or [])


def test_t3_verdict_needs_fourteen_and_a_zero_minimum() -> None:
    results = rows(13, [{"probe": "health_path", "status": "passed", "reason": "200"}])
    good = t3.verdict(results, True, "min 0", "through the public host")
    assert good.passed is True
    assert good.number == "14/14 probes passed through the public host, gateway min 0"
    assert "yes" in good.lines[-1]
    assert t3.verdict(results, False, "min 1", "x").passed is False
    short = t3.verdict(
        rows(13, [{"probe": "egress", "status": "failed", "reason": "r"}]), True, "", "x"
    )
    assert short.passed is False
    assert short.data["failed"] == ["egress: failed: r"]
    peer = {"probe": "cannot_reach_peer_cell", "status": "skipped", "reason": "no peer"}
    assert t3.verdict([*results, peer], True, "", "x").passed is True


def test_gateway_min_reads_template_and_service() -> None:
    run = FakeRun([(["describe", "ssc-gateway"], ok(GATEWAY_MIN0))])
    assert t3.gateway_min(run, "ssc-c-cellone01")[0] is True
    run = FakeRun([(["describe", "ssc-gateway"], ok(GATEWAY_MIN1))])
    assert t3.gateway_min(run, "ssc-c-cellone01")[0] is False


def nightly_args(**kw: str | None) -> argparse.Namespace:
    base = {
        "project": "ssc-c-cellone01",
        "agent_url": "https://agent",
        "digest": "sha256:" + "0" * 64,
    }
    return argparse.Namespace(**{**base, **kw})


def test_nightly_env_drops_peer_settings_and_requires_its_own() -> None:
    env = t3.nightly_env(nightly_args(), {"PATH": "/bin", "SSC_PROBE_PEER_RANGE": "10.0.0.0/8"})
    assert env["SSC_PROBE_PROJECT"] == "ssc-c-cellone01"
    assert "SSC_PROBE_PEER_RANGE" not in env
    assert env["PATH"] == "/bin"
    with pytest.raises(SystemExit):
        t3.nightly_env(nightly_args(digest=None), {})


def nightly_table(probe_rows: list[dict[str, str]]) -> str:
    body = "\n".join(f"| {r['probe']} | {r['status']} | {r['reason']} |" for r in probe_rows)
    return f"| probe | status | reason |\n| --- | --- | --- |\n{body}\nDrift repaired in: none\n"


def test_t3_nightly_runs_the_existing_nightly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSC_PROBE_PEER_APP_URL", "https://peer")
    table = nightly_table(rows(14))
    run = FakeRun(
        [
            (["ssc_conformance.nightly"], Done(0, table, "")),
            (["describe", "ssc-gateway"], ok(GATEWAY_MIN0)),
        ]
    )
    out = t3.run(argparse.Namespace(step="nightly", **vars(nightly_args())), run)
    assert out.passed is True
    env = run.envs[0]
    assert env is not None
    assert "SSC_PROBE_PEER_APP_URL" not in env
    assert any("restore the schedule" in line for line in out.lines)


def test_t3_nightly_with_no_table_is_incomplete() -> None:
    run = FakeRun([(["ssc_conformance.nightly"], Done(1, "", "boom\nERROR: no token"))])
    out = t3.run(argparse.Namespace(step="nightly", **vars(nightly_args())), run)
    assert out.passed is None
    assert out.lines == ["ERROR: no token"]


class FakeRunner:
    """Stands in for the conformance runner: records what it was asked to probe."""

    class checks:  # noqa: N801  (mirrors the runner's module attribute)
        APP_CREDENTIAL = "Bearer probe"

    class Probe:
        def __init__(self, base: str, token: str) -> None:
            self.base = base
            self.headers: dict[str, str] = {}

    def __init__(self) -> None:
        self.asked: list[Any] = []

    def run(self, probe: Any, peer_url: str, health: str, peer_cell: Any, hosts: Any) -> list[Any]:
        self.asked.append((probe, peer_url, health, peer_cell, hosts))
        return rows(14)


def test_t3_public_goes_through_the_public_host_with_the_cookie() -> None:
    env_a = {
        "id": "env_" + "a" * 20,
        "name": "preview",
        "url": "https://proba.cellone01.example.com",
    }
    env_b = {
        "id": "env_" + "b" * 20,
        "name": "preview",
        "url": "https://probb.cellone01.example.com",
    }
    run = FakeRun(
        [
            (["status", "proba"], ok({"environments": [env_a]})),
            (["status", "probb"], ok({"environments": [env_b]})),
        ]
    )
    CookieJar().put("proba.cellone01.example.com", "v1." + "c" * 30, "browser")
    runner = FakeRunner()
    args = argparse.Namespace(
        app="proba", peer_app="probb", env="preview", project_number="42", egress_host=["x.org"]
    )
    t3.public_results(args, run, runner)
    probe, peer_url, health, peer_cell, hosts = runner.asked[0]
    assert probe.base == env_a["url"]
    assert probe.headers["Cookie"].startswith("__Host-ssc-session=v1.")
    assert probe.headers["Authorization"] == "Bearer probe"
    assert peer_url == "https://ssc-a-" + "b" * 20 + "-42.us-central1.run.app"
    assert (health, peer_cell, hosts) == ("/health", None, ("x.org",))


REFUSED = (
    "app by name: {'blocked': True, 'error': 'timed out'}; "
    "app by name with ID token: HTTP 404; "
    "gateway by name: {'blocked': True}; "
    "gateway by Google VIP with ID token: HTTP 404; "
    "tcp 10.20.0.5:8080: {'blocked': True}; "
    "range leg not applicable: same range, separate networks"
)


def test_t4_passes_on_network_and_ingress_refusals_only() -> None:
    out = t4.verdict([{"probe": "cannot_reach_peer_cell", "status": "passed", "reason": REFUSED}])
    assert out.passed is True
    assert out.number == "app: 1 ingress, 1 network; gateway: 1 ingress, 1 network"
    assert any("not applicable" in line for line in out.lines)


def test_t4_fails_when_iam_was_reached() -> None:
    reason = "the peer cell let calls through: app by name with ID token: HTTP 403"
    out = t4.verdict([{"probe": "cannot_reach_peer_cell", "status": "failed", "reason": reason}])
    assert out.passed is False
    assert "1 iam" in out.number
    assert t4.verdict([]).passed is None


def test_t4_points_the_nightly_at_cell_two() -> None:
    env = t4.peer_env("77", "10.20.0.0/24")
    assert (
        env["SSC_PROBE_PEER_APP_URL"] == "https://ssc-a-probe00000000000000a-77.us-central1.run.app"
    )
    assert env["SSC_PROBE_PEER_GATEWAY_URL"] == "https://ssc-gateway-77.us-central1.run.app"
    table = nightly_table(
        [{"probe": "cannot_reach_peer_cell", "status": "passed", "reason": REFUSED}]
    )
    run = FakeRun([(["ssc_conformance.nightly"], Done(0, table, ""))])
    args = argparse.Namespace(
        **vars(nightly_args()), peer_project_number="77", peer_range="10.20.0.0/24"
    )
    assert t4.run(args, run).passed is True
    assert run.envs[0] is not None
    assert run.envs[0]["SSC_PROBE_PEER_RANGE"] == "10.20.0.0/24"


def op(kind: str, start: str, end: str, status: str = "DONE", **extra: str) -> dict[str, str]:
    return {"operationType": kind, "insertTime": start, "endTime": end, "status": status, **extra}


def test_t5_ops_times_the_instance_and_ten_databases() -> None:
    ops = [op("CREATE", "2026-10-03T10:00:00Z", "2026-10-03T10:09:00Z")]
    ops += [
        op("CREATE_DATABASE", f"2026-10-03T11:{i:02}:00Z", f"2026-10-03T11:{i:02}:20Z")
        for i in range(10)
    ]
    ops.append(op("CREATE_DATABASE", "2026-10-03T12:00:00Z", "", status="RUNNING"))
    out = t5.ops_verdict(ops, [op("CLONE", "2026-10-03T13:00:00Z", "2026-10-03T13:12:00Z")])
    assert out.passed is True
    assert out.number == "instance 9.0 min, 10 databases, slowest 20.0 s"
    assert out.lines[-1] == "restore clone: 12.0 min"
    slow = [*ops[:10], op("CREATE_DATABASE", "2026-10-03T12:00:00Z", "2026-10-03T12:01:30Z")]
    assert t5.ops_verdict(slow).passed is False
    assert t5.ops_verdict(ops[:5]).passed is False
    assert t5.ops_verdict([]).passed is False


def test_t5_classifies_cross_connects() -> None:
    assert t5.classify({"connected": True}) == "connected"
    assert t5.classify({"connected": False, "sqlstate": "42501"}) == "refused"
    assert t5.classify({"connected": False, "sqlstate": "28P01"}) == "refused"
    assert t5.classify({"connected": False, "sqlstate": "3D000"}) == "missing"
    assert t5.classify({"connected": False, "error": "timeout"}) == "error"


def test_t5_cross_verdict_leaves_the_maintenance_database_out() -> None:
    own = {"connected": True, "database": "app_x"}
    others = {f"app_{i}": {"connected": False, "sqlstate": "42501"} for i in range(9)}
    others["postgres"] = {"connected": True}
    out = t5.cross_verdict(own, others)
    assert out.passed is True
    assert out.number == "cross-connect refused 9/9"
    others["app_0"] = {"connected": False, "sqlstate": "3D000"}
    assert t5.cross_verdict(own, others).passed is False
    assert t5.cross_verdict({"connected": False}, {"app_1": others["app_1"]}).passed is False


def test_t5_cross_asks_the_app_for_each_database() -> None:
    envs = {
        slug: {
            "id": "env_" + letter * 20,
            "name": "preview",
            "url": f"https://{slug}.cell.example.com",
        }
        for slug, letter in (("pg01", "a"), ("pg02", "b"))
    }
    run = FakeRun([(["status", slug], ok({"environments": [e]})) for slug, e in envs.items()])
    CookieJar().put("pg01.cell.example.com", "v1." + "c" * 30, "browser")
    http = FakeHttp(
        [
            ("/db/own", answer(200, {"connected": True, "database": "app_" + "a" * 20})),
            ("name=app_", answer(200, {"connected": False, "sqlstate": "42501"})),
            ("name=postgres", answer(500)),
        ]
    )
    args = argparse.Namespace(step="cross", app="pg01", other=["pg02"], env="preview")
    out = t5.run(args, run, http)
    assert out.passed is True
    assert out.data["kinds"] == {"app_" + "b" * 20: "refused", "postgres": "error"}


def test_a_failed_nightly_reports_its_own_line_not_the_json_after_it() -> None:
    stderr = 'probe app ready\nnightly: POST https://x/jobs/r:run: HTTP 404 {\n  "error": 1\n}\n'
    assert t3.nightly_error(stderr) == "nightly: POST https://x/jobs/r:run: HTTP 404 {"
    assert t3.nightly_error("one\ntwo\n") == "two"
