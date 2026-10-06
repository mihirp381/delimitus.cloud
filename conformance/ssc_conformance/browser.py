"""The browser suite's results as evidence (SSC-056):
``python -m ssc_conformance.browser --report report.json --cell cell1 [--peer]``.

Reads the Playwright JSON report of ``e2e/isolation`` and adds one result per browser group to
the job's evidence file (``SSC_EVIDENCE_FILE``). A group is named by the suite's top-level
``describe``; a describe that no group names fails the job, so a new one is placed on purpose.
A case skipped as "needs a second test user" is the group ``browser.second_user``, whatever
describe it is in. Cases that need the Docker rig ("fail closed", and "the nightly sign-in",
which tests the sign-in against a stand-in) are no part of a live night.
"""

import argparse
import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ssc_conformance import evidence as ev
from ssc_conformance import matrix
from ssc_conformance.evidence import Evidence, EvidenceError, Result

GROUPS: Final = {
    "the public entry": matrix.BROWSER_ENTRY,
    "a sleeping app wakes": matrix.BROWSER_ENTRY,
    "streams": matrix.BROWSER_ENTRY,
    "cookies": matrix.BROWSER_LOGIN,
    "one app cannot act on another in the same cell": matrix.BROWSER_LOGIN,
    "login sessions": matrix.BROWSER_LOGIN,
    "two cells": matrix.BROWSER_CROSS_CELL,
}
RIG_ONLY: Final = frozenset({"fail closed", "the nightly sign-in"})
_FAILED: Final = ("unexpected", "flaky")


@dataclass(frozen=True, slots=True)
class Case:
    describe: str
    title: str
    project: str
    status: str
    reason: str


def cases(report: Mapping[str, Any]) -> Iterator[Case]:
    """Every test of the report, once per browser project."""

    def walk(suite: Mapping[str, Any], describe: str) -> Iterator[Case]:
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                skips = [a for a in test.get("annotations", []) if a.get("type") == "skip"]
                reason = str(skips[0].get("description", "")) if skips else ""
                yield Case(
                    describe,
                    str(spec["title"]),
                    str(test.get("projectName", "")),
                    str(test["status"]),
                    reason,
                )
        for child in suite.get("suites", []):
            yield from walk(child, describe or str(child["title"]))

    for file_suite in report.get("suites", []):
        yield from walk(file_suite, "")


def results(report: Mapping[str, Any]) -> tuple[Result, ...]:
    """One result per group: any failure fails it; else any skip skips it; else it passes."""
    by_group: dict[str, list[Case]] = {}
    for case in cases(report):
        if case.describe in RIG_ONLY:
            continue
        group = GROUPS.get(case.describe)
        if group is None:
            raise EvidenceError(f"no browser group for the describe {case.describe!r}")
        if case.status == "skipped" and ev.NEEDS_SECOND_USER in case.reason:
            group = matrix.BROWSER_SECOND_USER
        by_group.setdefault(group, []).append(case)
    by_group.setdefault(matrix.BROWSER_SECOND_USER, [])
    return tuple(_fold(group, found) for group, found in sorted(by_group.items()))


def _fold(group: str, found: Sequence[Case]) -> Result:
    failed = [c for c in found if c.status in _FAILED]
    if failed:
        first = failed[0]
        return Result(
            group, ev.FAIL, f"{len(failed)} failed, first: {first.title} ({first.project})"
        )
    skipped = sorted(
        {c.reason or "skipped without a reason" for c in found if c.status == "skipped"}
    )
    if skipped:
        return Result(group, ev.SKIPPED, "; ".join(skipped))
    return Result(group, ev.OK)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--cell", required=True)
    parser.add_argument("--peer", action="store_true")
    args = parser.parse_args(argv)
    path = os.environ.get(ev.EVIDENCE_ENV)
    if not path:
        sys.stderr.write(f"browser: {ev.EVIDENCE_ENV} is not set\n")
        return 1
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        found = results(report)
        ev.write(Path(path), Evidence(args.cell, args.peer, found))
    except (OSError, ValueError, KeyError, EvidenceError) as exc:
        sys.stderr.write(f"browser: {exc}\n")
        return 1
    for r in found:
        sys.stdout.write(f"{r.proof}: {r.status} {r.reason}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
