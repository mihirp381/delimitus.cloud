"""GA-4.8 warm option kit, offline: a fake control API, a fake Cloud Run service and a fake app
behind a fake gateway. Nothing sleeps; the clock moves only when the code sleeps."""

import argparse
import json
import sys
import tomllib
import types
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import Clock

from proofrun import warm
from proofrun.__main__ import PROOFS, parser
from proofrun.common import KIT, CommandError, CookieJar, Done, Fetched, Outcome

SLUG, LABEL = "ga4warm", "proofcell02"
HOST = f"{SLUG}.{LABEL}.delimitusapps.com"
WWW = f"www.{LABEL}.delimitusapps.com"
API = "https://api.example.test"
TOKEN = "operator-token-value"
COOKIE = "c" * 40
PROD, PREVIEW, OTHER = "env_" + "p" * 20, "env_" + "v" * 20, "env_" + "o" * 20
SERVICE = "ssc-a-" + "p" * 20
COST = 10
REVISION = SERVICE + "-1-abc"
WAKING = (
    b"<!doctype html><html lang=en><meta charset=utf-8><meta http-equiv=refresh content=2>"
    b"<title>Waking up</title><h1>Waking up</h1></html>\n"
)


def problem(code: str) -> bytes:
    return json.dumps({"status": 422, "code": code, "title": "t", "detail": "d"}).encode()


class ControlPlane:
    """``/v1/warm`` and ``/v1/audit`` as the routes answer, with the same refusals."""

    def __init__(self) -> None:
        self.warm: list[str] = []
        self.gateway = False
        self.puts: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.accept_wrong_cost = False
        self.preview_code = "VALIDATION_FAILED"
        self.refuse_off = False
        self.on_change: Callable[[list[str]], None] = lambda _ids: None
        self.headers: list[Mapping[str, str]] = []

    def doc(self) -> dict[str, Any]:
        envs = [(PROD, SLUG), (OTHER, "other")]
        return {
            "environments": [
                {"environment_id": e, "app_id": "app_x", "app_slug": s, "warm": e in self.warm}
                for e, s in envs
            ],
            "gateway": {"warm": self.gateway, "state": "off", "failure_code": None},
            "working_days": 20,
            "environment_monthly_usd": COST,
            "gateway_monthly_usd": 10,
            "monthly_usd": COST * len(self.warm),
        }

    def put(self, body: Mapping[str, Any]) -> Fetched:
        self.puts.append(dict(body))
        ids = sorted(set(body["environment_ids"]))
        if PREVIEW in ids:
            return Fetched(422, 0.1, None, problem(self.preview_code))
        if body["monthly_usd_shown"] != COST * len(ids) and not self.accept_wrong_cost:
            return Fetched(422, 0.1, None, problem("VALIDATION_FAILED"))
        if self.refuse_off and not ids:
            return Fetched(503, 0.1, None, b"{}")
        if ids != self.warm:
            self.events.insert(
                0,
                {
                    "seq": len(self.events) + 1,
                    "at": "2026-10-08T10:00:00Z",
                    "action": "org.updated",
                    "target": {"kind": "warm"},
                    "before": {"environment_ids": self.warm, "gateway": False},
                    "after": {
                        "environment_ids": ids,
                        "gateway": body["gateway"],
                        "monthly_usd_shown": body["monthly_usd_shown"],
                    },
                },
            )
            self.warm = ids
            self.on_change(ids)
        return Fetched(200, 0.1, None, json.dumps(self.doc()).encode())

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Fetched:
        assert url.startswith(API)
        self.headers.append(headers)
        if method == "PUT" and url.endswith("/v1/warm"):
            return self.put(json.loads(body or b"{}"))
        if url.endswith("/v1/warm"):
            return Fetched(200, 0.1, None, json.dumps(self.doc()).encode())
        if "/v1/audit?" in url:
            assert "target_kind=warm" in url and "action=org.updated" in url
            return Fetched(200, 0.1, None, json.dumps({"events": self.events}).encode())
        raise AssertionError(f"unexpected {method} {url}")


class Cell:
    """``ssc status``, the CLI login and ``gcloud run services describe``."""

    def __init__(self, plane: ControlPlane) -> None:
        self.plane = plane
        self.never_min = False
        self.revision = REVISION
        self.revision_after = REVISION
        self.calls: list[list[str]] = []

    def describe(self) -> dict[str, Any]:
        on = PROD in self.plane.warm and not self.never_min
        annotations = {"run.googleapis.com/minScale": "1"} if on else {}
        meta = {"generation": 3, "annotations": annotations}
        revision = self.revision_after if on else self.revision
        return {
            "metadata": meta,
            "spec": {"template": {"metadata": {"annotations": {}}, "spec": {}}},
            "status": {
                "observedGeneration": 3,
                "conditions": [{"type": "Ready", "status": "True"}],
                "traffic": [{"revisionName": revision, "percent": 100}],
                "latestReadyRevisionName": revision,
            },
        }

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Done:
        self.calls.append(list(argv))
        if "status" in argv:
            envs = [
                {
                    "name": "prod",
                    "id": PROD,
                    "url": f"https://{HOST}",
                    "current_deployment_id": "d",
                },
                {"name": "preview", "id": PREVIEW, "url": f"https://{SLUG}--preview.x"},
            ]
            return Done(0, json.dumps({"slug": SLUG, "environments": envs}), "")
        if "-c" in argv:
            return Done(0, f"{API}\n{TOKEN}", "")
        if "describe" in argv:
            assert SERVICE in argv and f"--project=ssc-c-{LABEL}" in argv
            return Done(0, json.dumps(self.describe()), "")
        raise AssertionError(f"unexpected command {argv}")


class App:
    """The fixture behind the gateway: a process identity, and a cold start that shows the
    waking page to a page load without the wake cookie."""

    def __init__(self) -> None:
        self.process = "2026-10-08T09:00:00.000000+00:00"
        self.cold = False
        self.wake_on_warm = False
        self.warm_seconds = 0.2
        self.cold_same_process = False
        self.calls: list[tuple[str, Mapping[str, str] | None]] = []

    def page(self) -> bytes:
        return (
            f"<!doctype html><meta name=ssc-started-at content={self.process}>"
            f"<meta name=ssc-pid content=7><title>{warm.PAGE_TITLE}</title>"
        ).encode()

    def start(self) -> None:
        self.cold = False
        if not self.cold_same_process:
            self.process = "2026-10-08T11:00:00.000000+00:00"

    def __call__(
        self, url: str, headers: Mapping[str, str] | None = None, timeout: float = 90.0
    ) -> Fetched:
        self.calls.append((url, headers))
        h = headers or {}
        if url == f"https://{WWW}/":
            return Fetched(404, 1.5, None, b"")
        cookie = {"Set-Cookie": "__Host-ssc-wake=1; Path=/"}
        if url == f"https://{HOST}/":
            assert h.get("Sec-Fetch-Mode") == "navigate" and h.get("Sec-Fetch-Dest") == "document"
            assert "text/html" in h.get("Accept", "")
            woke = "__Host-ssc-wake=1" in h.get("Cookie", "")
            if (self.cold or self.wake_on_warm) and not woke:
                self.wake_on_warm = False
                return Fetched(503, 2.0, None, WAKING, cookie)
            if self.cold:
                self.start()
            return Fetched(200, self.warm_seconds, None, self.page(), cookie)
        if url == f"https://{HOST}/health":
            assert "Sec-Fetch-Mode" not in h
            if self.cold:
                self.start()
            body = {"started_at": self.process, "pid": 7, "uptime_s": 1.0}
            return Fetched(200, 0.1, None, json.dumps(body).encode())
        raise AssertionError(f"unexpected request {url}")


class Rig:
    def __init__(self) -> None:
        self.plane = ControlPlane()
        self.cell = Cell(self.plane)
        self.app = App()
        self.clock = Clock()
        self.plane.on_change = self.changed
        CookieJar().put(HOST, COOKIE, "browser")

    def changed(self, ids: list[str]) -> None:
        if PROD not in ids:
            self.app.cold = True

    def wall(self) -> datetime:
        return datetime.fromtimestamp(self.clock.now, UTC)

    def run(self, *extra: str) -> Outcome:
        args = parser().parse_args(["warm", "--app", SLUG, "--label", LABEL, *extra])
        return warm.run(
            args,
            run=self.cell,
            send=self.plane,
            http=self.app,
            sleep=self.clock.sleep,
            clock=self.clock,
            wall=self.wall,
        )


def by_n(outcome: Outcome) -> dict[int, str]:
    return {c["n"]: c["result"] for c in outcome.data["checks"]}


def test_the_happy_path_passes_every_automatic_check() -> None:
    rig = Rig()
    outcome = rig.run()
    assert by_n(outcome) == dict.fromkeys(range(1, 10), "PASS"), outcome.lines
    assert outcome.verdict == "PASS"
    assert "warm left off: yes" in outcome.number
    assert rig.plane.warm == []
    assert all(p["gateway"] is False for p in rig.plane.puts)
    shown = [p["monthly_usd_shown"] for p in rig.plane.puts]
    assert shown == [COST + 1, COST, COST, 0]
    assert outcome.data["cold_waking"] is True and outcome.data["warm_waking"] is False
    text = json.dumps(outcome.data) + "\n".join(outcome.lines)
    assert TOKEN not in text and COOKIE not in text


def test_the_page_loads_are_browser_page_loads_and_the_retry_carries_the_wake_cookie() -> None:
    rig = Rig()
    rig.run()
    pages = [h or {} for u, h in rig.app.calls if u == f"https://{HOST}/"]
    assert "__Host-ssc-wake" not in pages[0]["Cookie"]
    assert pages[-1]["Cookie"].endswith("; __Host-ssc-wake=1")
    assert all(p["Cookie"].startswith(f"__Host-ssc-session={COOKIE}") for p in pages)
    www = [i for i, (u, _) in enumerate(rig.app.calls) if u == f"https://{WWW}/"]
    page_idx = [i for i, (u, _) in enumerate(rig.app.calls) if u == f"https://{HOST}/"]
    assert len(www) == 2 and www[0] == page_idx[0] - 1


def test_the_idle_holds_and_the_settle_are_slept_not_waited() -> None:
    rig = Rig()
    rig.run("--idle-minutes", "17")
    assert rig.clock.slept.count(60.0) == 34
    assert warm.SETTLE_AFTER_S in rig.clock.slept


def test_the_timeline_file_is_written_without_secrets() -> None:
    rig = Rig()
    rig.run()
    files = list(warm.results_dir().glob("warm-*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    events = [e["event"] for e in record["timeline"]]
    assert "warm on" in events and "warm off" in events and "service min 1" in events
    assert TOKEN not in files[0].read_text() and COOKIE not in files[0].read_text()


def test_a_cost_off_by_one_that_is_accepted_fails_check_two_and_is_set_off() -> None:
    rig = Rig()
    rig.plane.accept_wrong_cost = True
    outcome = rig.run()
    assert by_n(outcome)[2] == "FAIL"
    assert outcome.verdict == "FAIL"
    assert rig.plane.warm == [] and rig.plane.puts[-1]["environment_ids"] == []


def test_a_preview_refused_with_another_code_fails_check_two() -> None:
    rig = Rig()
    rig.plane.preview_code = "REFERENCE_NOT_FOUND"
    outcome = rig.run()
    checks = {c["n"]: c for c in outcome.data["checks"]}
    assert checks[2]["result"] == "FAIL"
    assert "preview: HTTP 422 REFERENCE_NOT_FOUND" in checks[2]["detail"]
    assert "off by one: HTTP 422 VALIDATION_FAILED" in checks[2]["detail"]


def test_a_minimum_that_never_reaches_one_fails_check_four_and_warm_is_set_off() -> None:
    rig = Rig()
    rig.cell.never_min = True
    outcome = rig.run("--settle-seconds", "60")
    results = by_n(outcome)
    assert results[4] == "FAIL"
    assert all(results[n] == "not read" for n in range(5, 10))
    assert outcome.verdict == "FAIL"
    assert rig.plane.warm == []
    assert rig.plane.puts[-1] == {"environment_ids": [], "gateway": False, "monthly_usd_shown": 0}
    assert any(line.startswith("finally: warm set off again") for line in outcome.lines)
    assert "warm left off: yes" in outcome.number
    assert rig.clock.slept.count(warm.POLL_S) == 12


def test_a_changed_serving_revision_fails_check_four() -> None:
    rig = Rig()
    rig.cell.revision_after = SERVICE + "-2-def"
    outcome = rig.run()
    checks = {c["n"]: c for c in outcome.data["checks"]}
    assert checks[4]["result"] == "FAIL" and "serving revision changed" in checks[4]["detail"]
    assert outcome.verdict == "FAIL" and rig.plane.warm == []


def test_a_failed_off_put_is_retried_in_finally_and_the_final_line_says_not_off() -> None:
    rig = Rig()
    rig.plane.refuse_off = True
    outcome = rig.run()
    assert by_n(outcome)[7] == "FAIL"
    assert rig.plane.puts[-1]["environment_ids"] == [] and rig.plane.warm == [PROD]
    assert "warm left off: NO" in outcome.number
    assert any("undo by hand" in line for line in outcome.lines)


def test_an_interrupt_sets_warm_off_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    rig = Rig()
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        if seconds == 60.0:
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
        rig.clock.sleep(seconds)

    args = parser().parse_args(["warm", "--app", SLUG, "--label", LABEL])
    with pytest.raises(KeyboardInterrupt):
        warm.run(args, rig.cell, rig.plane, rig.app, sleep, rig.clock, rig.wall)
    assert rig.plane.warm == []
    assert "finally: warm set off again" in capsys.readouterr().out


def test_a_missing_cookie_fails_with_the_cookie_set_hint() -> None:
    rig = Rig()
    CookieJar().path.unlink()
    outcome = rig.run()
    assert outcome.verdict == "FAIL"
    assert f"python -m proofrun cookie set {HOST}" in outcome.lines[0]
    assert rig.plane.puts == []


def test_an_org_with_something_warm_already_stops_before_any_change() -> None:
    rig = Rig()
    rig.plane.warm = [OTHER]
    outcome = rig.run()
    assert by_n(outcome)[1] == "FAIL"
    assert outcome.verdict == "FAIL"
    assert rig.plane.puts == []


def test_idle_minutes_below_sixteen_are_refused() -> None:
    with pytest.raises(SystemExit):
        parser().parse_args(["warm", "--app", SLUG, "--label", LABEL, "--idle-minutes", "15"])
    args = argparse.Namespace(app=SLUG, label=LABEL, project=None, idle_minutes=10)
    with pytest.raises(CommandError):
        warm.run(args, run=Cell(ControlPlane()))


def test_a_waking_page_on_the_warm_load_fails_check_six() -> None:
    rig = Rig()
    rig.app.wake_on_warm = True
    outcome = rig.run()
    checks = {c["n"]: c for c in outcome.data["checks"]}
    assert checks[6]["result"] == "FAIL" and "waking page True" in checks[6]["detail"]
    assert outcome.verdict == "FAIL"


def test_a_slow_first_byte_on_the_warm_load_fails_check_six() -> None:
    rig = Rig()
    rig.app.warm_seconds = 2.5
    outcome = rig.run()
    assert by_n(outcome)[6] == "FAIL"


def test_a_cold_load_without_the_waking_page_passes_when_the_process_is_new() -> None:
    rig = Rig()
    original = rig.changed

    def changed(ids: list[str]) -> None:
        original(ids)
        if PROD not in ids:
            rig.app.start()  # a fast start: the page answers inside 2 s

    rig.plane.on_change = changed
    outcome = rig.run()
    assert by_n(outcome)[9] == "PASS"
    assert outcome.data["cold_waking"] is False


def test_a_cold_load_from_the_same_process_fails_check_nine() -> None:
    rig = Rig()
    rig.app.cold_same_process = True
    outcome = rig.run()
    assert by_n(outcome)[9] == "FAIL"


def test_the_screenshot_is_reported_and_never_changes_the_verdict() -> None:
    rig = Rig()
    absent = rig.run()
    assert absent.data["console_screenshot"] == "absent" and absent.verdict == "PASS"
    shot = warm.results_dir() / warm.SCREENSHOT
    shot.write_bytes(b"\x89PNG\r\n")
    rig2 = Rig()
    present = rig2.run()
    assert present.data["console_screenshot"] == "present" and present.verdict == "PASS"
    assert any(line.startswith("check 10 manual:") for line in present.lines)


def test_the_command_is_registered() -> None:
    assert PROOFS["warm"] is warm
    args = parser().parse_args(["warm", "--app", SLUG, "--label", LABEL])
    assert (args.idle_minutes, args.settle_seconds, args.project) == (20, 300, None)


def test_the_waking_marker_is_in_the_gateways_page() -> None:
    pages = pytest.importorskip("ssc_edge.pages")
    assert warm.WAKING_MARKER.encode() in pages.WAKING


def load_fixture(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    class Api:
        def get(self, _path: str, **_kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            return lambda fn: fn

    stub = types.ModuleType("fastapi")
    stub.FastAPI = Api  # type: ignore[attr-defined]
    responses = types.ModuleType("fastapi.responses")
    responses.HTMLResponse = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastapi", stub)
    monkeypatch.setitem(sys.modules, "fastapi.responses", responses)
    module = types.ModuleType("warm_fixture")
    source = (KIT / "apps" / "warm" / "main.py").read_text()
    exec(compile(source, "main.py", "exec"), module.__dict__)  # noqa: S102
    return module


def test_the_fixture_answers_its_process_on_both_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    app = load_fixture(monkeypatch)
    health = app.health()
    assert set(health) == {"started_at", "pid", "uptime_s"}
    assert datetime.fromisoformat(health["started_at"]).tzinfo is not None
    page = Fetched(200, 0.1, None, app.page().encode())
    seen = warm.page_identity(page)
    assert seen is not None and seen.started_at == health["started_at"]
    assert seen.pid == str(health["pid"])


def test_the_fixture_manifest_has_the_runtime_only() -> None:
    folder = KIT / "apps" / "warm"
    manifest = tomllib.loads((folder / "ssc.toml").read_text())
    assert set(manifest) == {"schema", "runtime"}
    assert manifest["runtime"]["health_path"] == "/health"
    assert "fastapi" in (folder / "requirements.txt").read_text()
