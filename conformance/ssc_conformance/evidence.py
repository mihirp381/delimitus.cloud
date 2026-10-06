"""What each nightly job reports to the one-page result (SSC-056).

A job writes one JSON file: the cell it ran against, whether that run had a peer cell, and one
``Result`` per proof it made. ``ssc_conformance.matrix`` merges every file into the page. A proof
is named by a probe, a check or a browser group (``matrix.ROWS``); a job never says which
isolation row it proves.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

EVIDENCE_ENV: Final = "SSC_EVIDENCE_FILE"
OK: Final = "pass"
FAIL: Final = "fail"
SKIPPED: Final = "skipped"
NO_PEER: Final = "no peer cell"
WAITS_FOR_DATAGW: Final = "waits for the data gateway"
NEEDS_SECOND_USER: Final = "needs a second test user"
NO_READ_ACCESS: Final = "no read access"

type Status = Literal["pass", "fail", "skipped"]
type Json = dict[str, Any]
_STATUSES: Final = (OK, FAIL, SKIPPED)
_PROBE_STATUS: Final = {"passed": OK, "failed": FAIL, "skipped": SKIPPED}


class EvidenceError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Result:
    proof: str
    status: Status
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Evidence:
    """One job's results for one cell. ``peer`` is whether the run was given a second cell."""

    cell: str
    peer: bool
    results: tuple[Result, ...]

    def to_json(self) -> str:
        body = {
            "cell": self.cell,
            "peer": self.peer,
            "results": [
                {"proof": r.proof, "status": r.status, "reason": r.reason} for r in self.results
            ],
        }
        return json.dumps(body, indent=2, sort_keys=True) + "\n"


def probe_results(probes: Sequence[Mapping[str, object]]) -> tuple[Result, ...]:
    """The probe runner's ``ssc_probe`` lines as results, one per probe, named as the probe."""
    return tuple(
        Result(
            str(p.get("probe")),
            cast(Status, _PROBE_STATUS.get(str(p.get("status")), FAIL)),
            str(p.get("reason") or ""),
        )
        for p in probes
    )


def write(path: Path, evidence: Evidence) -> None:
    """Writes ``evidence`` to ``path``, or adds its results when the file already holds this
    cell's: a job's steps each add their own."""
    results = evidence.results
    if path.exists():
        before = read_file(path)
        if before.cell != evidence.cell:
            raise EvidenceError(f"{path} holds {before.cell}, not {evidence.cell}")
        kept = tuple(r for r in before.results if r.proof not in {x.proof for x in results})
        evidence = Evidence(evidence.cell, before.peer or evidence.peer, kept + results)
    path.write_text(evidence.to_json(), encoding="utf-8")


def read_file(path: Path) -> Evidence:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        cell, peer, raw = body["cell"], body["peer"], body["results"]
        results = tuple(Result(str(r["proof"]), r["status"], str(r.get("reason", ""))) for r in raw)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EvidenceError(f"{path}: not an evidence file ({type(exc).__name__})") from None
    if not isinstance(cell, str) or not isinstance(peer, bool) or not cell:
        raise EvidenceError(f"{path}: not an evidence file")
    if any(r.status not in _STATUSES for r in results):
        raise EvidenceError(f"{path}: unknown status")
    return Evidence(cell, peer, results)


def read_all(directory: Path) -> list[Evidence]:
    """Every ``*.json`` file under ``directory``; downloaded artifacts sit one folder deep."""
    return [read_file(p) for p in sorted(directory.rglob("*.json"))]
