"""The Playwright report turned into evidence for the page."""

import json
from pathlib import Path
from typing import Any

import pytest

from ssc_conformance import browser, matrix
from ssc_conformance import evidence as ev
from ssc_conformance.evidence import EvidenceError, Result


def case(
    title: str, status: str = "expected", reason: str = "", project: str = "chromium"
) -> dict[str, Any]:
    annotations = [{"type": "skip", "description": reason}] if reason else []
    return {
        "title": title,
        "tests": [{"projectName": project, "status": status, "annotations": annotations}],
    }


def report(**describes: list[dict[str, Any]]) -> dict[str, Any]:
    suites = [{"title": name, "specs": specs} for name, specs in describes.items()]
    return {"suites": [{"title": "x.spec.ts", "file": "x.spec.ts", "suites": suites}]}


def by_proof(rs: tuple[Result, ...]) -> dict[str, Result]:
    return {r.proof: r for r in rs}


def test_groups_pass_when_every_case_passed() -> None:
    found = by_proof(
        browser.results(
            report(
                **{
                    "the public entry": [case("a")],
                    "streams": [case("b")],
                    "cookies": [case("c")],
                    "login sessions": [case("d")],
                    "two cells": [case("e")],
                }
            )
        )
    )
    assert found[matrix.BROWSER_ENTRY].status == ev.OK
    assert found[matrix.BROWSER_LOGIN].status == ev.OK
    assert found[matrix.BROWSER_CROSS_CELL].status == ev.OK
    assert found[matrix.BROWSER_SECOND_USER].status == ev.OK


def test_one_failure_fails_the_group_and_names_the_case_and_browser() -> None:
    found = by_proof(
        browser.results(report(cookies=[case("c1"), case("c2", "unexpected", project="webkit")]))
    )
    assert found[matrix.BROWSER_LOGIN].status == ev.FAIL
    assert "c2 (webkit)" in found[matrix.BROWSER_LOGIN].reason


def test_a_skip_keeps_its_reason() -> None:
    found = by_proof(
        browser.results(report(**{"two cells": [case("e", "skipped", "no peer cell"), case("f")]}))
    )
    assert found[matrix.BROWSER_CROSS_CELL] == Result(
        matrix.BROWSER_CROSS_CELL, "skipped", "no peer cell"
    )


def test_a_second_user_case_moves_to_its_own_group() -> None:
    second = case("o", "skipped", "needs a second test user")
    found = by_proof(browser.results(report(**{"login sessions": [case("d"), second]})))
    assert found[matrix.BROWSER_LOGIN].status == ev.OK
    assert found[matrix.BROWSER_SECOND_USER] == Result(
        matrix.BROWSER_SECOND_USER, "skipped", "needs a second test user"
    )


def test_rig_only_cases_are_not_part_of_a_night() -> None:
    found = by_proof(
        browser.results(report(**{"fail closed": [case("z", "skipped", "needs the rig")]}))
    )
    assert matrix.BROWSER_LOGIN not in found


def test_a_describe_no_group_names_fails_the_job() -> None:
    with pytest.raises(EvidenceError, match="no browser group"):
        browser.results(report(**{"something new": [case("n")]}))


def test_main_writes_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report(cookies=[case("c")])))
    evidence = tmp_path / "e.json"
    monkeypatch.setenv(ev.EVIDENCE_ENV, str(evidence))
    assert browser.main(["--report", str(path), "--cell", "cell1", "--peer"]) == 0
    written = ev.read_file(evidence)
    assert written.cell == "cell1"
    assert written.peer
    monkeypatch.delenv(ev.EVIDENCE_ENV)
    assert browser.main(["--report", str(path), "--cell", "cell1"]) == 1
