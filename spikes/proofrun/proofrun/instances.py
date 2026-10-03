"""Instance counts of one Cloud Run service from Cloud Monitoring, a minute at a time.

``instances --project <p> --service <s> --minutes 25 [--end <RFC 3339>]``

Answers the live check left open by SSC-028 (``infra/README.md``, App usage, check 3; decision
025's usage source is reversed if it fails): while a WebSocket is open with no clicks, is every
minute counted as ``active``? Hold the page for 20 minutes with ``t9 hold ... --hours 0.34``,
wait 5 minutes for Monitoring, then run this over the window. Pass: every minute of the window
has at least one ``active`` instance. After T8 it confirms the app's instances reached 0.

``--metric startup_latencies`` (mean start per minute, ms) or ``billable_instance_time`` (seconds
per minute) prints those instead, with no verdict: T7 compares the first with the wait a user
sees (decision 025's other reversal condition), T9 cross-checks the bill with the second.
"""

import argparse
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

from proofrun.cloudrun import (
    BILLABLE_TIME,
    INSTANCE_COUNT,
    STARTUP_LATENCIES,
    Get,
    _get,
    read_series,
)
from proofrun.common import Outcome, Run, parse_time, run_command

ACTIVE: Final = "active"
METRICS: Final = {
    "instance_count": (INSTANCE_COUNT, "ALIGN_MAX"),
    "startup_latencies": (STARTUP_LATENCIES, "ALIGN_DELTA"),
    "billable_instance_time": (BILLABLE_TIME, "ALIGN_SUM"),
}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)
    parser.add_argument("--service", required=True, help="the Cloud Run service (ssc-a-...)")
    parser.add_argument("--minutes", type=int, default=25, help="the window, ending at --end")
    parser.add_argument("--end", help="the window's end, RFC 3339 (default: now)")
    parser.add_argument("--metric", choices=sorted(METRICS), default="instance_count")
    parser.add_argument(
        "--skip-last", type=int, default=0, help="leave out this many minutes at the window's end"
    )


def read_window(by_state: Mapping[str, Mapping[str, float]], skip_last: int = 0) -> Outcome:
    """Minutes with an active instance against all minutes Monitoring reported."""
    minutes = sorted({m for points in by_state.values() for m in points})
    if skip_last:
        minutes = minutes[:-skip_last]
    if not minutes:
        return Outcome("instance_count", "no data", None, ["Monitoring returned no points"])
    active = by_state.get(ACTIVE, {})
    idle = by_state.get("idle", {})
    lines = [f"{m}  active {active.get(m, 0):g}  idle {idle.get(m, 0):g}" for m in minutes]
    counted = sum(1 for m in minutes if active.get(m, 0) >= 1)
    return Outcome(
        "instance_count",
        f"{counted}/{len(minutes)} minutes active",
        counted == len(minutes),
        lines,
        {"active_minutes": counted, "minutes": len(minutes)},
    )


def read_values(name: str, by_state: Mapping[str, Mapping[str, float]]) -> Outcome:
    """Any other metric, a line a minute, with no verdict beyond whether there was data."""
    points = sorted((m, v) for values in by_state.values() for m, v in values.items())
    lines = [f"{m}  {v:g}" for m, v in points]
    total = sum(v for _, v in points)
    return Outcome(
        name,
        f"{len(points)} minute(s), total {total:g}",
        True if points else None,
        lines,
        {"points": len(points), "total": total},
    )


def run(args: argparse.Namespace, run: Run = run_command, get: Get = _get) -> Outcome:
    end = parse_time(args.end) if args.end else datetime.now(UTC)
    metric, aligner = METRICS[args.metric]
    by_state = read_series(
        run,
        project=args.project,
        service=args.service,
        metric=metric,
        end=end,
        minutes=args.minutes,
        aligner=aligner,
        get=get,
    )
    if args.metric != "instance_count":
        return read_values(args.metric, by_state)
    return read_window(by_state, args.skip_last)
