"""The required-checks gate promote passes before it builds for prod.

A required check is a check run name and the workflow file it must come from. It is green on a
commit only when, among that commit's check runs with the name whose check suite belongs to a
workflow run of that file on the connected branch, the latest (highest id) has completed with
``success``. A run of the same name from another workflow file, another branch or another app
never counts, so renaming a job in a different workflow cannot satisfy the gate. A workflow
run's ``path`` is compared without any ``@<ref>`` suffix GitHub adds. GitHub errors propagate:
the caller refuses, the gate never opens on an error.
"""

from typing import Any, cast

from ssc_control.github.client import GitHubApp
from ssc_control.github.links import Link, RequiredCheck


def _suites(workflow_runs: list[dict[str, Any]]) -> dict[int, tuple[str, str]]:
    out: dict[int, tuple[str, str]] = {}
    for w in workflow_runs:
        suite, path, branch = w.get("check_suite_id"), w.get("path"), w.get("head_branch")
        if isinstance(suite, int) and isinstance(path, str) and isinstance(branch, str):
            out[suite] = (path.split("@", 1)[0], branch)
    return out


def _suite_of(run: dict[str, Any]) -> int | None:
    suite = run.get("check_suite")
    if not isinstance(suite, dict):
        return None
    value = cast("dict[str, Any]", suite).get("id")
    return value if isinstance(value, int) else None


def failing(
    required: tuple[RequiredCheck, ...],
    branch: str,
    check_runs: list[dict[str, Any]],
    workflow_runs: list[dict[str, Any]],
) -> list[RequiredCheck]:
    """The required checks that are not green, in the order they were required."""
    suites = _suites(workflow_runs)
    out: list[RequiredCheck] = []
    for check in required:
        bound = [
            r
            for r in check_runs
            if r.get("name") == check.name
            and isinstance(r.get("id"), int)
            and suites.get(_suite_of(r) or 0) == (check.workflow, branch)
        ]
        latest = max(bound, key=lambda r: int(r["id"]), default=None)
        if (
            latest is None
            or latest.get("status") != "completed"
            or latest.get("conclusion") != "success"
        ):
            out.append(check)
    return out


async def failing_checks(github: GitHubApp, link: Link, sha: str) -> list[RequiredCheck]:
    """:func:`failing` for ``sha`` of the connected repository; raises ``GitHubError``."""
    if not link.required_checks:
        return []
    check_runs = await github.check_runs(link.repo, sha)
    workflow_runs = await github.workflow_runs(link.repo, sha)
    return failing(link.required_checks, link.branch, check_runs, workflow_runs)
