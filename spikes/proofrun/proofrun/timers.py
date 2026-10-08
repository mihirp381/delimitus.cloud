"""GA-4.1: timers live, a one-minute schedule on a real cell, read through the control API.

``timers --app <slug> [--env prod] [--minutes 4] [--disable]``

1. Lists the environment's schedules and stops at once unless ``minute`` is ``active``.
2. Watches ``minute``'s runs every 10 s, until two scheduled runs have succeeded or ``--minutes``
   are up, and prints them newest first. Runs scheduled before the
   command started are left out of the table and the checks; one line says how many.
3. Checks 1 to 5 on that history: two succeeded runs with HTTP 200, exactly 60 s apart, no overlap,
   each started within 15 s of its slot, each with ``start_ms`` and ``duration_ms``.
4. Check 6 reads ``ssc logs --source app`` for two ``TICK role=schedule`` lines (the app prints one
   only after the gateway admitted the schedule token and the app verified the note) and no
   ``TICK refused=`` line. When the logs cannot be read the check is "not read", not a fail.
5. **[real]** With ``--disable``: runs ``ssc disable``, check 7 is the schedule ``paused`` for
   ``app_disabled``; then ``ssc enable`` (always), check 8 is it ``active`` with ``next_run_at``.

The app (``apps/timer``) must be deployed to prod (``ssc deploy`` then ``ssc promote``): preview
schedules are stored paused. Pass: every check that ran passed. The kit reads the control API and
runs only ``ssc logs``, and with ``--disable`` ``ssc disable`` and ``ssc enable``.
"""

import argparse
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from proofrun import t8
from proofrun.common import (
    REPO,
    CommandError,
    Http,
    Outcome,
    Run,
    fetch,
    parse_time,
    results_dir,
    run_command,
    ssc_error,
    ssc_prefix,
)

SCHEDULE: Final = "minute"
POLL_S: Final = 10.0
SPACING_S: Final = 60.0
LATE_S: Final = 15.0
TERMINAL: Final = frozenset({"succeeded", "failed", "timed_out", "skipped"})
LOG_READS: Final = 3
LOG_WAIT_S: Final = 15.0
SETTLE_S: Final = 60.0
SETTLE_POLL_S: Final = 5.0
RUNS_LIMIT: Final = 20
TICK: Final = "TICK role=schedule"
REFUSED: Final = "TICK refused="


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app", required=True, help="the timer app's slug")
    parser.add_argument("--env", default="prod", help="prod: preview schedules are stored paused")
    parser.add_argument("--minutes", type=int, default=4, help="how long to wait for two runs")
    parser.add_argument("--disable", action="store_true", help="[real] ssc disable, then enable")


@dataclass(frozen=True, slots=True)
class Check:
    """One numbered check; ``result`` is None when it could not be read."""

    n: int
    name: str
    result: bool | None
    detail: str

    @property
    def word(self) -> str:
        return {True: "PASS", False: "FAIL", None: "not read"}[self.result]

    def line(self) -> str:
        return f"check {self.n} {self.word}: {self.name} ({self.detail})"

    def data(self) -> dict[str, Any]:
        return {"n": self.n, "name": self.name, "result": self.word, "detail": self.detail}


@dataclass
class Api:
    """GET requests to the control API; the token stays in the header."""

    http: Http
    url: str
    token: str

    def get(self, path: str) -> dict[str, Any]:
        answer = self.http(self.url + path, {"Authorization": f"Bearer {self.token}"}, 30.0)
        if answer.status != 200:
            raise CommandError(f"GET {path} answered {answer.status or answer.error}")
        return json.loads(answer.body)


@dataclass
class Logs:
    """What ``ssc logs`` showed: ticks and refusals counted, or why it could not be read."""

    ticks: int = 0
    refused: int = 0
    error: str | None = None
    reads: int = 0
    command: str = ""


@dataclass
class Findings:
    checks: list[Check] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


def when(run: Mapping[str, Any], key: str) -> datetime | None:
    value = run.get(key)
    return parse_time(value) if value else None


def is_terminal(run: Mapping[str, Any]) -> bool:
    return run.get("state") in TERMINAL


def succeeded(runs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Scheduled runs that succeeded with HTTP 200, oldest first."""
    found = [
        r
        for r in runs
        if r.get("trigger") == "schedule" and r.get("state") == "succeeded"
        if r.get("http_status") == 200
    ]
    return sorted(found, key=lambda r: parse_time(r["scheduled_for"]))


def latest_two(runs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The two most recent succeeded scheduled runs, oldest first."""
    return succeeded(runs)[-2:]


def lateness_s(run: Mapping[str, Any]) -> float | None:
    """Seconds from the slot a run was for to the moment it started."""
    started, slot = when(run, "started_at"), when(run, "scheduled_for")
    return None if started is None or slot is None else (started - slot).total_seconds()


def check_succeeded(runs: Sequence[Mapping[str, Any]]) -> Check:
    found = succeeded(runs)
    return Check(1, "two scheduled runs succeeded with 200", len(found) >= 2, f"{len(found)} found")


def check_spacing(runs: Sequence[Mapping[str, Any]]) -> Check:
    name = f"the two latest are exactly {SPACING_S:.0f} s apart"
    pair = latest_two(runs)
    if len(pair) < 2:
        return Check(2, name, False, "fewer than two succeeded runs")
    gap = (
        parse_time(pair[1]["scheduled_for"]) - parse_time(pair[0]["scheduled_for"])
    ).total_seconds()
    return Check(2, name, abs(gap - SPACING_S) < 0.001, f"{gap:.1f} s")


def overlaps(runs: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Pairs of runs whose started-to-finished intervals intersect (touching is not overlap)."""
    timed = [
        (str(r["run_id"]), s, f)
        for r in runs
        if (s := when(r, "started_at")) is not None and (f := when(r, "finished_at")) is not None
    ]
    return [
        (a[0], b[0])
        for i, a in enumerate(timed)
        for b in timed[i + 1 :]
        if a[1] < b[2] and b[1] < a[2]
    ]


def check_overlap(runs: Sequence[Mapping[str, Any]]) -> Check:
    name = "no two runs overlap"
    pairs = overlaps(runs)
    flagged = [str(r["run_id"]) for r in runs if r.get("error") == "overlap"]
    timed = sum(1 for r in runs if r.get("started_at") and r.get("finished_at"))
    detail = (
        f"{timed} timed runs, {len(pairs)} intersecting pairs, {len(flagged)} with error overlap"
    )
    return Check(3, name, not pairs and not flagged, detail)


def check_lateness(runs: Sequence[Mapping[str, Any]]) -> Check:
    name = f"each started within {LATE_S:.0f} s of its slot"
    pair = latest_two(runs)
    late = [lateness_s(r) for r in pair]
    shown = ", ".join("n/a" if v is None else f"{v:.1f} s" for v in late) or "no run"
    ok = len(pair) == 2 and all(v is not None and v <= LATE_S for v in late)  # noqa: PLR2004
    return Check(4, name, ok, shown)


def check_start_ms(runs: Sequence[Mapping[str, Any]]) -> Check:
    name = "history shows start_ms and duration_ms"
    pair = latest_two(runs)
    shown = ", ".join(f"{r.get('start_ms')}/{r.get('duration_ms')} ms" for r in pair) or "no run"
    ok = len(pair) == 2 and all(r.get("start_ms") is not None for r in pair)
    ok = ok and all(r.get("duration_ms") is not None for r in pair)
    return Check(5, name, ok, shown)


def count_ticks(lines: Sequence[str]) -> tuple[int, int]:
    """How many lines are a schedule's tick and how many are a refused call."""
    return sum(TICK in t for t in lines), sum(REFUSED in t for t in lines)


def check_logs(logs: Logs) -> Check:
    name = "the app logged each tick as role schedule, none refused"
    if logs.error is not None and logs.ticks == 0:
        return Check(6, name, None, f"{logs.error}; run by hand: {logs.command}")
    detail = f"{logs.ticks} TICK role=schedule lines, {logs.refused} refused, {logs.reads} read(s)"
    return Check(6, name, logs.ticks >= 2 and logs.refused == 0, detail)


def check_paused(schedule: Mapping[str, Any] | None, seconds: float) -> Check:
    name = f"{SCHEDULE} is paused for app_disabled after ssc disable"
    state = (schedule or {}).get("state")
    reason = (schedule or {}).get("pause_reason")
    return Check(
        7,
        name,
        state == "paused" and reason == "app_disabled",
        f"{state}, {reason}, {seconds:.1f} s",
    )


def check_resumed(schedule: Mapping[str, Any] | None, seconds: float) -> Check:
    name = f"{SCHEDULE} is active with next_run_at after ssc enable"
    state = (schedule or {}).get("state")
    nxt = (schedule or {}).get("next_run_at")
    return Check(8, name, state == "active" and bool(nxt), f"{state}, next {nxt}, {seconds:.1f} s")


def since(runs: Sequence[dict[str, Any]], cutoff: datetime) -> tuple[list[dict[str, Any]], int]:
    """The runs scheduled at or after ``cutoff``, and how many older ones were left out."""
    kept = [r for r in runs if parse_time(r["scheduled_for"]) >= cutoff]
    return kept, len(runs) - len(kept)


def run_table(runs: Sequence[Mapping[str, Any]]) -> list[str]:
    """The runs, newest first, with lateness."""
    head = (
        "scheduled_for | started_at | finished_at | state | error | http | "
        "start_ms | duration_ms | late"
    )
    rows = [head]
    for r in sorted(runs, key=lambda r: parse_time(r["scheduled_for"]), reverse=True):
        late = lateness_s(r)
        rows.append(
            " | ".join(
                [
                    str(r.get("scheduled_for")),
                    str(r.get("started_at")),
                    str(r.get("finished_at")),
                    f"{r.get('state')} ({r.get('trigger')})",
                    str(r.get("error")),
                    str(r.get("http_status")),
                    str(r.get("start_ms")),
                    str(r.get("duration_ms")),
                    "n/a" if late is None else f"{late:.1f} s",
                ]
            )
        )
    return rows


def schedule_line(s: Mapping[str, Any]) -> str:
    return (
        f"schedule {s['name']}: {s['state']}, pause_reason {s.get('pause_reason')}, "
        f"next_run_at {s.get('next_run_at')}, cron {s['cron']}, {s['method']} {s['path']}, "
        f"timeout {s['timeout_seconds']} s"
    )


def resolve(api: Api, ref: str, env_name: str) -> tuple[str, str]:
    """An app id and one environment's id, from a slug (or an ``app_`` id) and a name."""
    app_id = ref
    if not ref.startswith("app_"):
        for entry in api.get("/v1/apps").get("apps", []):
            if entry.get("slug") == ref:
                app_id = entry["id"]
                break
        else:
            raise CommandError(f"no app with slug {ref!r} is visible to you")
    for env in api.get(f"/v1/apps/{app_id}").get("environments", []):
        if env.get("name") == env_name:
            return app_id, env["id"]
    raise CommandError(f"{ref} has no {env_name} environment")


def find(schedules: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return next((s for s in schedules if s.get("name") == SCHEDULE), None)


def watch(  # noqa: PLR0913, PLR0917  (the loop's whole state)
    api: Api,
    path: str,
    minutes: int,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    say: Callable[[str], None],
    cutoff: datetime,
) -> tuple[list[dict[str, Any]], int]:
    """Poll the runs every 10 s until two scheduled runs have succeeded or the time is up.
    Returns the finished runs scheduled at or after ``cutoff`` and how many older ones were left
    out."""
    deadline = clock() + minutes * 60
    while True:
        page = api.get(f"{path}?limit={RUNS_LIMIT}")["items"]
        kept, left_out = since(page, cutoff)
        runs = [r for r in kept if is_terminal(r)]
        say(f"watch: {len(runs)} finished runs, {len(succeeded(runs))} scheduled succeeded")
        if len(succeeded(runs)) >= 2 or clock() >= deadline:  # noqa: PLR2004
            return runs, left_out
        sleep(POLL_S)


def read_logs(  # noqa: PLR0913, PLR0917  (the read loop's whole state)
    run: Run, slug: str, env: str, since_min: int, sleep: Callable[[float], None]
) -> Logs:
    """Up to three reads, 15 s apart, until two ticks show: read-back can lag the app."""
    since = f"{since_min}m"
    logs = Logs(command=f"ssc logs {slug} --env {env} --source app --since {since}")
    argv = [
        *ssc_prefix(),
        "logs",
        slug,
        "--env",
        env,
        "--source",
        "app",
        "--since",
        since,
        "--json",
    ]
    for attempt in range(LOG_READS):
        if attempt:
            sleep(LOG_WAIT_S)
        done = run(argv, cwd=REPO)
        logs.reads += 1
        if done.returncode != 0:
            logs.error = f"ssc logs failed: {ssc_error(done)}"
            continue
        try:
            texts = [str(line.get("text", "")) for line in json.loads(done.stdout)["lines"]]
        except ValueError, KeyError, TypeError:
            logs.error = "ssc logs answered something unreadable"
            continue
        logs.error = None
        logs.ticks, logs.refused = count_ticks(texts)
        if logs.ticks >= 2:  # noqa: PLR2004
            break
    return logs


def settle(  # noqa: PLR0913, PLR0917  (the poll loop's whole state)
    api: Api,
    path: str,
    ready: Callable[[Mapping[str, Any]], bool],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[Mapping[str, Any] | None, float]:
    """Read the schedules every 5 s, up to 60 s, until ``minute`` is as wanted."""
    started = clock()
    while True:
        found = find(api.get(path)["items"])
        if (found is not None and ready(found)) or clock() - started >= SETTLE_S:
            return found, clock() - started
        sleep(SETTLE_POLL_S)


def kill_cycle(  # noqa: PLR0913, PLR0917  (the phase's whole state)
    api: Api,
    path: str,
    run: Run,
    slug: str,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    out: Findings,
) -> None:
    """Phase 4: disable, read, enable (always), read. Each step's time goes in the findings."""
    t0 = clock()
    off = run([*ssc_prefix(), "disable", slug, "--json"], cwd=REPO)
    off_s = clock() - t0
    out.lines.append(f"ssc disable returned after {off_s:.1f} s, exit {off.returncode}")
    if off.returncode != 0:
        out.lines.append(f"ssc disable said: {ssc_error(off)}")
    paused, wait_s = settle(
        api,
        path,
        lambda s: s.get("state") == "paused" and s.get("pause_reason") == "app_disabled",
        sleep,
        clock,
    )
    out.checks.append(check_paused(paused, off_s + wait_s))
    t1 = clock()
    on = run([*ssc_prefix(), "enable", slug, "--json"], cwd=REPO)
    on_s = clock() - t1
    out.lines.append(f"ssc enable returned after {on_s:.1f} s, exit {on.returncode}")
    if on.returncode != 0:
        out.lines += [f"ssc enable said: {ssc_error(on)}", f"undo: ssc enable {slug}"]
    active, wait_on = settle(
        api, path, lambda s: s.get("state") == "active" and bool(s.get("next_run_at")), sleep, clock
    )
    out.checks.append(check_resumed(active, on_s + wait_on))
    out.data.update(disable_s=off_s, enable_s=on_s, paused_wait_s=wait_s, active_wait_s=wait_on)


def verdict(checks: Sequence[Check]) -> bool | None:
    if any(c.result is False for c in checks):
        return False
    return None if any(c.result is None for c in checks) else True


def number(checks: Sequence[Check], runs: Sequence[Mapping[str, Any]]) -> str:
    pair = latest_two(runs)
    late = [v for r in pair if (v := lateness_s(r)) is not None]
    top = f"{max(late):.1f} s" if late else "n/a"
    done = len(succeeded(runs))
    return f"{done} scheduled runs succeeded, latest lateness {top}, {len(checks)} checks"


def save(outcome: Outcome, app: str) -> None:
    """The per-run file beside the shared history; it never holds the token."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = results_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"timers-{app}-{stamp}.json").write_text(
        json.dumps(
            {
                "verdict": outcome.verdict,
                "number": outcome.number,
                "lines": outcome.lines,
                **outcome.data,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def run(  # noqa: PLR0913, PLR0917  (the kit's proof signature plus the injected clock)
    args: argparse.Namespace,
    run: Run = run_command,
    http: Http = fetch,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Outcome:
    url, token = t8.control_api(run)
    api = Api(http, url, token)
    app_id, env_id = resolve(api, args.app, args.env)
    base = f"/v1/apps/{app_id}/environments/{env_id}/schedules"
    out = Findings()
    out.data.update(app=args.app, env=args.env)
    schedules = api.get(base)["items"]
    out.data["schedules"] = schedules
    out.lines += [schedule_line(s) for s in schedules]
    minute = find(schedules)
    if minute is None or minute.get("state") != "active":
        state = "missing" if minute is None else f"{minute['state']} ({minute.get('pause_reason')})"
        out.lines.append(
            f"stopped: schedule {SCHEDULE} is {state}; deploy and promote to prod first"
        )
        outcome = Outcome("GA-4.1", f"schedule {SCHEDULE} is {state}", False, out.lines, out.data)
        save(outcome, args.app)
        return outcome
    t0 = clock()
    cutoff = min(parse_time(minute["next_run_at"]), wall()) if minute.get("next_run_at") else wall()
    runs, left_out = watch(
        api,
        f"{base}/{minute['schedule_id']}/runs",
        args.minutes,
        sleep,
        clock,
        out.lines.append,
        cutoff,
    )
    out.data.update(runs=runs, left_out=left_out, cutoff=cutoff.isoformat())
    out.lines.append(f"left out {left_out} older runs scheduled before {cutoff.isoformat()}")
    out.lines += run_table(runs)
    out.checks += [check_succeeded(runs), check_spacing(runs), check_overlap(runs)]
    out.checks += [check_lateness(runs), check_start_ms(runs)]
    since = math.ceil((clock() - t0) / 60) + 2
    logs = read_logs(run, args.app, args.env, since, sleep)
    out.checks.append(check_logs(logs))
    out.data["logs"] = {"ticks": logs.ticks, "refused": logs.refused, "error": logs.error}
    if args.disable:
        kill_cycle(api, base, run, args.app, sleep, clock, out)
    out.lines += [c.line() for c in out.checks]
    out.data["checks"] = [c.data() for c in out.checks]
    outcome = Outcome("GA-4.1", number(out.checks, runs), verdict(out.checks), out.lines, out.data)
    save(outcome, args.app)
    return outcome
