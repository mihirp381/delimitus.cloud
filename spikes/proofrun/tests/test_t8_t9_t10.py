import argparse
import json
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import Clock, FakeHttp, FakeRun, answer, ok

from proofrun import cloudrun, cost, t8, t9, t10
from proofrun.common import CookieJar, Done, StateFile

STEPS = ("gateway_deny", "datagw_suspend", "egress_remove", "scale_to_zero", "pause_timers")


def disable_result(state: str = "done") -> dict[str, Any]:
    return {
        "run_id": "ksr_1",
        "state": state,
        "steps": [
            {"name": n, "state": state, "elapsed_ms": 100, "attempts": 1, "error": None}
            for n in STEPS
        ],
        "total_ms": 4000,
    }


def audit_page(since: Mapping[str, int]) -> dict[str, Any]:
    events = [
        {"action": t8.STEP_ACTION, "after": {"step": n, "state": "running", "since_command_ms": 1}}
        for n in since
    ]
    events += [
        {"action": t8.STEP_ACTION, "after": {"step": n, "state": "done", "since_command_ms": ms}}
        for n, ms in since.items()
    ]
    events.append({"action": "app.disabled", "after": {}})
    return {"events": events}


def test_since_by_step_keeps_finished_steps_only() -> None:
    out = t8.since_by_step(audit_page({"gateway_deny": 900, "scale_to_zero": 6000}))
    assert out == {
        "gateway_deny": {"since_command_ms": 900, "state": "done"},
        "scale_to_zero": {"since_command_ms": 6000, "state": "done"},
    }


def test_t8_summarise_passes_inside_ten_seconds() -> None:
    audit = t8.since_by_step(audit_page({n: 1000 * (i + 1) for i, n in enumerate(STEPS)}))
    watch = t8.Watch(refused_s=1.2, refused_status=403, cut_s=1.4, ticks=7)
    out = t8.summarise(watch=watch, cli_s=5.5, result=disable_result(), audit=audit, compile_s=2.0)
    assert out.passed is True
    assert out.number.startswith("end to end 5.00 s")
    assert "instances 0 at 4.00 s" in out.number


def test_t8_summarise_fails_late_or_undone_and_is_incomplete_without_a_cut() -> None:
    late = t8.since_by_step(audit_page({**{n: 1000 for n in STEPS}, "scale_to_zero": 12000}))
    watch = t8.Watch(refused_s=1.0, refused_status=403, cut_s=1.0)
    assert (
        t8.summarise(watch=watch, cli_s=13, result=disable_result(), audit=late, compile_s=1).passed
        is False
    )
    failed = disable_result("failed")
    audit = t8.since_by_step(audit_page({n: 1000 for n in STEPS}))
    assert (
        t8.summarise(watch=t8.Watch(), cli_s=3, result=failed, audit=audit, compile_s=None).passed
        is False
    )
    no_cut = t8.Watch(refused_s=1.0, refused_status=403)
    assert (
        t8.summarise(
            watch=no_cut, cli_s=3, result=disable_result(), audit=audit, compile_s=1
        ).passed
        is None
    )
    missing = dict(list(audit.items())[:3])
    assert (
        t8.summarise(
            watch=watch, cli_s=3, result=disable_result(), audit=missing, compile_s=1
        ).passed
        is False
    )


def test_read_audit_uses_the_cli_login_without_printing_it() -> None:
    run = FakeRun([(["-c"], Done(0, "https://api.example.com\nsecret-token-value", ""))])
    page = audit_page({"gateway_deny": 800})
    http = FakeHttp([("/v1/audit?", answer(200, page))])
    assert t8.read_audit(run, http, "ksr_1") == {
        "gateway_deny": {"since_command_ms": 800, "state": "done"}
    }
    url, headers = http.calls[0]
    assert "target_id=ksr_1" in url
    assert "secret-token-value" not in url
    assert headers == {"Authorization": "Bearer secret-token-value"}


def test_latest_changed_reads_the_object_update_time() -> None:
    run = FakeRun([(["describe"], ok({"update_time": "2026-10-03T10:00:02.5Z"}))])
    assert t8.latest_changed(run, "cellone01", "org_1").second == 2
    assert run.calls[0][4] == "gs://ssc-c-cellone01-cell/snapshots/org_1/latest.json"


class DrillWorld:
    """A cell where ``ssc disable`` refuses the front door and ends the stream at once."""

    def __init__(self) -> None:
        self.pulled = threading.Event()

    def run(self, argv: Any, *, cwd: Any = None, env: Any = None) -> Done:
        if "status" in argv:
            env_doc = {
                "id": "env_" + "a" * 20,
                "name": "preview",
                "url": "https://api.cellone01.example.com",
            }
            return ok({"environments": [env_doc]})
        if "whoami" in argv:
            return ok({"org_id": "org_1"})
        if "disable" in argv:
            self.pulled.set()
            time.sleep(0.05)
            return ok(disable_result())
        if "-c" in argv:
            return Done(0, "https://api.example.com\ntok", "")
        if "describe" in argv:
            return ok({"update_time": "2099-01-01T00:00:00Z"})
        raise AssertionError(argv)

    def http(self, url: str, headers: Any = None, timeout: float = 90.0) -> Any:
        if "/v1/audit" in url:
            return answer(200, audit_page({n: 500 for n in STEPS}))
        return answer(403 if self.pulled.is_set() else 200)

    def socket(self, url: str, origin: str, headers: Mapping[str, str]) -> Any:
        world = self

        class Socket:
            def recv(self, timeout: float | None = None) -> str:
                if world.pulled.is_set():
                    raise ConnectionError("closed")
                time.sleep(0.01)
                return "tick"

            def close(self) -> None:
                return None

        return Socket()


def test_t8_drill_end_to_end_with_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROOFRUN_SSC", "ssc")
    monkeypatch.setattr(t8, "POLL_S", 0.01)
    CookieJar().put("api.cellone01.example.com", "v1." + "c" * 30, "browser")
    world = DrillWorld()
    args = argparse.Namespace(app="api", label="cellone01", env="preview", ws_path="/ws")
    out = t8.run(args, world.run, world.http, world.socket)
    assert out.data["refused_s"] is not None
    assert out.data["cut_s"] is not None
    assert out.passed is True
    assert out.lines[-1] == "undo: ssc enable api"


def test_cost_figures_match_the_model() -> None:
    assert round(cost.hourly("request"), 4) == 0.0909
    assert round(cost.hourly("instance"), 4) == 0.0684
    assert round(cost.hourly("request", 0.5, 0.5), 4) == 0.0477
    assert cost.usage_cost("instance", 3600, 1800) == pytest.approx(0.0684)
    assert cost.within(1.15, 1.0)
    assert not cost.within(1.25, 1.0)
    assert not cost.within(0.0, 0.0)


def test_the_kit_reads_the_products_cost_model_as_data() -> None:
    assert cost.MODEL_FILE.is_file()
    assert cost.MODEL_FILE.parts[-6:] == (
        "packages",
        "ssc_control",
        "src",
        "ssc_control",
        "metrics",
        "cost_model.toml",
    )
    assert cost.MODEL["format"] == "ssc-cost-model/v1"
    assert cost.RATES == {
        "request": {"vcpu": 0.000024, "gib": 0.0000025},
        "instance": {"vcpu": 0.000018, "gib": 0.000002},
    }
    assert cost.TOLERANCE == 0.20
    assert cost.EMPTY_CELL_MONTH_USD == 23.0
    reference = cost.MODEL["cloud_run"]["reference"]
    assert round(cost.hourly("request"), 4) == reference["request_hourly"]
    assert round(cost.hourly("instance"), 4) == reference["instance_hourly"]
    assert "ssc_control" not in sys.modules


def t9_state(
    intervals: list[dict[str, Any]], mode: str = "instance", hours: float = 2.0
) -> dict[str, Any]:
    return {"config": {"mode": mode, "hours": hours}, "intervals": intervals, "refusals": []}


def test_t9_report_counts_hour_drops_and_held_time() -> None:
    state = t9_state(
        [
            {"open": 0, "close": 3600, "code": "1006"},
            {"open": 3602, "close": 7200, "code": "held", "reason": "hours up"},
        ]
    )
    out = t9.report(state)
    assert out.passed is True
    assert "drops at about 60 minutes: 1 of 1" in out.lines
    assert out.data["reconnects"] == 1
    assert t9.report(t9_state([{"open": 0, "close": 600, "code": "x"}])).passed is None
    gappy = t9_state(
        [{"open": 0, "close": 3000, "code": "x"}, {"open": 3600, "close": 7200, "code": "held"}]
    )
    assert t9.report(gappy).passed is False


def test_t9_bill_against_the_model() -> None:
    state = t9_state([{"open": 0, "close": 24 * 3600, "code": "held"}], hours=24)
    out = t9.bill(state, usd=None, vcpu_s=24 * 3600, gib_s=12 * 3600)
    assert out.passed is True
    assert out.data["model_usd"] == pytest.approx(24 * 0.0684)
    assert t9.bill(state, usd=3.0, vcpu_s=None, gib_s=None).passed is False
    with pytest.raises(SystemExit):
        t9.bill(state, usd=None, vcpu_s=1.0, gib_s=None)


class DroppedError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"closed {code}")
        self.rcvd = type("Close", (), {"code": code})()


class HeldSocket:
    """A stream that stays quiet until ``drop_at`` on the fake clock, then closes."""

    def __init__(self, clock: Clock, drop_at: float | None) -> None:
        self.clock, self.drop_at, self.closed = clock, drop_at, False

    def recv(self, timeout: float | None = None) -> str:
        step = timeout or 1.0
        if self.drop_at is not None and self.clock.now + step >= self.drop_at:
            self.clock.now = self.drop_at
            raise DroppedError(1006)
        self.clock.now += step
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


def test_t9_hold_reconnects_after_a_drop_and_a_refusal(tmp_path: Path) -> None:
    clock = Clock(0.0)
    store = StateFile(tmp_path / "t9.json")
    state = store.resume({"mode": "instance", "hours": 2.0})
    plan: list[Any] = [3600.0, RuntimeError("HTTP 403"), None]
    seen: list[Mapping[str, str]] = []

    def socket(url: str, origin: str, headers: Mapping[str, str]) -> HeldSocket:
        seen.append(headers)
        step = plan.pop(0)
        if isinstance(step, Exception):
            raise step
        return HeldSocket(clock, step)

    t9.hold(
        store,
        state,
        url="wss://st.cellone01.example.com/_stcore/stream",
        headers=lambda: {"Cookie": "fresh"},
        socket=socket,
        clock=clock,
        sleep=clock.sleep,
        say=lambda _: None,
    )
    saved = store.load()
    assert saved is not None
    assert [i["code"] for i in saved["intervals"]] == ["1006", "held"]
    assert saved["intervals"][0]["close"] == 3600.0
    assert len(saved["refusals"]) == 1
    assert clock.slept == [t9.RETRY_S]
    assert len(seen) == 3
    assert t9.report(saved).passed is True


def test_t9_hold_resumes_closing_the_cut_stream_at_its_last_heartbeat(tmp_path: Path) -> None:
    clock = Clock(10_000.0)
    store = StateFile(tmp_path / "t9.json")
    state = store.resume({"mode": "request", "hours": 1.0})
    state["ends_at"] = 10_500.0
    state["intervals"] = []
    state["open"] = {"open": 9_000.0}
    state["alive_at"] = 9_900.0
    store.save(state)
    resumed = store.resume({"mode": "request", "hours": 1.0})
    t9.hold(
        store,
        resumed,
        url="wss://st.cellone01.example.com/_stcore/stream",
        headers=lambda: {},
        socket=lambda u, o, h: HeldSocket(clock, None),
        clock=clock,
        sleep=clock.sleep,
        say=lambda _: None,
    )
    assert resumed["intervals"][0] == {"open": 9_000.0, "close": 9_900.0, "code": "stopped"}
    assert resumed["intervals"][-1]["code"] == "held"
    assert resumed["ends_at"] == 10_500.0


def service_doc(
    throttling: str | None, generation: str | None = None, cpu: str = "1"
) -> dict[str, Any]:
    annotations = {}
    if throttling is not None:
        annotations["run.googleapis.com/cpu-throttling"] = throttling
    if generation is not None:
        annotations["run.googleapis.com/execution-environment"] = generation
    return {
        "spec": {
            "template": {
                "metadata": {"annotations": annotations},
                "spec": {
                    "containers": [{"resources": {"limits": {"cpu": cpu, "memory": "512Mi"}}}]
                },
            }
        }
    }


def test_t9_hold_refuses_a_service_billed_the_other_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROOFRUN_SSC", "ssc")
    env = {"id": "env_" + "c" * 20, "name": "preview", "url": "https://st.cellone01.example.com"}
    run = FakeRun(
        [(["status"], ok({"environments": [env]})), (["describe"], ok(service_doc(None)))]
    )
    args = argparse.Namespace(
        step="hold",
        state=tmp_path / "s.json",
        app="st",
        project="ssc-c-cellone01",
        mode="instance",
        hours=1.0,
        env="preview",
    )
    out = t9.run(args, run)
    assert out.passed is None
    assert out.number == "the service is not billed that way"
    assert not (tmp_path / "s.json").exists()


def test_t10_costs_and_the_gen1_guard() -> None:
    lines = t10.costs()
    assert "$0.0909" in lines[0]
    assert "$0.0477" in lines[0]
    run = FakeRun([(["describe", "ssc-gateway"], ok(service_doc(None)))])
    args = argparse.Namespace(project="ssc-c-cellone01")
    out = t10.run(args, run)
    assert out.passed is None
    assert "override" in out.lines[-1]


class TickSocket:
    def __init__(self, ticks: int) -> None:
        self.left = ticks

    def recv(self, timeout: float | None = None) -> str:
        if self.left == 0:
            raise TimeoutError
        self.left -= 1
        return "tick"

    def close(self) -> None:
        return None


def test_t10_ws_ticks_and_verdict() -> None:
    assert t10.ws_ticks(lambda u, o, h: TickSocket(9), "wss://a.b.example.com/ws", {}) == (5, "")
    got, why = t10.ws_ticks(lambda u, o, h: TickSocket(2), "wss://a.b.example.com/ws", {})
    assert (got, why.split(":")[0]) == (2, "TimeoutError")

    def refused(u: str, o: str, h: Mapping[str, str]) -> Any:
        raise ConnectionRefusedError("403")

    assert t10.ws_ticks(refused, "wss://a.b.example.com/ws", {})[0] == 0
    gateway = cloudrun.settings(service_doc(None, "gen1", "500m"))
    results = [{"probe": f"p{i}", "status": "passed", "reason": ""} for i in range(14)]
    assert t10.verdict(gateway, results, 5, "").passed is True
    assert t10.verdict(gateway, results, 3, "closed").passed is False
    gen2 = cloudrun.settings(service_doc(None, "gen2", "1"))
    assert t10.verdict(gen2, results, 5, "").passed is False
    assert json.dumps(t10.verdict(gateway, results, 5, "").data)
