"""T1: two staging cells from the amended stack, one full and one empty.

- ``t1 diff <label 1> <label 2>`` runs ``ssc_infra.cell_diff`` (which leaves out what differing
  flags name) and reads its count. Pass: at least one resource compared and 0 differences, and no
  policy override.
- ``t1 cost --usd <cost> --days <n>`` turns the empty cell's billed cost before credits, for whole
  days, into a daily figure. Pass: under $1 a day.
"""

import argparse
import re
from typing import Final

from proofrun.common import REPO, Outcome, Run, last_line, run_command

DAILY_LIMIT_USD: Final = 1.0
EMPTY_CELL_MONTH_USD: Final = 23.0
DAYS_PER_MONTH: Final = 30.4
_COUNT: Final = re.compile(r"(\d+) resources compared, (\d+) difference\(s\)")
_LEFT_OUT: Final = re.compile(r"flags differ, their resources left out: (.+)")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="step", required=True)
    diff = sub.add_parser("diff", help="cell_diff between the full and the empty cell")
    diff.add_argument("full", help="cell 1's label (every flag on)")
    diff.add_argument("empty", help="cell 2's label (every flag off)")
    cost = sub.add_parser("cost", help="the empty cell's cost per day")
    cost.add_argument("--usd", type=float, required=True, help="cost before credits, whole days")
    cost.add_argument("--days", type=float, required=True)


def read_diff(returncode: int, stdout: str, stderr: str) -> Outcome:
    """The verdict from ``cell_diff``'s output and exit code."""
    count = _COUNT.search(stderr)
    lines = [line for line in stdout.splitlines() if line.strip()]
    if count is None:
        return Outcome("T1", "cell_diff did not finish", None, [last_line(stderr)])
    compared, diffs = int(count.group(1)), int(count.group(2))
    left_out = _LEFT_OUT.search(stderr)
    overrides = [line.strip() for line in lines if line.strip().startswith("override:")]
    found = [
        f"left out for differing flags: {left_out.group(1) if left_out else 'nothing'}",
        *lines,
    ]
    passed = returncode == 0 and compared > 0 and diffs == 0 and not overrides
    return Outcome(
        "T1",
        f"{diffs} difference(s) over {compared} resources, {len(overrides)} policy override(s)",
        passed,
        found,
        {"compared": compared, "differences": diffs, "overrides": overrides},
    )


def daily_cost(usd: float, days: float) -> Outcome:
    """The empty cell's cost per day against $1, and the month it implies against about $23."""
    if days <= 0:
        raise ValueError("--days must be more than 0")
    per_day = usd / days
    month = per_day * DAYS_PER_MONTH
    return Outcome(
        "T1",
        f"empty cell ${per_day:.2f} a day",
        per_day < DAILY_LIMIT_USD,
        [
            f"${usd:.2f} over {days:g} day(s), before credits",
            f"implies ${month:.2f} a month against the model's ${EMPTY_CELL_MONTH_USD:.0f}",
        ],
        {"per_day_usd": round(per_day, 4), "month_usd": round(month, 2)},
    )


def run(args: argparse.Namespace, run: Run = run_command) -> Outcome:
    if args.step == "cost":
        return daily_cost(args.usd, args.days)
    done = run(
        ["uv", "run", "python", "-m", "ssc_infra.cell_diff", args.full, args.empty],
        cwd=REPO / "infra",
    )
    return read_diff(done.returncode, done.stdout, done.stderr)
