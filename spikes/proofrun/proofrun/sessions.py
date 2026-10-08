"""GA-4.7: the two session helpers (SSC-090) held past the 60-minute mark, live.

``sessions [--minutes 70] [--hosts <host>,<host>] [--streamlit-host <host>]``

A thin wrapper: ``apps/reconnect/check.py`` is the measurement. The kit runs it once per host, both
at the same time, as two subprocesses (so a crash of one leaves the other running, and check.py
stays unchanged), one reader thread each. It prints every line with a ``[prcpy]``/``[prcnode]``
prefix, parses the lines and records the result. The cookie is never handed over: check.py reads
the cookie jar itself on every connect, so no argument, setting or input carries it.

The 1012 close is the helper's own, sent about 30 s before the deadline the gateway announced
(``X-SSC-Request-Deadline``), so at about 59.5 minutes. It is not a gateway cut; the gateway's
3600 s limit is never reached. The helpers only close; the client (check.py here, ``sscSocket`` in a
browser) reconnects.

Per host (hosts 1 and 2 use checks 1 to 4 and 5 to 8):

1. the host has a cookie in the jar (else checks 2 to 4 are "not read": nothing is started for it);
2. check.py's last line is PASS (held 60 minutes or more, one restart or more, no gap);
3. the first ``ended 1012`` connection was held 58.0 to 61.0 minutes, and the 1012 lines match
   the restarts check.py counted;
4. no user action and no gap: one unattended launch (input closed), no ``gap:`` line, and numbers
   went on after the 1012 on a new connection. Other ends are listed, never failed.

Check 9 is the Streamlit screenshot ``results/ga-4.7-streamlit.png``, taken by a person: reported
"manual: present/absent", and it never sets the verdict. Pass: every automatic check passed.
"""

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import IO, Any, Final, Protocol

from proofrun.common import (
    KIT,
    CommandError,
    CookieError,
    CookieJar,
    Outcome,
    cookie_host,
    fence,
    last_line,
    results_dir,
)
from proofrun.rollback import Check, verdict

CHECK_PY: Final = KIT / "apps" / "reconnect" / "check.py"
SCREENSHOT: Final = "ga-4.7-streamlit.png"
PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"
DEFAULT_MINUTES: Final = 70.0
MIN_MINUTES: Final = 62.0
DROP_MIN: Final = 58.0
DROP_MAX: Final = 61.0
SHOT_AFTER_MIN: Final = 61
HOSTS: Final = (
    "prcpy--preview.proofcell01.delimitusapps.com",
    "prcnode--preview.proofcell01.delimitusapps.com",
)
STREAMLIT_SLUG: Final = "pstream"
RESTART: Final = "1012"
CONNECTION: Final = re.compile(
    r"(\d\d:\d\d:\d\d) connection held (\d+(?:\.\d+)?) min, last (-?\d+), ended (.*)"
)
FINAL: Final = re.compile(
    r"(PASS|FAIL): (\d+(?:\.\d+)?) min, (\d+) restart\(s\) on 1012, (\d+) other end\(s\), "
    r"(\d+) gap\(s\), last number (-?\d+)"
)


def minutes_arg(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {raw!r}") from None
    if value < MIN_MINUTES:
        raise argparse.ArgumentTypeError(
            f"{value:g} is too short: check.py passes only after 60 minutes with a 1012 near "
            f"59.5, so use {MIN_MINUTES:g} or more"
        )
    return value


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--minutes", type=minutes_arg, default=DEFAULT_MINUTES, help="default 70")
    parser.add_argument(
        "--hosts",
        default=",".join(HOSTS),
        help="the session apps' preview hosts, comma separated (prcpy, prcnode)",
    )
    parser.add_argument(
        "--streamlit-host",
        default=None,
        help="host for the screenshot step; default pstream--preview.<the first host's cell>",
    )


class Child(Protocol):
    """One running check.py: its output lines, its exit code, and a way to stop it."""

    def lines(self) -> Iterable[str]: ...

    def wait(self) -> int: ...

    def stop(self) -> None: ...


type Spawn = Callable[[Sequence[str]], Child]


class PopenChild:
    """The real child: argv fenced, input closed, error output merged into the lines."""

    def __init__(self, argv: Sequence[str]) -> None:
        fence(*argv)
        self.proc = subprocess.Popen(  # noqa: S603
            list(argv),
            cwd=KIT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def lines(self) -> Iterable[str]:
        stream: IO[str] | None = self.proc.stdout
        return stream if stream is not None else ()

    def wait(self) -> int:
        return self.proc.wait()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


@dataclass(frozen=True, slots=True)
class Connection:
    """One ``connection held`` line, with when the kit saw it."""

    clock: str
    held: float
    last: int
    ended: str
    seen_at: str
    seen_min: float


@dataclass(frozen=True, slots=True)
class Last:
    """check.py's last line."""

    passed: bool
    minutes: float
    restarts: int
    other: int
    gaps: int
    last: int
    text: str


@dataclass
class HostRun:
    """Everything about one host's check.py."""

    host: str
    label: str
    cookie_error: str | None = None
    started: bool = False
    error: str | None = None
    returncode: int | None = None
    lines: list[str] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)
    gap_lines: int = 0
    final: Last | None = None


@dataclass
class Board:
    """What the reader threads share: the printer, the clocks and the values to scrub."""

    say: Callable[[str], None]
    clock: Callable[[], float]
    wall: Callable[[], datetime]
    t0: float
    secrets: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def scrub(self, line: str) -> str:
        for value in self.secrets:
            line = line.replace(value, "[cookie]")
        return line

    def print(self, label: str, line: str) -> None:
        with self.lock:
            self.say(f"[{label}] {line}")


def label_of(host: str) -> str:
    return host.split(".", 1)[0].split("--", 1)[0]


def parse_final(line: str) -> Last | None:
    m = FINAL.fullmatch(line.strip())
    if m is None:
        return None
    word, minutes, restarts, other, gaps, last = m.groups()
    return Last(
        word == "PASS",
        float(minutes),
        int(restarts),
        int(other),
        int(gaps),
        int(last),
        line.strip(),
    )


def note(host: HostRun, line: str, board: Board) -> None:
    """Read one check.py line into the host's findings."""
    text = line.strip()
    m = CONNECTION.fullmatch(text)
    if m is not None:
        clock, held, last, ended = m.groups()
        seen = board.wall().strftime("%Y-%m-%dT%H:%M:%SZ")
        minutes = round((board.clock() - board.t0) / 60, 2)
        host.connections.append(Connection(clock, float(held), int(last), ended, seen, minutes))
    elif text.startswith("gap:"):
        host.gap_lines += 1
    else:
        host.final = parse_final(text) or host.final


def watch(host: HostRun, children: list[Child], spawn: Spawn, board: Board, minutes: float) -> None:
    """Run check.py for one host, printing and reading its lines until it ends."""
    argv = [sys.executable, "-u", str(CHECK_PY), host.host, "--minutes", f"{minutes:g}"]
    try:
        child = spawn(argv)
    except OSError as exc:
        host.error = f"could not start check.py: {type(exc).__name__}"
        return
    children.append(child)
    host.started = True
    for raw in child.lines():
        line = board.scrub(raw.rstrip("\r\n"))
        if line.strip():
            host.lines.append(line)
            note(host, line, board)
            board.print(host.label, line)
    host.returncode = child.wait()


def first_drop(host: HostRun) -> Connection | None:
    return next((c for c in host.connections if c.ended == RESTART), None)


NAMES: Final = {
    1: "cookie in the jar",
    2: "check.py ends PASS (60 min or more held, a 1012 restart, no gap)",
    3: f"the helper's 1012 near the 60-minute mark ({DROP_MIN:g} to {DROP_MAX:g} min)",
    4: "reconnected with no user action and no gap",
}


def checks_for(index: int, host: HostRun) -> list[Check]:
    """Checks ``4 * index + 1`` to ``4 * index + 4`` for one host."""
    base = 4 * index
    found = None if host.cookie_error is not None else True
    jar = Check(base + 1, title(host, 1), found, host.cookie_error or "found")
    if host.cookie_error is not None:
        why = "not run: no cookie"
        return [jar] + [Check(base + k, title(host, k), None, why) for k in (2, 3, 4)]
    return [jar, final_check(base, host), drop_check(base, host), unattended_check(base, host)]


def title(host: HostRun, k: int) -> str:
    return f"{host.label}: {NAMES[k]}"


def not_finished(host: HostRun) -> str:
    why = host.error or f"check.py printed no final line (exit {host.returncode})"
    return f"{why}; last: {last_line(chr(10).join(host.lines))[:120]}" if host.lines else why


def final_check(base: int, host: HostRun) -> Check:
    if host.final is None:
        return Check(base + 2, title(host, 2), None, not_finished(host))
    return Check(base + 2, title(host, 2), host.final.passed, host.final.text)


def drop_check(base: int, host: HostRun) -> Check:
    n, name = base + 3, title(host, 3)
    drop = first_drop(host)
    count = sum(c.ended == RESTART for c in host.connections)
    if drop is None:
        if host.final is None:
            return Check(n, name, None, not_finished(host))
        return Check(n, name, False, "no connection ended 1012")
    detail = (
        f"first 1012 after {drop.held:.1f} min at {drop.seen_at} ({drop.seen_min:.1f} min into the "
        f"run), last number {drop.last}; {count} reconnect(s)"
    )
    agrees = host.final is None or host.final.restarts == count
    if not agrees and host.final is not None:
        detail += f"; check.py counted {host.final.restarts}"
    return Check(n, name, DROP_MIN <= drop.held <= DROP_MAX and agrees, detail)


def other_ends(host: HostRun) -> list[str]:
    return [f"{c.ended} at {c.clock}" for c in host.connections if c.ended not in {RESTART, "held"}]


def unattended_check(base: int, host: HostRun) -> Check:
    n, name = base + 4, title(host, 4)
    drop = first_drop(host)
    tail = f"; other ends: {', '.join(other_ends(host)) or 'none'}"
    if host.final is None and drop is None:
        return Check(n, name, None, not_finished(host))
    if host.gap_lines or (host.final is not None and host.final.gaps):
        return Check(n, name, False, f"{host.gap_lines} gap line(s)" + tail)
    later = []
    if drop is not None:
        after = host.connections[host.connections.index(drop) + 1 :]
        later = [c for c in after if c.last > drop.last]
    if not later:
        return Check(n, name, False, "no connection with later numbers after the 1012" + tail)
    return Check(
        n,
        name,
        True,
        f"one launch, input closed; numbers went on to {later[-1].last}, 0 gaps" + tail,
    )


def screenshot_check(n: int) -> Check:
    path = results_dir() / SCREENSHOT
    try:
        present = path.is_file() and path.read_bytes()[:8] == PNG_MAGIC
    except OSError:
        present = False
    name = f"Streamlit 60-minute reload screenshot results/{SCREENSHOT}"
    return Check(n, name, None, "present" if present else "absent", manual=True)


def hosts_of(raw: str) -> list[str]:
    hosts = [cookie_host(h) for h in raw.split(",") if h.strip()]
    if not hosts:
        raise CommandError("--hosts names no host")
    if len(set(hosts)) != len(hosts) or len({label_of(h) for h in hosts}) != len(hosts):
        raise CommandError("--hosts names the same app twice")
    return hosts


def streamlit_host_of(arg: str | None, first: str) -> str:
    if arg:
        return cookie_host(arg)
    _, _, cell = first.partition(".")
    if not cell:
        raise CommandError(f"cannot take the cell from {first!r}: pass --streamlit-host")
    return f"{STREAMLIT_SLUG}--preview.{cell}"


def streamlit_lines(host: str, started: datetime, minutes: float) -> list[str]:
    """What the person does, with the times worked out from the start."""
    stamp = "%H:%M:%S UTC"
    t0, shot = (
        started.strftime(stamp),
        (started + timedelta(minutes=SHOT_AFTER_MIN)).strftime(stamp),
    )
    return [
        f"Streamlit screenshot (manual), host https://{host}/ (app {STREAMLIT_SLUG}, preview):",
        f"  1. At {t0} (now) open it in a signed-in browser and note the caption 'page served at"
        " HH:MM:SS UTC'.",
        "  2. Leave the tab open and untouched: no reload, no sleep (keep the machine awake).",
        f"  3. At {shot} or a few minutes after, and before the run ends, take a screenshot of the"
        " whole window.",
        "  must show: the URL bar with the host, the title 'SSC proof run: Streamlit + pandas',"
        f" the caption with a later time (about {SHOT_AFTER_MIN - 1} minutes after the first, not"
        " the first) and no 'Connecting' banner, and the clock.",
        f"  save it as {results_dir() / SCREENSHOT}; the kit reports it present or absent at the"
        " end and the verdict does not depend on it.",
    ]


def start_lines(hosts: Sequence[str], minutes: float, started: datetime) -> list[str]:
    stamp = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        f"sessions: check.py on {', '.join(hosts)} at the same time for {minutes:g} min, from "
        f"{stamp}.",
        "the 1012 is the helper's own close about 30 s before the deadline (about 59.5 min), not a "
        "gateway cut; the client reconnects.",
    ]


def host_data(host: HostRun) -> dict[str, Any]:
    drops = [c for c in host.connections if c.ended == RESTART]
    return {
        "host": host.host,
        "cookie_error": host.cookie_error,
        "error": host.error,
        "exit_code": host.returncode,
        "final": host.final.text if host.final else None,
        "reconnects": len(drops),
        "drops": [
            {
                "seen_at": c.seen_at,
                "minutes_into_run": c.seen_min,
                "held_min": c.held,
                "last_number": c.last,
                "check_py_clock": c.clock,
            }
            for c in drops
        ],
        "other_ends": [
            f"{c.ended} at {c.clock}" for c in host.connections if c.ended not in {RESTART, "held"}
        ],
        "gap_lines": host.gap_lines,
        "lines": host.lines,
    }


def save(outcome: Outcome) -> None:
    """The per-run file beside the shared history; it holds no cookie."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = results_dir()
    folder.mkdir(parents=True, exist_ok=True)
    body = {
        "verdict": outcome.verdict,
        "number": outcome.number,
        "lines": outcome.lines,
        **outcome.data,
    }
    (folder / f"sessions-{stamp}.json").write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n"
    )


def number_of(checks: Sequence[Check], runs: Sequence[HostRun]) -> str:
    automatic = [c for c in checks if not c.manual]
    passed = sum(c.result is True for c in automatic)
    drops = ", ".join(
        f"{h.label} 1012 at {d.held:.1f} min" for h in runs if (d := first_drop(h)) is not None
    )
    shot = next(c.detail for c in checks if c.manual)
    seen = drops or "no 1012 seen"
    return f"{passed} of {len(automatic)} automatic checks passed; {seen}; screenshot {shot}"


def prepare(hosts: Sequence[str], board: Board) -> list[HostRun]:
    """One :class:`HostRun` per host; a host without a usable cookie is marked and not started."""
    jar, runs = CookieJar(), []
    for host in hosts:
        entry = HostRun(host, label_of(host))
        try:
            board.secrets.append(jar.get(host).value)
        except CookieError as exc:
            entry.cookie_error = str(exc)
        runs.append(entry)
    return runs


def run(
    args: argparse.Namespace,
    spawn: Spawn = PopenChild,
    say: Callable[[str], None] = print,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Outcome:
    hosts = hosts_of(args.hosts)
    shot_host = streamlit_host_of(args.streamlit_host, hosts[0])
    fence(*hosts, shot_host)
    started = wall()
    board = Board(say, clock, wall, clock())
    runs = prepare(hosts, board)
    opening = start_lines(hosts, args.minutes, started) + streamlit_lines(
        shot_host, started, args.minutes
    )
    for line in opening:
        say(line)
    for entry in runs:
        if entry.cookie_error is not None:
            board.print(entry.label, f"not started: {entry.cookie_error}")
    children: list[Child] = []
    threads = [
        threading.Thread(target=watch, args=(e, children, spawn, board, args.minutes), daemon=True)
        for e in runs
        if e.cookie_error is None
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for child in children:
            child.stop()
        raise
    checks = [c for i, e in enumerate(runs) for c in checks_for(i, e)]
    checks.append(screenshot_check(4 * len(runs) + 1))
    lines = [c.line() for c in checks] + streamlit_lines(shot_host, started, args.minutes)
    data: dict[str, Any] = {
        "minutes": args.minutes,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "the 1012 is the helper's close ahead of the gateway deadline, not a gateway cut",
        "streamlit_host": shot_host,
        "screenshot": f"results/{SCREENSHOT}",
        "hosts": {e.label: host_data(e) for e in runs},
        "checks": [c.data() for c in checks],
    }
    outcome = Outcome("GA-4.7", number_of(checks, runs), verdict(checks), opening[:2] + lines, data)
    save(outcome)
    return outcome
