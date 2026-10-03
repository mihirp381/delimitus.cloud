import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import Clock, FakeHttp, FakeRun, answer, ok

from proofrun import t6, t7
from proofrun.common import CommandError, CookieJar, StateFile

NAT_IP = "34.1.2.3"


def test_envoy_config_fills_hosts_image_and_resolver() -> None:
    image = "envoyproxy/envoy:v1@sha256:" + "e" * 64
    text = t6.envoy_config(("a.example", "b.example"), image, "8.8.8.8")
    assert text.startswith("#cloud-config\n")
    assert (
        '["a.example:443", "b.example:443"]' in text or '"a.example:443", "b.example:443"' in text
    )
    assert image in text
    assert "DNS=8.8.8.8" in text
    assert "@" + "DOMAINS@" not in text
    assert "@" + "IMAGE@" not in text
    assert "@" + "RESOLVER@" not in text


def test_envoy_config_step_writes_a_file(tmp_path: Path) -> None:
    out = tmp_path / "user-data.yaml"
    args = argparse.Namespace(
        step="envoy-config",
        allow=None,
        envoy_image="envoy@sha256:" + "1" * 64,
        resolver="8.8.4.4",
        out=out,
    )
    outcome = t6.run(args, FakeRun())
    assert outcome.passed is True
    assert '"ifconfig.me:443"' in out.read_text()


def test_job_report_reads_json_and_text_payloads() -> None:
    report = {"ip": NAT_IP}
    assert t6.job_report([{"jsonPayload": {"proofrun_egress": report}}]) == report
    text = json.dumps({"proofrun_egress": report})
    assert t6.job_report([{"textPayload": "starting"}, {"textPayload": text}]) == report
    assert t6.job_report([{"textPayload": "nothing"}]) is None


def test_read_report_polls_until_the_log_arrives() -> None:
    entries: list[list[dict[str, Any]]] = [
        [],
        [{"jsonPayload": {"proofrun_egress": {"ip": NAT_IP}}}],
    ]

    def logging(argv: Any, **_: Any) -> Any:
        return ok(entries.pop(0))

    clock = Clock()
    assert t6.read_report(logging, "ssc-c-cellone01", t6.NAT_JOB, "exec-1", clock.sleep) == {
        "ip": NAT_IP
    }
    assert clock.slept == [t6.LOG_POLL_S]
    never = FakeRun([(["logging"], ok([]))])
    with pytest.raises(CommandError, match="no report"):
        t6.read_report(never, "ssc-c-cellone01", t6.NAT_JOB, "exec-1", Clock().sleep)


def test_nat_verdict() -> None:
    assert t6.nat_verdict({"ip": NAT_IP}, NAT_IP).passed is True
    assert t6.nat_verdict({"ip": "35.0.0.9"}, NAT_IP).passed is False
    assert t6.nat_verdict({"error": "timed out"}, NAT_IP).passed is False


def test_proxy_verdict_needs_every_allowed_host_via_nat_and_every_unlisted_refused() -> None:
    report = {
        "allowed": {
            "ifconfig.me": {"status": 200, "ip": NAT_IP},
            "api.ipify.org": {"status": 200, "ip": NAT_IP},
        },
        "unlisted": {"example.org": {"status": 403}, "1.1.1.1": {"error": "reset"}},
    }
    good = t6.proxy_verdict(report, NAT_IP)
    assert good.passed is True
    assert good.number == f"proxy stand-in: 2/2 allowed via {NAT_IP}, 2/2 unlisted refused"
    leaked = {**report, "unlisted": {"example.org": {"status": 200}}}
    assert t6.proxy_verdict(leaked, NAT_IP).passed is False
    wrong_exit = {**report, "allowed": {"ifconfig.me": {"status": 200, "ip": "35.0.0.9"}}}
    assert t6.proxy_verdict(wrong_exit, NAT_IP).passed is False
    assert t6.proxy_verdict({}, NAT_IP).passed is False


def test_t6_proxy_runs_the_job_with_its_hosts() -> None:
    report = {
        "allowed": {"a.example": {"status": 200, "ip": NAT_IP}},
        "unlisted": {"b.example": {"status": 403}},
    }
    run = FakeRun(
        [
            (["execute"], ok({"metadata": {"name": "proofrun-egress-app-x1"}})),
            (["logging"], ok([{"jsonPayload": {"proofrun_egress": report}}])),
        ]
    )
    args = argparse.Namespace(
        step="proxy",
        project="ssc-c-cellone01",
        nat_ip=NAT_IP,
        job=t6.APP_JOB,
        allow=["a.example"],
        unlisted=["b.example"],
    )
    assert t6.run(args, run).passed is True
    assert "--args=proxy,10.20.4.10:3128,allow=a.example,deny=b.example" in run.calls[0]
    assert "--wait" in run.calls[0]
    assert any("proofrun-egress-app-x1" in part for part in run.calls[1])


def sample_for(slot: int, seconds: float = 2.0, status: int = 200) -> dict[str, Any]:
    out: dict[str, Any] = {
        "slot": slot,
        "series": t7.series_of(slot),
        "apps": {app: {"status": status, "s": seconds, "error": None} for app in t7.APPS},
        "vpc": {"delay_s": 0.4},
    }
    if out["series"] == "warm":
        out["gateway"] = {"status": 200, "s": 1.5, "error": None}
    return out


def state_with(samples: list[dict[str, Any]], wanted: int = 2) -> dict[str, Any]:
    return {"config": {"samples": wanted, "gap_minutes": 26}, "samples": samples}


def test_summarise_takes_medians_against_bake_off_plus_gateway() -> None:
    out = t7.summarise(state_with([sample_for(i) for i in range(4)]))
    assert out.passed is True
    assert out.data["gateway_s"] == 1.5
    assert out.data["vpc_delay_s"] == 0.4
    assert out.data["medians"]["static"] == {"cold": 2.0, "warm": 2.0}
    slow = [sample_for(i, seconds=6.5) for i in range(4)]
    assert t7.summarise(state_with(slow)).passed is False
    failed = [sample_for(0), sample_for(1), sample_for(2, status=502), sample_for(3)]
    assert t7.summarise(state_with(failed)).passed is False


def test_summarise_is_incomplete_until_every_sample_is_in() -> None:
    out = t7.summarise(state_with([sample_for(0)]))
    assert out.passed is None
    assert "1/4 samples taken" in out.lines[-1]


def test_sample_loop_waits_a_gap_first_and_between_samples(tmp_path: Path) -> None:
    clock = Clock()
    store = StateFile(tmp_path / "t7.json")
    state = store.resume({"samples": 2, "gap_minutes": 26})
    taken: list[int] = []

    def take(slot: int) -> dict[str, Any]:
        taken.append(slot)
        clock.now += 5
        return sample_for(slot)

    t7.sample_loop(store, state, take=take, clock=clock, sleep=clock.sleep, say=lambda _: None)
    assert taken == [0, 1, 2, 3]
    assert clock.slept == [26 * 60] * 4
    saved = store.load()
    assert saved is not None
    assert len(saved["samples"]) == 4
    assert "in_progress" not in saved


def test_sample_loop_discards_a_cut_off_sample_and_waits_a_full_gap(tmp_path: Path) -> None:
    clock = Clock()
    store = StateFile(tmp_path / "t7.json")
    state = store.resume({"samples": 1, "gap_minutes": 26})
    state["samples"] = [{**sample_for(0), "at": clock.now - 3000}]
    state["last_traffic_at"] = clock.now - 3000
    state["in_progress"] = {"slot": 1, "at": clock.now - 60}
    store.save(state)
    said: list[str] = []
    resumed = store.resume({"samples": 1, "gap_minutes": 26})
    t7.sample_loop(store, resumed, take=sample_for, clock=clock, sleep=clock.sleep, say=said.append)
    assert "slot 1 was cut off" in said[0]
    assert clock.slept == [26 * 60 - 60]
    assert [s["slot"] for s in resumed["samples"]] == [0, 1]


def test_sample_loop_start_now_skips_the_first_wait(tmp_path: Path) -> None:
    clock = Clock()
    store = StateFile(tmp_path / "t7.json")
    state = store.resume({"samples": 1, "gap_minutes": 26})
    t7.sample_loop(
        store,
        state,
        take=sample_for,
        clock=clock,
        sleep=clock.sleep,
        say=lambda _: None,
        start_now=True,
    )
    assert clock.slept == [26 * 60]


def test_take_sample_asks_the_gateway_first_when_warm() -> None:
    CookieJar().put("static.cellone01.example.com", "v1." + "c" * 30, "browser")
    targets = {
        "static": "https://static.cellone01.example.com/",
        "api": "https://api.cellone01.example.com/health",
        "streamlit": "https://st.cellone01.example.com/_stcore/health",
    }
    http = FakeHttp(
        [
            ("www.", answer(404, b"", 1.2)),
            ("/vpc", answer(200, {"delay_s": 0.3, "attempts": 6})),
            ("example.com", answer(200, b"ok", 2.5)),
        ]
    )
    sample = t7.take_sample(1, targets, "https://www.cellone01.example.com/", CookieJar(), http)
    assert http.calls[0][0].startswith("https://www.")
    assert sample["gateway"] == {"status": 404, "s": 1.2, "error": None}
    assert sample["apps"]["static"]["status"] == 200
    assert sample["vpc"] == {"delay_s": 0.3, "attempts": 6}
    static_headers = next(h for u, h in http.calls if u == targets["static"])
    assert static_headers is not None
    assert "Cookie" in static_headers
    api_headers = next(h for u, h in http.calls if u == targets["api"])
    assert api_headers is not None
    assert "Cookie" not in api_headers
    cold = t7.take_sample(0, targets, "https://www.cellone01.example.com/", CookieJar(), http)
    assert "gateway" not in cold
