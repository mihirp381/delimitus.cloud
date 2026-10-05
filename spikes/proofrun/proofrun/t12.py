"""T12: delete cell 2 and watch the billing slot come back.

``t12 --billing-account <id> --project ssc-c-<label 2> [--expect 4]`` (the account may come from
``$PROOFRUN_BILLING_ACCOUNT``) reads, every ``--interval-minutes`` (10) for up to ``--hours``
(12), two things, both read-only:

- ``gcloud billing projects list --billing-account <id>``: how many projects are linked with
  billing enabled;
- ``gcloud projects describe <project>``: cell 2's lifecycle state (``DELETE_REQUESTED`` once
  ``pulumi destroy`` has run).

``--once`` takes a single reading: take one before the destroy, so the count it starts from is
on record. Stopped, it resumes from ``--state``.

Pass: once cell 2 is ``DELETE_REQUESTED``, the linked count falls to ``--expect`` or below on the
same UTC day. Record the times in RESULTS.md and in SSC-089. If the count has not fallen by the
end of that day, decision 026's rule stands (unlink billing before deleting); the fallback is
README T12's undelete, unlink, delete.
"""

import argparse
import os
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from proofrun.common import (
    CommandError,
    Outcome,
    Run,
    StateFile,
    gcloud_json,
    results_dir,
    run_command,
)

ACCOUNT_ENV: Final = "PROOFRUN_BILLING_ACCOUNT"
EXPECT: Final = 4
DELETING: Final = "DELETE_REQUESTED"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--billing-account", default=os.environ.get(ACCOUNT_ENV), help=f"default: ${ACCOUNT_ENV}"
    )
    parser.add_argument("--project", required=True, help="cell 2's project id")
    parser.add_argument("--expect", type=int, default=EXPECT, help="linked count once it is back")
    parser.add_argument("--interval-minutes", type=float, default=10.0)
    parser.add_argument("--hours", type=float, default=12.0)
    parser.add_argument("--state", type=Path, default=results_dir() / "t12.state.json")
    parser.add_argument("--once", action="store_true", help="one reading, then the verdict")


def masked(account: str) -> str:
    """The billing account with all but its last four characters hidden, for printing."""
    return "…" + account[-4:]


def reading(run: Run, account: str, project: str, at: float) -> dict[str, Any]:
    linked = gcloud_json(run, "billing", "projects", "list", f"--billing-account={account}")
    try:
        doc = gcloud_json(run, "projects", "describe", project)
        lifecycle = (doc or {}).get("lifecycleState")
    except CommandError as exc:
        lifecycle = f"unreadable: {exc}"[:120]
    return {
        "at": at,
        "linked": sum(1 for p in linked or [] if p.get("billingEnabled")),
        "lifecycle": lifecycle,
    }


def _day(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).date().isoformat()


def _clock(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%d %H:%M UTC")


def verdict(readings: Sequence[Mapping[str, Any]], expect: int) -> Outcome:
    lines = [f"{_clock(r['at'])}: {r['linked']} linked, cell 2 {r['lifecycle']}" for r in readings]
    deleted = next((r for r in readings if r["lifecycle"] == DELETING), None)
    if deleted is None:
        last = readings[-1]["linked"] if readings else "n/a"
        return Outcome("T12", f"{last} linked, cell 2 not deleted yet", None, lines)
    after = [r for r in readings if r["at"] >= deleted["at"]]
    back = next((r for r in after if r["linked"] <= expect), None)
    same_day = [r for r in after if _day(r["at"]) == _day(deleted["at"])]
    day_over = len(same_day) < len(after)
    if back is not None:
        minutes = (back["at"] - deleted["at"]) / 60
        ok = _day(back["at"]) == _day(deleted["at"])
        lines.append(f"slot back {minutes:.0f} min after cell 2 was first seen deleting")
        return Outcome(
            "T12",
            f"linked count {back['linked']} at {_clock(back['at'])}, "
            f"{minutes:.0f} min after delete",
            ok,
            lines,
            {"minutes": minutes, "linked": back["linked"]},
        )
    lines.append(f"still {after[-1]['linked']} linked, expected {expect} or fewer")
    return Outcome(
        "T12",
        f"linked count still {after[-1]['linked']}",
        False if day_over else None,
        lines,
        {"linked": after[-1]["linked"]},
    )


def watch(  # noqa: PLR0913  (keyword-only)
    store: StateFile,
    state: dict[str, Any],
    *,
    read: Callable[[float], dict[str, Any]],
    once: bool,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Take readings until the slot is back, the hours are up, or one if ``once``."""
    config = state["config"]
    readings: list[dict[str, Any]] = state.setdefault("readings", [])
    end = clock() + float(config["hours"]) * 3600
    while True:
        r = read(clock())
        readings.append(r)
        store.save(state)
        say(f"{_clock(r['at'])}: {r['linked']} linked, cell 2 {r['lifecycle']}")
        result = verdict(readings, int(config["expect"]))
        if once or result.passed is not None or clock() >= end:
            return readings
        sleep(float(config["interval_minutes"]) * 60)


def run(args: argparse.Namespace, run: Run = run_command) -> Outcome:
    if not args.billing_account:
        raise SystemExit(f"--billing-account or ${ACCOUNT_ENV} is required")
    config = {
        "project": args.project,
        "expect": args.expect,
        "interval_minutes": args.interval_minutes,
        "hours": args.hours,
    }
    store = StateFile(args.state)
    state = store.resume(config)
    readings = watch(
        store,
        state,
        read=lambda at: reading(run, args.billing_account, args.project, at),
        once=args.once,
    )
    outcome = verdict(readings, args.expect)
    outcome.lines.insert(0, f"billing account {masked(args.billing_account)}")
    return outcome
