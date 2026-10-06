"""The one-page result: its rows, its folding and its exit rule."""

import os
from pathlib import Path

import pytest

from ssc_conformance import evidence as ev
from ssc_conformance import matrix
from ssc_conformance.evidence import Evidence, Result
from ssc_conformance.matrix import ALSO, ROWS

CELLS = ["cell1", "cell2"]


def passing(cell: str, *, peer: bool = True) -> Evidence:
    proofs = [p for row in ROWS for p in row.proofs] + list(ALSO)
    results = [Result(p, ev.OK) for p in dict.fromkeys(proofs)]
    return Evidence(cell, peer, tuple(results))


def changed(base: Evidence, *results: Result) -> Evidence:
    names = {r.proof for r in results}
    kept = tuple(r for r in base.results if r.proof not in names)
    return Evidence(base.cell, base.peer, kept + results)


def row(page: matrix.Page, title: str) -> str:
    return page.row_status(next(r for r in ROWS if r.title == title))


def test_the_page_has_the_seventeen_rows_with_their_own_titles() -> None:
    titles = [r.title for r in ROWS]
    assert len(titles) == 17
    assert len(set(titles)) == 17


def test_the_titles_are_those_of_the_architecture_document() -> None:
    doc = os.environ.get("SSC_ARCH_DOC")
    if not doc:
        pytest.skip("architecture doc not available")
    assert [r.title for r in ROWS] == matrix.section3_titles(Path(doc).read_text(encoding="utf-8"))


def test_section3_titles_reads_the_table() -> None:
    text = (
        "## 2. Before\n\n| a | b |\n|---|---|\n| nope | x |\n\n"
        "## 3. The isolation matrix\n\nwords\n\n"
        "| What could leak | Boundary | Kind | Proof |\n|---|---|---|---|\n"
        "| First | x | y | z |\n| Second (and more) | x | y | z |\n\n"
        "## 4. After\n\n| later | x |\n"
    )
    assert matrix.section3_titles(text) == ["First", "Second (and more)"]


def test_a_proof_belongs_to_no_unknown_name_and_is_not_listed_twice_in_a_row() -> None:
    for r in ROWS:
        assert len(set(r.proofs)) == len(r.proofs), r.title
        assert not (r.proofs and r.manual), r.title
        assert r.proofs or r.manual, r.title


def test_two_cells_all_passing_is_green() -> None:
    page = matrix.judge([passing("cell1"), passing("cell2")], CELLS)
    assert page.failures() == []
    assert page.peer
    assert row(page, "Secrets and credentials") == "pass"
    assert row(page, "Files") == "not automated"


def test_with_one_cell_every_line_across_cells_is_skipped_even_when_the_job_says_pass() -> None:
    page = matrix.judge([passing("cell1", peer=False)], ["cell1"])
    assert page.failures() == []
    for proof in matrix.CROSS_CELL:
        assert page.proofs[proof].by_cell["cell1"] == Result(proof, ev.SKIPPED, ev.NO_PEER)
    assert row(page, "App to app, across customers") == "skipped"
    assert row(page, "Network") == "skipped"
    assert row(page, "Runtime") == "pass"
    text = matrix.markdown(page)
    assert "skipped (no peer cell)" in text
    assert "Night is GREEN" in text


def test_with_one_cell_a_failure_across_cells_still_fails() -> None:
    bad = changed(passing("cell1", peer=False), Result("deny_peer_cell", ev.FAIL, "got 200"))
    page = matrix.judge([bad], ["cell1"])
    assert page.failures() == ["deny_peer_cell (cell1): got 200"]


def test_needing_a_peer_turns_the_skip_into_a_failure() -> None:
    page = matrix.judge([passing("cell1", peer=False)], ["cell1"], need_peer=True)
    assert any("a skip that is not allowed" in f for f in page.failures())
    assert "Night is RED" in matrix.markdown(page, need_peer=True)


def test_a_proof_nobody_reported_fails() -> None:
    base = passing("cell1")
    partial = Evidence("cell1", True, tuple(r for r in base.results if r.proof != "non_root_10001"))
    page = matrix.judge([partial, passing("cell2")], CELLS)
    assert page.failures() == ["non_root_10001 (cell1): did not report"]
    assert row(page, "Runtime") == "fail"


def test_a_proof_reported_twice_fails() -> None:
    twice = [passing("cell1"), Evidence("cell1", True, (Result("non_root_10001", ev.OK),))]
    page = matrix.judge([*twice, passing("cell2")], CELLS)
    assert page.failures() == ["non_root_10001 (cell1): reported 2 times"]


def test_a_failure_in_one_cell_names_the_cell_and_fails_the_row() -> None:
    bad = changed(passing("cell2"), Result("no_dns_exfil", ev.FAIL, "resolved"))
    page = matrix.judge([passing("cell1"), bad], CELLS)
    assert row(page, "Network") == "fail"
    assert page.failures() == ["no_dns_exfil (cell2): resolved"]
    assert "cell2 fail (resolved)" in matrix.markdown(page)


@pytest.mark.parametrize(
    "result",
    [
        Result(matrix.STAFF, ev.SKIPPED, "no read access"),
        Result("non_root_10001", ev.SKIPPED, "no reason"),
        Result(matrix.DATAGW_READ_ONLY, ev.SKIPPED, "no read access"),
    ],
)
def test_a_skip_that_is_not_allowed_fails(result: Result) -> None:
    page = matrix.judge([changed(passing("cell1"), result), passing("cell2")], CELLS)
    assert [f for f in page.failures() if "not allowed" in f]


def test_the_allowed_skips_pass_the_night() -> None:
    second = Result(matrix.BROWSER_SECOND_USER, ev.SKIPPED, ev.NEEDS_SECOND_USER)
    datagw = Result(matrix.DATAGW_READ_ONLY, ev.SKIPPED, ev.WAITS_FOR_DATAGW)
    cells = [changed(passing(c), second, datagw) for c in CELLS]
    page = matrix.judge(cells, CELLS)
    assert page.failures() == []
    assert row(page, "Login sessions") == "skipped"


def test_waiting_for_the_data_gateway_is_allowed_only_in_the_also_checked_block() -> None:
    other = Result("non_root_10001", ev.SKIPPED, ev.WAITS_FOR_DATAGW)
    page = matrix.judge([changed(passing("cell1"), other), passing("cell2")], CELLS)
    assert page.failures()


def test_the_exit_code_follows_the_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ev.write(tmp_path / "a.json", passing("cell1"))
    ev.write(tmp_path / "b.json", passing("cell2"))
    out = tmp_path / "page.md"
    summary = tmp_path / "summary.md"
    os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
    try:
        assert (
            matrix.main(["--evidence", str(tmp_path), "--cells", "cell1,cell2", "--out", str(out)])
            == 0
        )
        assert "Night is GREEN" in capsys.readouterr().out
        assert out.read_text() == summary.read_text()
        ev.write(tmp_path / "b.json", Evidence("cell2", True, (Result("non_root_10001", ev.FAIL),)))
        assert matrix.main(["--evidence", str(tmp_path), "--cells", "cell1,cell2"]) == 1
        assert matrix.main(["--evidence", str(tmp_path), "--cells", ""]) == 1
    finally:
        del os.environ["GITHUB_STEP_SUMMARY"]


def test_an_unreadable_evidence_file_fails_the_page(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("nope")
    assert matrix.main(["--evidence", str(tmp_path), "--cells", "cell1"]) == 1


def test_the_page_lists_every_row_and_the_also_checked_block() -> None:
    text = matrix.markdown(matrix.judge([passing("cell1"), passing("cell2")], CELLS))
    for number, r in enumerate(ROWS, 1):
        assert f"| {number} | {r.title.replace('|', '/')} |" in text
    for title in matrix.ALSO_TITLES.values():
        assert title in text
    assert "not automated: cell diff" in text
