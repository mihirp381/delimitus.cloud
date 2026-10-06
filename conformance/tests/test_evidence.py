"""Evidence files: what a job writes and the page reads."""

from pathlib import Path

import pytest

from ssc_conformance import evidence as ev
from ssc_conformance.evidence import Evidence, EvidenceError, Result


def test_a_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "cell1.json"
    written = Evidence("cell1", True, (Result("a", ev.OK), Result("b", ev.SKIPPED, "no peer cell")))
    ev.write(path, written)
    assert ev.read_file(path) == written


def test_a_second_write_adds_to_the_same_cell_and_replaces_a_proof(tmp_path: Path) -> None:
    path = tmp_path / "cell1.json"
    ev.write(path, Evidence("cell1", False, (Result("a", ev.FAIL, "late"), Result("b", ev.OK))))
    ev.write(path, Evidence("cell1", True, (Result("a", ev.OK), Result("c", ev.OK))))
    after = ev.read_file(path)
    assert sorted((r.proof, r.status) for r in after.results) == [
        ("a", "pass"),
        ("b", "pass"),
        ("c", "pass"),
    ]
    assert after.peer


def test_a_file_of_another_cell_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    ev.write(path, Evidence("cell1", False, ()))
    with pytest.raises(EvidenceError, match="cell1, not cell2"):
        ev.write(path, Evidence("cell2", False, ()))


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "{}",
        '{"cell": "", "peer": false, "results": []}',
        '{"cell": "c", "peer": "yes", "results": []}',
        '{"cell": "c", "peer": false, "results": [{"proof": "a", "status": "ok"}]}',
    ],
)
def test_a_file_that_is_not_evidence_is_refused(tmp_path: Path, body: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(body)
    with pytest.raises(EvidenceError):
        ev.read_file(path)


def test_read_all_finds_files_one_folder_deep(tmp_path: Path) -> None:
    (tmp_path / "probes-cell1").mkdir()
    ev.write(tmp_path / "probes-cell1" / "e.json", Evidence("cell1", False, ()))
    ev.write(tmp_path / "top.json", Evidence("cell2", False, ()))
    assert sorted(e.cell for e in ev.read_all(tmp_path)) == ["cell1", "cell2"]


def test_probe_lines_become_results() -> None:
    probes = [
        {"probe": "a", "status": "passed"},
        {"probe": "b", "status": "failed", "reason": "got 200"},
        {"probe": "c", "status": "skipped", "reason": "no peer cell"},
        {"probe": "d", "status": "surprise"},
    ]
    assert ev.probe_results(probes) == (
        Result("a", "pass"),
        Result("b", "fail", "got 200"),
        Result("c", "skipped", "no peer cell"),
        Result("d", "fail"),
    )
