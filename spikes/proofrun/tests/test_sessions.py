import argparse
import json
import sys
import threading
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from proofrun import sessions
from proofrun.__main__ import PROOFS, parser
from proofrun.common import CommandError, CookieJar, FencedError, Outcome, emit, results_dir

PY = "prcpy--preview.proofcell01.delimitusapps.com"
NODE = "prcnode--preview.proofcell01.delimitusapps.com"
SECRET = "sentinelcookievalue123456"
OTHER_SECRET = "othercookievalue9876543"
START = datetime(2026, 10, 9, 10, 0, 0, tzinfo=UTC)


def final(word: str = "PASS", restarts: int = 1, other: int = 0, gaps: int = 0) -> str:
    return (
        f"{word}: 70.0 min, {restarts} restart(s) on 1012, {other} other end(s), "
        f"{gaps} gap(s), last number 4200"
    )


FINAL_OK = final()


def good(held: float = 59.5, final: str | None = FINAL_OK, extra: Sequence[str] = ()) -> list[str]:
    lines = [
        f"10:59:31 connection held {held} min, last 3570, ended 1012",
        *extra,
        "11:10:00 connection held 10.5 min, last 4200, ended held",
    ]
    return lines + ([final] if final else [])


class FakeChild:
    def __init__(self, lines: Iterable[str], gate: threading.Barrier | None = None) -> None:
        self._lines = list(lines)
        self.gate = gate
        self.stopped = False

    def lines(self) -> Iterable[str]:
        if self.gate is not None:
            self.gate.wait(timeout=5)
        yield from (line + "\n" for line in self._lines)

    def wait(self) -> int:
        return 0

    def stop(self) -> None:
        self.stopped = True


class Spawner:
    """Answers each spawn from the host in argv, and records the argv."""

    def __init__(self, scripts: dict[str, Sequence[str]], gate: threading.Barrier | None = None):
        self.scripts, self.gate = scripts, gate
        self.argvs: list[list[str]] = []
        self.children: list[FakeChild] = []

    def __call__(self, argv: Sequence[str]) -> FakeChild:
        self.argvs.append(list(argv))
        host = argv[3]
        child = FakeChild(self.scripts[host], self.gate)
        self.children.append(child)
        return child


def seal(*hosts: str) -> None:
    jar = CookieJar()
    for host, value in zip(hosts, (SECRET, OTHER_SECRET), strict=False):
        jar.put(host, value, "browser")


def drive(
    spawn: Spawner,
    hosts: str = f"{PY},{NODE}",
    minutes: float = 70.0,
    streamlit_host: str | None = None,
) -> tuple[Outcome, list[str]]:
    said: list[str] = []
    args = argparse.Namespace(minutes=minutes, hosts=hosts, streamlit_host=streamlit_host)
    outcome = sessions.run(
        args, spawn=spawn, say=said.append, clock=lambda: 1000.0, wall=lambda: START
    )
    return outcome, said


def both(scripts: dict[str, Sequence[str]] | None = None) -> Spawner:
    return Spawner(scripts or {PY: good(), NODE: good()})


def results(outcome: Outcome) -> dict[int, dict[str, str]]:
    return {c["n"]: c for c in outcome.data["checks"]}


def test_parse_final_reads_pass_and_fail_and_ignores_other_lines() -> None:
    ok = sessions.parse_final(FINAL_OK)
    assert ok is not None
    assert (ok.passed, ok.minutes, ok.restarts, ok.other, ok.gaps, ok.last) == (
        True,
        70.0,
        1,
        0,
        0,
        4200,
    )
    bad = sessions.parse_final(
        "FAIL: 61.0 min, 0 restart(s) on 1012, 2 other end(s), 1 gap(s), last number 9"
    )
    assert bad is not None
    assert (bad.passed, bad.restarts, bad.other, bad.gaps) == (False, 0, 2, 1)
    assert sessions.parse_final("10:00:00 connection held 1.0 min, last 3, ended held") is None
    assert sessions.parse_final("PASS: soon") is None


def test_two_healthy_hosts_pass_every_automatic_check() -> None:
    seal(PY, NODE)
    outcome, said = drive(both())
    checks = results(outcome)
    assert outcome.passed is True
    assert [checks[n]["result"] for n in range(1, 9)] == ["PASS"] * 8
    assert checks[9]["result"] == "manual"
    assert outcome.final_line() == "GA-4.7 " + outcome.number + " PASS"
    assert "8 of 8 automatic checks passed" in outcome.number
    assert checks[1]["name"].startswith("prcpy:") and checks[5]["name"].startswith("prcnode:")
    assert any(line.startswith("[prcpy] 10:59:31 connection held 59.5") for line in said)
    assert any(line.startswith("[prcnode] PASS: 70.0 min") for line in said)
    data = outcome.data["hosts"]["prcpy"]
    assert data["reconnects"] == 1
    assert data["drops"][0]["held_min"] == 59.5
    assert data["drops"][0]["last_number"] == 3570
    assert data["drops"][0]["seen_at"] == "2026-10-09T10:00:00Z"


def test_check_py_runs_unchanged_with_the_host_and_minutes_and_no_cookie_in_argv() -> None:
    seal(PY, NODE)
    spawn = both()
    drive(spawn, minutes=65)
    assert sorted(a[3] for a in spawn.argvs) == [NODE, PY]
    for argv in spawn.argvs:
        assert argv[:3] == [sys.executable, "-u", str(sessions.CHECK_PY)]
        assert argv[4:] == ["--minutes", "65"]
        assert SECRET not in " ".join(argv)
    assert sessions.CHECK_PY.name == "check.py" and sessions.CHECK_PY.is_file()


def test_both_hosts_run_at_the_same_time() -> None:
    seal(PY, NODE)
    spawn = Spawner({PY: good(), NODE: good()}, gate=threading.Barrier(2))
    outcome, _ = drive(spawn)
    assert outcome.passed is True  # a broken barrier would have left a host not read


def test_a_missing_cookie_makes_that_host_not_read_and_the_other_still_runs() -> None:
    seal(PY)
    spawn = Spawner({PY: good()})
    outcome, said = drive(spawn)
    checks = results(outcome)
    assert [a[3] for a in spawn.argvs] == [PY]
    assert [checks[n]["result"] for n in (5, 6, 7, 8)] == ["not read"] * 4
    assert [checks[n]["result"] for n in range(1, 5)] == ["PASS"] * 4
    assert f"cookie set {NODE}" in checks[5]["detail"]
    assert outcome.passed is None
    assert outcome.verdict == "INCOMPLETE"
    assert any(
        line.startswith("[prcnode] not started:") and f"cookie set {NODE}" in line for line in said
    )


def test_the_cookie_never_reaches_printed_lines_or_saved_files() -> None:
    seal(PY, NODE)
    echo = "WARN cookie __Host-ssc-session=" + SECRET
    other = "WARN " + OTHER_SECRET
    spawn = Spawner({PY: [echo, *good()], NODE: [other, *good()]})
    outcome, said = drive(spawn)
    emit(outcome)
    folder = results_dir()
    saved = "".join(p.read_text() for p in folder.glob("*.json"))
    assert saved
    jar_path = str(CookieJar().path)
    for text in (*said, saved, json.dumps(outcome.data), "\n".join(outcome.lines)):
        assert SECRET not in text
        assert OTHER_SECRET not in text
        assert jar_path not in text
    assert any("[cookie]" in line for line in said)


def test_results_are_saved_twice_history_and_stamped() -> None:
    seal(PY, NODE)
    outcome, _ = drive(both())
    emit(outcome)
    names = sorted(p.name for p in results_dir().iterdir())
    assert "ga-4.7.json" in names
    assert any(n.startswith("sessions-") and n.endswith(".json") for n in names)
    stamped = next(results_dir().glob("sessions-*.json"))
    body = json.loads(stamped.read_text())
    assert body["verdict"] == "PASS"
    assert "gateway cut" in body["note"]
    assert body["hosts"]["prcnode"]["final"] == FINAL_OK


def test_a_fenced_host_is_refused_before_anything_starts(fake_digest: str) -> None:
    seal(PY)
    spawn = Spawner({PY: good()})
    with pytest.raises(FencedError):
        drive(spawn, hosts=f"{PY},{fake_digest}.example.com")
    assert spawn.argvs == []
    with pytest.raises(FencedError):
        drive(spawn, hosts=PY, streamlit_host=fake_digest + ".example.com")
    assert spawn.argvs == []


def test_the_real_child_fences_its_argv(fake_digest: str) -> None:
    with pytest.raises(FencedError):
        sessions.PopenChild([sys.executable, "-c", "pass", fake_digest])


def test_minutes_below_62_are_refused_by_the_command() -> None:
    top = parser()
    assert top.parse_args(["sessions"]).minutes == 70.0
    assert top.parse_args(["sessions", "--minutes", "62"]).minutes == 62.0
    for bad in ("61", "30", "abc"):
        with pytest.raises(SystemExit):
            top.parse_args(["sessions", "--minutes", bad])


def test_the_command_is_registered_with_its_defaults() -> None:
    assert PROOFS["sessions"] is sessions
    args = parser().parse_args(["sessions"])
    assert args.hosts.split(",") == [PY, NODE]
    assert args.streamlit_host is None


@pytest.mark.parametrize(
    ("scripts", "verdict"),
    [
        ({PY: good(), NODE: good()}, "PASS"),
        # check.py says FAIL
        (
            {
                PY: good(final=final("FAIL", gaps=1)),
                NODE: good(),
            },
            "FAIL",
        ),
        # the 1012 came at 50 minutes, outside 58.0 to 61.0
        ({PY: good(held=50.0), NODE: good()}, "FAIL"),
        # no 1012 at all
        (
            {
                PY: [
                    "11:10:00 connection held 70.0 min, last 4200, ended held",
                    final("FAIL", restarts=0),
                ],
                NODE: good(),
            },
            "FAIL",
        ),
        # a gap line
        ({PY: good(extra=["gap: 10 then 12"]), NODE: good()}, "FAIL"),
        # check.py counted a restart the kit never saw
        (
            {
                PY: good(final=final(restarts=2)),
                NODE: good(),
            },
            "FAIL",
        ),
        # the child died with no final line
        ({PY: ["Traceback (most recent call last):"], NODE: good()}, "INCOMPLETE"),
        ({PY: [], NODE: good()}, "INCOMPLETE"),
        # other ends are listed, never failed
        (
            {
                PY: good(
                    extra=["10:20:00 connection held 20.0 min, last 1200, ended no close frame"],
                    final=final(other=1),
                ),
                NODE: good(),
            },
            "PASS",
        ),
    ],
)
def test_verdict_rule(scripts: dict[str, Sequence[str]], verdict: str) -> None:
    seal(PY, NODE)
    outcome, _ = drive(Spawner(scripts))
    assert outcome.verdict == verdict


def test_other_ends_appear_in_the_detail() -> None:
    seal(PY, NODE)
    extra = ["10:20:00 connection held 20.0 min, last 1200, ended no close frame"]
    outcome, _ = drive(both({PY: good(extra=extra), NODE: good()}))
    assert "no close frame at 10:20:00" in results(outcome)[4]["detail"]
    assert outcome.data["hosts"]["prcpy"]["other_ends"] == ["no close frame at 10:20:00"]


def test_the_screenshot_is_reported_by_hand_and_never_sets_the_verdict() -> None:
    seal(PY, NODE)
    outcome, _ = drive(both())
    assert results(outcome)[9]["detail"] == "absent"
    assert outcome.passed is True
    assert any("check 9 manual:" in line and "(absent)" in line for line in outcome.lines)
    folder = results_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / sessions.SCREENSHOT).write_bytes(sessions.PNG_MAGIC + b"data")
    outcome, _ = drive(both())
    assert results(outcome)[9]["detail"] == "present"
    assert any("check 9 manual:" in line and "(present)" in line for line in outcome.lines)
    assert outcome.passed is True
    (folder / sessions.SCREENSHOT).write_bytes(b"not a png")
    outcome, _ = drive(both())
    assert results(outcome)[9]["detail"] == "absent"


def test_the_streamlit_steps_are_printed_at_the_start_with_the_times() -> None:
    seal(PY, NODE)
    _, said = drive(both())
    text = "\n".join(said)
    assert "https://pstream--preview.proofcell01.delimitusapps.com/" in text
    assert "10:00:00 UTC" in text and "11:01:00 UTC" in text
    assert "helper's own close" in text and "not a gateway cut" in text
    assert str(results_dir() / sessions.SCREENSHOT) in text


def test_a_host_and_the_streamlit_host_can_be_given() -> None:
    assert sessions.hosts_of(f"https://{PY}/, {NODE}") == [PY, NODE]
    assert sessions.streamlit_host_of(None, PY) == "pstream--preview.proofcell01.delimitusapps.com"
    assert sessions.streamlit_host_of("https://x.example.com/", PY) == "x.example.com"
    with pytest.raises(CommandError):
        sessions.hosts_of(f"{PY},{PY}")
    with pytest.raises(CommandError):
        sessions.hosts_of(" , ")


def test_the_real_child_streams_lines_and_closes_input() -> None:
    code = "import sys; print('one', flush=True); print(sys.stdin.read() == '', flush=True)"
    child = sessions.PopenChild([sys.executable, "-I", "-c", code])
    assert [line.strip() for line in child.lines()] == ["one", "True"]
    assert child.wait() == 0
    child.stop()


def test_an_interrupt_stops_the_children(monkeypatch: pytest.MonkeyPatch) -> None:
    seal(PY)
    spawn = Spawner({PY: good()})

    def interrupted(self: threading.Thread, timeout: float | None = None) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(threading.Thread, "join", interrupted)
    with pytest.raises(KeyboardInterrupt):
        drive(spawn, hosts=PY)


def test_check_py_is_untouched_and_imports_the_kit() -> None:
    text = Path(sessions.CHECK_PY).read_text()
    assert "from proofrun.common import CookieJar, session_headers" in text
