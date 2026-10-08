import argparse
import json
import subprocess
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import Clock, FakeHttp, FakeRun, answer, ok

from proofrun import timers
from proofrun.__main__ import PROOFS, parser
from proofrun.common import KIT, REPO, CommandError, Done, Fetched

BASE = datetime(2026, 10, 8, 10, 0, 0, tzinfo=UTC)
TOKEN = "operator-token-value"
APP, ENV, SCHEDULE = "app_1", "env_1", "sch_1"


def stamp(seconds: float) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def make_run(  # noqa: PLR0913  (a run's fields)
    slot: int,
    *,
    late: float = 2.0,
    took: float = 1.0,
    state: str = "succeeded",
    error: str | None = None,
    start_ms: int | None = 300,
    trigger: str = "schedule",
) -> dict[str, Any]:
    return {
        "run_id": f"tmr_{slot}",
        "schedule_id": SCHEDULE,
        "trigger": trigger,
        "state": state,
        "error": error,
        "http_status": 200 if state == "succeeded" else None,
        "start_ms": start_ms,
        "duration_ms": 120 if state == "succeeded" else None,
        "scheduled_for": stamp(slot),
        "started_at": stamp(slot + late),
        "finished_at": stamp(slot + late + took),
    }


GOOD = [make_run(0), make_run(60)]


def test_a_passing_set_passes_checks_one_to_five() -> None:
    checks = [
        timers.check_succeeded(GOOD),
        timers.check_spacing(GOOD),
        timers.check_overlap(GOOD),
        timers.check_lateness(GOOD),
        timers.check_start_ms(GOOD),
    ]
    assert [c.result for c in checks] == [True] * 5
    assert [c.n for c in checks] == [1, 2, 3, 4, 5]
    assert "PASS" in checks[0].line()


def test_pairing_takes_the_two_most_recent_succeeded_scheduled_runs() -> None:
    runs = [
        make_run(0),
        make_run(60, state="failed"),
        make_run(120),
        make_run(180, trigger="manual"),
        make_run(240),
    ]
    assert [r["run_id"] for r in timers.latest_two(runs)] == ["tmr_120", "tmr_240"]
    assert timers.check_spacing(runs).result is False
    assert timers.check_succeeded(runs).detail == "3 found"


def test_fewer_than_two_succeeded_fails_every_pair_check() -> None:
    runs = [make_run(0), make_run(60, state="failed")]
    assert timers.check_succeeded(runs).result is False
    assert timers.check_spacing(runs).result is False
    assert timers.check_lateness(runs).result is False
    assert timers.check_start_ms(runs).result is False


def test_a_one_hundred_twenty_second_gap_fails_the_spacing() -> None:
    check = timers.check_spacing([make_run(0), make_run(120)])
    assert check.result is False
    assert check.detail == "120.0 s"


def test_overlapping_runs_are_found() -> None:
    runs = [make_run(0, took=70), make_run(60)]
    assert timers.overlaps(runs) == [("tmr_0", "tmr_60")]
    assert timers.check_overlap(runs).result is False


def test_runs_that_only_touch_do_not_overlap() -> None:
    runs = [make_run(0, late=0, took=60), make_run(60, late=0)]
    assert timers.overlaps(runs) == []
    assert timers.check_overlap(runs).result is True


def test_a_run_with_error_overlap_fails_the_overlap_check() -> None:
    runs = [*GOOD, make_run(120, state="skipped", error="overlap")]
    check = timers.check_overlap(runs)
    assert check.result is False
    assert "1 with error overlap" in check.detail


def test_a_late_run_fails_the_lateness_check_and_shows_the_numbers() -> None:
    runs = [make_run(0), make_run(60, late=16.5)]
    check = timers.check_lateness(runs)
    assert check.result is False
    assert check.detail == "2.0 s, 16.5 s"
    assert timers.lateness_s(runs[1]) == 16.5
    assert timers.check_lateness([make_run(0, late=15), make_run(60, late=15)]).result is True


def test_a_run_without_start_ms_fails_check_five() -> None:
    assert timers.check_start_ms([make_run(0), make_run(60, start_ms=None)]).result is False


def test_count_ticks_counts_schedule_ticks_and_refusals() -> None:
    lines = [
        "TICK role=schedule at=2026-10-08T10:00:03.000Z method=POST",
        "GET /health 200",
        "TICK role=schedule at=2026-10-08T10:01:02.000Z method=POST",
        "TICK refused=missing",
        "TICK role=builder at=x method=GET",
    ]
    assert timers.count_ticks(lines) == (2, 1)


def test_check_six_passes_fails_or_is_not_read() -> None:
    assert timers.check_logs(timers.Logs(ticks=2, reads=1)).result is True
    assert timers.check_logs(timers.Logs(ticks=2, refused=1, reads=1)).result is False
    assert timers.check_logs(timers.Logs(ticks=1, reads=3)).result is False
    unread = timers.check_logs(timers.Logs(error="ssc logs failed: no", command="ssc logs x"))
    assert unread.result is None
    assert unread.word == "not read"
    assert "ssc logs x" in unread.detail


def test_verdict_is_fail_over_incomplete_over_pass() -> None:
    yes, no, unread = (timers.Check(1, "a", r, "") for r in (True, False, None))
    assert timers.verdict([yes]) is True
    assert timers.verdict([yes, unread]) is None
    assert timers.verdict([unread, no]) is False


def test_the_run_table_is_newest_first_with_lateness() -> None:
    rows = timers.run_table(GOOD)
    assert rows[1].startswith(stamp(60))
    assert rows[2].startswith(stamp(0))
    assert rows[1].endswith("2.0 s")


class World:
    """A cell with one app: a `minute` schedule, its runs, `ssc logs`, disable and enable."""

    def __init__(
        self,
        *,
        state: str = "active",
        reason: str | None = None,
        runs_by_poll: list[list[dict[str, Any]]] | None = None,
        logs: Done | None = None,
    ) -> None:
        self.state, self.reason = state, reason
        self.polls = runs_by_poll or [[], list(reversed(GOOD))]
        self.runs_polled = 0
        self.logs = logs or Done(0, json.dumps({"lines": tick_lines(2)}), "")
        self.commands: list[str] = []

    def schedules(self) -> Fetched:
        item = {
            "schedule_id": SCHEDULE,
            "environment_id": ENV,
            "name": "minute",
            "cron": "* * * * *",
            "timezone": "UTC",
            "path": "/tick",
            "method": "POST",
            "timeout_seconds": 60,
            "state": self.state,
            "pause_reason": self.reason,
            "next_run_at": stamp(120) if self.state == "active" else None,
            "last_run": None,
        }
        return answer(200, {"environment_id": ENV, "items": [item]})

    def runs(self) -> Fetched:
        page = self.polls[min(self.runs_polled, len(self.polls) - 1)]
        self.runs_polled += 1
        return answer(200, {"items": page, "next_before": None})

    def http(self) -> FakeHttp:
        detail = {"id": APP, "slug": "timer", "environments": [{"id": ENV, "name": "prod"}]}
        return FakeHttp(
            [
                (f"{SCHEDULE}/runs", self.runs),
                ("/schedules", self.schedules),
                (f"/v1/apps/{APP}", answer(200, detail)),
                ("/v1/apps", answer(200, {"apps": [{"id": APP, "slug": "timer"}]})),
            ]
        )

    def run(self) -> FakeRun:
        return WorldRun(self)


class WorldRun(FakeRun):
    """Login and logs from rules; disable and enable change the world's schedule."""

    def __init__(self, world: World) -> None:
        super().__init__(
            [(["-c"], Done(0, f"https://api.example.com\n{TOKEN}", "")), (["logs"], world.logs)]
        )
        self.world = world

    def __call__(self, argv: Any, **kw: Any) -> Done:
        for word, state, reason in (
            ("disable", "paused", "app_disabled"),
            ("enable", "active", None),
        ):
            if word in argv:
                self.calls.append(list(argv))
                self.world.commands.append(word)
                self.world.state, self.world.reason = state, reason
                return ok({"state": "done"})
        return super().__call__(argv, **kw)


def tick_lines(n: int) -> list[dict[str, str]]:
    return [
        {
            "timestamp": stamp(i * 60 + 3),
            "severity": "INFO",
            "source": "app",
            "text": f"TICK role=schedule at={stamp(i * 60 + 3)} method=POST",
        }
        for i in range(n)
    ]


def WALL() -> datetime:  # noqa: N802  (a stand-in for the wall clock, one second before BASE)
    return BASE - timedelta(seconds=1)


def go(world: World, *, disable: bool = False, minutes: int = 4) -> Any:
    clock = Clock()
    args = argparse.Namespace(app="timer", env="prod", minutes=minutes, disable=disable)
    return timers.run(args, world.run(), world.http(), clock.sleep, clock, WALL)


@pytest.fixture(autouse=True)
def plain_ssc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROOFRUN_SSC", "ssc")


def test_the_flow_passes_when_two_runs_appear_on_the_second_poll(isolated: Path) -> None:
    world = World()
    out = go(world)
    assert out.passed is True
    assert out.proof == "GA-4.1"
    assert world.runs_polled == 2
    assert [c["result"] for c in out.data["checks"]] == ["PASS"] * 6
    assert "check 6 PASS" in "\n".join(out.lines)
    assert any("minute: active" in line for line in out.lines)
    assert out.final_line().endswith("PASS")
    saved = list((isolated / "results").glob("timers-timer-*.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text())["verdict"] == "PASS"


def test_the_token_is_never_printed_or_saved(isolated: Path) -> None:
    world = World()
    out = go(world, disable=True)
    text = "\n".join(out.lines) + json.dumps(out.data)
    for path in (isolated / "results").glob("*.json"):
        text += path.read_text()
    assert TOKEN not in text


def test_a_schedule_paused_for_preview_stops_in_phase_one(isolated: Path) -> None:
    world = World(state="paused", reason="preview")
    out = go(world)
    assert out.passed is False
    assert "preview" in out.number
    assert "preview" in "\n".join(out.lines)
    assert world.runs_polled == 0


def test_a_missing_minute_schedule_fails_at_once() -> None:
    world = World()
    http = world.http()
    http.rules[1] = ("/schedules", answer(200, {"environment_id": ENV, "items": []}))
    clock = Clock()
    args = argparse.Namespace(app="timer", env="prod", minutes=4, disable=False)
    out = timers.run(args, world.run(), http, clock.sleep, clock, WALL)
    assert out.passed is False
    assert "missing" in out.number


def test_the_watch_gives_up_after_the_minutes_and_fails_check_one() -> None:
    world = World(runs_by_poll=[[make_run(0)]], logs=Done(0, json.dumps({"lines": []}), ""))
    clock = Clock()
    args = argparse.Namespace(app="timer", env="prod", minutes=1, disable=False)
    out = timers.run(args, world.run(), world.http(), clock.sleep, clock, WALL)
    assert out.passed is False
    assert world.runs_polled == 7
    assert clock.slept[:6] == [10.0] * 6


def test_runs_from_before_the_command_are_left_out() -> None:
    old = [make_run(-7200), make_run(-7140)]
    world = World(runs_by_poll=[old], logs=Done(0, json.dumps({"lines": []}), ""))
    clock = Clock()
    args = argparse.Namespace(app="timer", env="prod", minutes=1, disable=False)
    out = timers.run(args, world.run(), world.http(), clock.sleep, clock, WALL)
    assert out.passed is False
    assert out.data["checks"][0]["result"] == "FAIL"
    assert out.data["runs"] == []
    assert out.data["left_out"] == 2
    assert any(line.startswith("left out 2 older runs") for line in out.lines)


def test_since_keeps_the_cutoff_slot_and_later() -> None:
    kept, left = timers.since([make_run(-60), make_run(0), make_run(60)], BASE)
    assert [r["run_id"] for r in kept] == ["tmr_0", "tmr_60"]
    assert left == 1


def test_unreadable_logs_make_check_six_not_read_and_the_proof_incomplete() -> None:
    world = World(logs=Done(1, "", "boom"))
    out = go(world)
    assert out.passed is None
    assert out.data["checks"][5]["result"] == "not read"
    assert "ssc logs timer --env prod --source app --since" in out.data["checks"][5]["detail"]


def test_logs_are_read_again_until_two_ticks_show() -> None:
    first = Done(0, json.dumps({"lines": tick_lines(1)}), "")
    second = Done(0, json.dumps({"lines": tick_lines(2)}), "")
    reads = [first, second]

    class Seq(FakeRun):
        def __call__(self, argv: Any, **kw: Any) -> Done:
            return reads.pop(0)

    clock = Clock()
    logs = timers.read_logs(Seq(), "timer", "prod", 6, clock.sleep)
    assert (logs.ticks, logs.reads, clock.slept) == (2, 2, [15.0])


def test_a_refused_tick_in_the_logs_fails_check_six() -> None:
    lines = [*tick_lines(2), {"text": "TICK refused=bad_signature"}]
    out = go(World(logs=Done(0, json.dumps({"lines": lines}), "")))
    assert out.passed is False
    assert out.data["checks"][5]["result"] == "FAIL"


def test_disable_pauses_and_enable_resumes_the_schedule() -> None:
    world = World()
    out = go(world, disable=True)
    assert world.commands == ["disable", "enable"]
    assert out.passed is True
    assert [c["n"] for c in out.data["checks"]] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert out.data["disable_s"] == 0
    assert any(line.startswith("ssc enable returned") for line in out.lines)


def test_enable_runs_even_when_the_pause_check_fails() -> None:
    world = World()
    run = world.run()
    clock = Clock()
    args = argparse.Namespace(app="timer", env="prod", minutes=4, disable=True)

    class Stuck(FakeRun):
        def __call__(self, argv: Any, **kw: Any) -> Done:
            if "disable" in argv:
                world.commands.append("disable")
                return Done(1, "", "no")
            return run(argv, **kw)

    out = timers.run(args, Stuck(), world.http(), clock.sleep, clock, WALL)
    assert world.commands == ["disable", "enable"]
    assert out.passed is False
    assert out.data["checks"][6]["result"] == "FAIL"


def test_a_failed_enable_prints_the_undo() -> None:
    world = World()
    run = world.run()
    clock = Clock()
    args = argparse.Namespace(app="timer", env="prod", minutes=4, disable=True)

    class NoEnable(FakeRun):
        def __call__(self, argv: Any, **kw: Any) -> Done:
            if "enable" in argv:
                return Done(1, "", "refused")
            return run(argv, **kw)

    out = timers.run(args, NoEnable(), world.http(), clock.sleep, clock, WALL)
    assert "undo: ssc enable timer" in out.lines
    assert out.passed is False


def test_a_failed_control_api_read_stops_with_the_status_only() -> None:
    http = FakeHttp([("/v1/apps", answer(403, b"nope"))])
    clock = Clock()
    args = argparse.Namespace(app="timer", env="prod", minutes=1, disable=False)
    with pytest.raises(CommandError, match="answered 403") as info:
        timers.run(args, World().run(), http, clock.sleep, clock)
    assert TOKEN not in str(info.value)


def test_resolve_takes_an_app_id_without_listing() -> None:
    http = World().http()
    api = timers.Api(http, "https://api.example.com", TOKEN)
    assert timers.resolve(api, APP, "prod") == (APP, ENV)
    assert [url for url, _ in http.calls] == [f"https://api.example.com/v1/apps/{APP}"]
    with pytest.raises(CommandError, match="no preview"):
        timers.resolve(api, APP, "preview")


def test_timers_is_a_registered_command() -> None:
    assert PROOFS["timers"] is timers
    args = parser().parse_args(["timers", "--app", "timer"])
    assert (args.env, args.minutes, args.disable) == ("prod", 4, False)
    assert parser().parse_args(["timers", "--app", "x", "--disable"]).disable is True


MANIFEST = KIT / "apps" / "timer" / "ssc.toml"


def test_the_fixture_manifest_declares_the_one_minute_schedule() -> None:
    data = tomllib.loads(MANIFEST.read_text())
    assert data["schema"] == "ssc/v1"
    assert data["runtime"] == {
        "start": "uvicorn main:app --host 0.0.0.0 --port $PORT",
        "health_path": "/health",
    }
    assert data["schedules"] == [
        {
            "name": "minute",
            "cron": "* * * * *",
            "timezone": "UTC",
            "path": "/tick",
            "method": "POST",
            "timeout_seconds": 60,
        }
    ]


def test_the_fixture_manifest_loads_with_the_products_loader() -> None:
    python = REPO / ".venv" / "bin" / "python"
    if not python.exists():
        pytest.skip("the repository environment is not synced here")
    code = (
        "import sys\n"
        "from ssc_contracts.manifest import load_manifest\n"
        "m = load_manifest(open(sys.argv[1], 'rb').read())\n"
        "print(m.schedules[0].name, m.runtime.health_path)\n"
    )
    done = subprocess.run(  # noqa: S603
        [str(python), "-c", code, str(MANIFEST)], capture_output=True, text=True, check=False
    )
    if "No module named" in done.stderr:
        pytest.skip("ssc_contracts cannot be imported here")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "minute /health"


def test_the_fixture_vendors_the_identity_helper_unchanged() -> None:
    app = KIT / "apps" / "timer"
    packages = REPO / "packages"
    pairs = [
        (app / "ssc_app" / "identity.py", packages / "ssc_app/src/ssc_app/identity.py"),
        (app / "ssc_app" / "__init__.py", packages / "ssc_app/src/ssc_app/__init__.py"),
        (
            app / "ssc_contracts" / "identity.py",
            packages / "ssc_contracts/src/ssc_contracts/identity.py",
        ),
        (
            app / "ssc_contracts" / "__init__.py",
            packages / "ssc_contracts/src/ssc_contracts/__init__.py",
        ),
    ]
    for copy, original in pairs:
        if not original.exists():
            pytest.skip("the packages are not in this checkout")
        assert copy.read_bytes() == original.read_bytes(), copy
