"""T7: cold starts through the public host, ten samples a series with a 26-minute gap.

``t7 run --state results/t7.state.json --static <slug> --api <slug> --streamlit <slug>`` takes
twenty samples, alternating the two series, 26 minutes apart (about 8.7 hours). Each sample asks
the three apps at once, through their public hosts with a session cookie, and times the first
byte of each answer:

- ``cold``: nothing has called the cell for 26 minutes, so the gateway starts too;
- ``warm``: the cell's ``www`` host is asked first (the gateway answers it itself, so that first
  byte is the gateway's own cold start), then the apps, with the gateway up.

After each sample the API app's ``/vpc`` reports how long its instance waited for Direct VPC
egress to carry a connection. The run can be stopped at any point and started again with the
same command: a sample cut off half way is thrown away, and the next waits a full gap after the
last call, so every kept sample is a cold one. Nothing else may call cell 1 while this runs.

``t7 report --state <file>`` prints the medians from the state file alone.

Pass: all ten samples of each series answered 200, and each median is no worse than the
bake-off's (static 4.5 s, API 8 s, Streamlit 22 s) plus the median gateway start.
"""

import argparse
import json
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final

from proofrun.common import (
    USER_AGENT,
    CookieError,
    CookieJar,
    Fetched,
    Http,
    Outcome,
    Run,
    StateFile,
    app_environment,
    fetch,
    host_of,
    median,
    results_dir,
    run_command,
    seconds,
    session_headers,
    www_host,
)

APPS: Final = ("static", "api", "streamlit")
BAKE_OFF_S: Final = {"static": 4.5, "api": 8.0, "streamlit": 22.0}
PATHS: Final = {"static": "/", "api": "/health", "streamlit": "/_stcore/health"}
SERIES: Final = ("cold", "warm")
SAMPLES: Final = 10
GAP_MINUTES: Final = 26
TIMEOUT_S: Final = 180.0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="step", required=True)
    go = sub.add_parser("run", help="take (or resume) the samples")
    go.add_argument("--state", type=Path, default=results_dir() / "t7.state.json")
    for app in APPS:
        go.add_argument(f"--{app}", required=True, help=f"the {app} probe app's slug")
    go.add_argument("--env", default="preview")
    go.add_argument("--samples", type=int, default=SAMPLES, help="per series")
    go.add_argument("--gap-minutes", type=float, default=GAP_MINUTES)
    go.add_argument(
        "--start-now", action="store_true", help="the cell has been idle a full gap already"
    )
    report = sub.add_parser("report", help="the medians so far")
    report.add_argument("--state", type=Path, default=results_dir() / "t7.state.json")


def series_of(slot: int) -> str:
    return SERIES[slot % 2]


def _headers(jar: CookieJar, url: str) -> dict[str, str]:
    try:
        return session_headers(jar.get(host_of(url)))
    except CookieError:
        return {"User-Agent": USER_AGENT}


def _record(answer: Fetched) -> dict[str, Any]:
    return {"status": answer.status, "s": round(answer.seconds, 3), "error": answer.error}


def take_sample(
    slot: int, targets: Mapping[str, str], www: str, jar: CookieJar, http: Http
) -> dict[str, Any]:
    """One sample: the gateway first in the warm series, then the three apps at once, then the
    API app's Direct VPC egress delay."""
    sample: dict[str, Any] = {"slot": slot, "series": series_of(slot)}
    if sample["series"] == "warm":
        sample["gateway"] = _record(http(www, _headers(jar, www), TIMEOUT_S))
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {
            app: pool.submit(http, url, _headers(jar, url), TIMEOUT_S)
            for app, url in targets.items()
        }
        sample["apps"] = {app: _record(f.result()) for app, f in futures.items()}
    vpc_url = targets["api"].rsplit("/", 1)[0] + "/vpc"
    answer = http(vpc_url, _headers(jar, vpc_url), 30.0)
    try:
        sample["vpc"] = json.loads(answer.body) if answer.status == 200 else None
    except ValueError:
        sample["vpc"] = None
    return sample


def summarise(state: Mapping[str, Any]) -> Outcome:
    """Medians per app and series, the gateway's start, the VPC delay, and the verdict."""
    config = state["config"]
    wanted = int(config["samples"])
    samples: list[dict[str, Any]] = state.get("samples", [])
    gateway = [s["gateway"]["s"] for s in samples if s.get("gateway", {}).get("status") is not None]
    gateway_median = median(gateway)
    lines = [f"gateway cold start: median {seconds(gateway_median)} over {len(gateway)}"]
    medians: dict[str, dict[str, float | None]] = {}
    complete = True
    healthy = True
    within = True
    for app in APPS:
        medians[app] = {}
        for series in SERIES:
            taken = [s["apps"][app] for s in samples if s["series"] == series]
            ok = [a["s"] for a in taken if a["status"] == 200]
            value = median(ok)
            medians[app][series] = value
            limit = BAKE_OFF_S[app] + (gateway_median or 0.0)
            lines.append(
                f"{app} {series}: median {seconds(value)}, {len(ok)}/{len(taken)} answered 200, "
                f"limit {limit:.2f} s"
            )
            complete = complete and len(taken) >= wanted
            healthy = healthy and len(ok) == len(taken)
            within = within and value is not None and value <= limit
    delays = [
        float(s["vpc"]["delay_s"])
        for s in samples
        if isinstance(s.get("vpc"), dict) and s["vpc"].get("delay_s") is not None
    ]
    lines.append(f"Direct VPC egress delay: median {seconds(median(delays))} over {len(delays)}")
    number = ", ".join(
        f"{app} {seconds(medians[app]['cold'])}/{seconds(medians[app]['warm'])}" for app in APPS
    )
    passed: bool | None = (healthy and within) if complete else None
    if not complete:
        lines.append(f"{len(samples)}/{2 * wanted} samples taken: run `t7 run` again to resume")
    return Outcome(
        "T7",
        f"cold/warm medians {number}, gateway {seconds(gateway_median)}",
        passed,
        lines,
        {"medians": medians, "gateway_s": gateway_median, "vpc_delay_s": median(delays)},
    )


def sample_loop(  # noqa: PLR0913  (keyword-only)
    store: StateFile,
    state: dict[str, Any],
    *,
    take: Callable[[int], dict[str, Any]],
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = print,
    start_now: bool = False,
) -> dict[str, Any]:
    """Take the remaining samples, saving after each, honouring the gap across restarts. A new
    run waits a full gap first unless ``start_now`` says the cell is idle already."""
    gap = float(state["config"]["gap_minutes"]) * 60
    total = 2 * int(state["config"]["samples"])
    samples: list[dict[str, Any]] = state.setdefault("samples", [])
    if not samples and "last_traffic_at" not in state and not start_now:
        state["last_traffic_at"] = clock()
        store.save(state)
    started = state.pop("in_progress", None)
    if started is not None:
        say(f"slot {started['slot']} was cut off: thrown away, waiting a full gap")
        state["last_traffic_at"] = max(state.get("last_traffic_at", 0.0), started["at"])
        store.save(state)
    while len(samples) < total:
        slot = len(samples)
        last = state.get("last_traffic_at")
        if last is not None:
            wait = last + gap - clock()
            if wait > 0:
                say(f"slot {slot + 1}/{total} ({series_of(slot)}) in {wait / 60:.1f} min")
                sleep(wait)
        state["in_progress"] = {"slot": slot, "at": clock()}
        store.save(state)
        sample = take(slot)
        sample["at"] = clock()
        samples.append(sample)
        state["last_traffic_at"] = sample["at"]
        state.pop("in_progress", None)
        store.save(state)
        apps = ", ".join(f"{a} {r['status']} {r['s']} s" for a, r in sample["apps"].items())
        say(f"slot {slot + 1}/{total} {sample['series']}: {apps}")
    return state


def run(args: argparse.Namespace, run: Run = run_command, http: Http = fetch) -> Outcome:
    store = StateFile(args.state)
    if args.step == "report":
        state = store.load()
        if state is None:
            return Outcome("T7", f"no state at {args.state}", None, [])
        return summarise(state)
    config = {
        "apps": {app: getattr(args, app) for app in APPS},
        "env": args.env,
        "samples": args.samples,
        "gap_minutes": args.gap_minutes,
    }
    state = store.resume(config)
    targets = {
        app: app_environment(run, slug, args.env)["url"].rstrip("/") + PATHS[app]
        for app, slug in config["apps"].items()
    }
    www = f"https://{www_host(host_of(targets['static']))}/"
    jar = CookieJar()
    state = sample_loop(
        store,
        state,
        take=lambda slot: take_sample(slot, targets, www, jar, http),
        start_now=args.start_now,
    )
    return summarise(state)
