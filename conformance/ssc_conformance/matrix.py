"""The one-page nightly result, laid out as the isolation matrix (SSC-056):
``python -m ssc_conformance.matrix --evidence DIR --cells cell1,cell2``.

One line per row of architecture section 3 (all 17): the proofs behind it and tonight's result,
which is pass, fail, skipped with its reason, or not automated. Every job writes an evidence file
(``ssc_conformance.evidence``); this merges them. Below the rows, "also checked tonight" lists
what is nightly but is no row (drift repair, the certificate, the kill drill, the read-only
matrix against the data gateway).

Rules, which keep a night honest:

- A proof that crosses cells is skipped with "no peer cell" when its run had no peer, and never
  passes, whatever the job said. ``--need-peer`` (the two-cell window) makes that skip a failure.
- A row is as good as its worst proof: fail, then skipped, then pass.
- A proof a cell's jobs never reported is a failure. So is any skip that is not allowed: "no
  peer cell" while no peer is needed, "needs a second test user", and "waits for the data
  gateway" in the also-checked block. "No read access" is not allowed.

Exits 1 on any failure; a night is green only when this exits 0. Writes the page to stdout, to
``--out`` and to ``GITHUB_STEP_SUMMARY``.
"""

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ssc_conformance import evidence as ev
from ssc_conformance.evidence import Evidence, EvidenceError, Result

BROWSER_ENTRY: Final = "browser.public_entry"
BROWSER_LOGIN: Final = "browser.login_sessions"
BROWSER_CROSS_CELL: Final = "browser.cross_cell_sessions"
BROWSER_SECOND_USER: Final = "browser.second_user"
BUILD_BUNDLE: Final = "least_privilege.build_account_bundle_only"
STAFF: Final = "least_privilege.no_standing_staff_access"
DENY_READ: Final = "least_privilege.secret_read_denied"
VERSIONS_ONLY: Final = "least_privilege.control_adds_versions_only"
ORG_POLICIES: Final = "org_policies"
DRIFT: Final = "drift"
CERTIFICATE: Final = "certificate"
DRILL: Final = "drill"
DATAGW_READ_ONLY: Final = "datagw_read_only"
CROSS_CELL: Final = frozenset({"cannot_reach_peer_cell", "deny_peer_cell", BROWSER_CROSS_CELL})
NOT_AUTOMATED: Final = "not automated"


@dataclass(frozen=True, slots=True)
class Row:
    """A row of the isolation matrix. ``manual`` is how it is proved today when nothing here is."""

    title: str
    proofs: tuple[str, ...] = ()
    manual: str = ""


ROWS: Final = (
    Row("App data in Postgres", manual="SSC-040 cross-connect test; restore drill per customer"),
    Row("Files", manual="SSC-046 prefix test"),
    Row(
        "Secrets and credentials",
        (
            "cannot_read_secrets",
            "no_platform_credentials_in_env",
            "deny_peer_cell",
            DENY_READ,
            VERSIONS_ONLY,
        ),
    ),
    Row("Encryption keys", manual="cell diff"),
    Row("Network", ("no_direct_egress", "no_dns_exfil", "cannot_reach_peer_cell")),
    Row("App to app, across customers", ("cannot_reach_peer_cell", "deny_peer_cell")),
    Row("App to app, same customer", ("cannot_reach_peer_app",)),
    Row(
        "Runtime",
        (
            "metadata_token_no_roles",
            "metadata_identity_is_own",
            "non_root_10001",
            "no_write_outside_memory",
        ),
    ),
    Row("Identities", manual="cell diff; agent tests"),
    Row("Public entry and TLS", (BROWSER_ENTRY, CERTIFICATE)),
    Row("Outbound IP", manual="published in the console"),
    Row("Login sessions", (BROWSER_LOGIN, BROWSER_CROSS_CELL, BROWSER_SECOND_USER)),
    Row(
        "Rules that govern apps (sharing, connections, allowed hosts, timers)",
        manual="row-level security and SC001 tests; audit chain per org; the outside security test",
    ),
    Row("Logs and audit", manual="audit verify command"),
    Row("Images and source", (BUILD_BUNDLE,)),
    Row("Misconfiguration by us", (ORG_POLICIES,)),
    Row("Our own staff", (STAFF,)),
)
ALSO: Final = (DRIFT, CERTIFICATE, DRILL, DATAGW_READ_ONLY)
ALSO_TITLES: Final = {
    DRIFT: "Drift repaired within a minute",
    CERTIFICATE: "Certificate has 21 days or more left",
    DRILL: "Kill switch drill (SSC-054)",
    DATAGW_READ_ONLY: "Read-only matrix against the data gateway (SSC-051)",
}


@dataclass(frozen=True, slots=True)
class Judged:
    """One proof's results, one per cell, after the page's rules."""

    proof: str
    by_cell: Mapping[str, Result]

    @property
    def status(self) -> ev.Status:
        statuses = {r.status for r in self.by_cell.values()}
        if ev.FAIL in statuses:
            return ev.FAIL
        return ev.SKIPPED if ev.SKIPPED in statuses else ev.OK

    def text(self) -> str:
        results = list(self.by_cell.items())
        if len({(r.status, r.reason) for _, r in results}) == 1:
            return _say(results[0][1])
        return ", ".join(f"{cell} {_say(r)}" for cell, r in results)


def _say(result: Result) -> str:
    if result.status == ev.OK or not result.reason:
        return result.status
    return f"{result.status} ({result.reason})"


def _allowed(proof: str, reason: str, *, need_peer: bool) -> bool:
    if reason.startswith(ev.NO_PEER):
        return not need_peer
    if reason.startswith(ev.NEEDS_SECOND_USER):
        return True
    return proof in ALSO and reason.startswith(ev.WAITS_FOR_DATAGW)


def _crosses(evidence: Evidence) -> bool:
    """Whether a file reports a proof that crosses cells: only those say if the run had a peer, a
    file of the drill or the read-only checks does not know."""
    return any(r.proof in CROSS_CELL for r in evidence.results)


def judge_one(proof: str, cell: str, files: Sequence[Evidence], *, need_peer: bool) -> Result:
    """The result of ``proof`` in ``cell`` once the page's rules are applied."""
    found = [r for e in files if e.cell == cell for r in e.results if r.proof == proof]
    if not found:
        return Result(proof, ev.FAIL, "did not report")
    if len(found) > 1:
        return Result(proof, ev.FAIL, f"reported {len(found)} times")
    result = found[0]
    if proof in CROSS_CELL and not all(e.peer for e in files if e.cell == cell and _crosses(e)):
        if result.status == ev.FAIL:
            return result
        result = Result(proof, ev.SKIPPED, ev.NO_PEER)
    if result.status == ev.SKIPPED and not _allowed(proof, result.reason, need_peer=need_peer):
        return Result(proof, ev.FAIL, f"skipped, {result.reason}; a skip that is not allowed")
    return result


@dataclass(frozen=True, slots=True)
class Page:
    cells: tuple[str, ...]
    peer: bool
    proofs: Mapping[str, Judged]

    def row_status(self, row: Row) -> str:
        if not row.proofs:
            return NOT_AUTOMATED
        return _worst([self.proofs[p].status for p in row.proofs])

    def failures(self) -> list[str]:
        found: list[str] = []
        for judged in self.proofs.values():
            for cell, result in judged.by_cell.items():
                if result.status == ev.FAIL:
                    found.append(f"{judged.proof} ({cell}): {result.reason or 'failed'}")
        return found


def _worst(statuses: Sequence[str]) -> str:
    for status in (ev.FAIL, ev.SKIPPED):
        if status in statuses:
            return status
    return ev.OK


def judge(files: Sequence[Evidence], cells: Sequence[str], *, need_peer: bool = False) -> Page:
    """Every proof of the page, in each of ``cells``."""
    wanted = dict.fromkeys(p for row in ROWS for p in row.proofs) | dict.fromkeys(ALSO)
    proofs = {
        proof: Judged(proof, {c: judge_one(proof, c, files, need_peer=need_peer) for c in cells})
        for proof in wanted
    }
    peer = len(cells) > 1 and all(e.peer for e in files if _crosses(e))
    return Page(tuple(cells), peer, proofs)


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def markdown(page: Page, *, need_peer: bool = False) -> str:
    names = ", ".join(page.cells)
    peers = "two cells" if page.peer else "one cell: lines across cells are skipped, no peer cell"
    lines = [
        "| # | What could leak | Proof | Tonight |",
        "| --- | --- | --- | --- |",
    ]
    for number, row in enumerate(ROWS, 1):
        if row.proofs:
            proof = "<br>".join(f"{p}: {_cell(page.proofs[p].text())}" for p in row.proofs)
        else:
            proof = f"{NOT_AUTOMATED}: {row.manual}"
        lines.append(f"| {number} | {_cell(row.title)} | {proof} | {page.row_status(row)} |")
    lines += ["", "Also checked tonight (not rows of the matrix)", ""]
    lines += ["| Check | Tonight |", "| --- | --- |"]
    lines += [f"| {ALSO_TITLES[p]} | {_cell(page.proofs[p].text())} |" for p in ALSO]
    failures = page.failures()
    verdict = "RED" if failures else "GREEN"
    head = [f"Cells tonight: {names} ({peers}). Night is {verdict}."]
    if need_peer:
        head.append("Two cells are required tonight: a skip across cells fails.")
    tail = ["", *(f"- FAILED {f}" for f in failures)] if failures else []
    return "\n".join([*head, "", *lines, *tail]) + "\n"


def section3_titles(text: str) -> list[str]:
    """The first column of the isolation matrix table in the architecture document."""
    found: list[str] = []
    lines = iter(text.splitlines())
    for line in lines:
        if re.match(r"^## 3\. The isolation matrix", line):
            break
    for line in lines:
        if line.startswith("## "):
            break
        if line.startswith("|") and not line.startswith(("| What could leak", "|---", "| ---")):
            found.append(line.split("|")[1].strip())
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--cells", required=True, help="comma-separated cell names")
    parser.add_argument("--need-peer", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    cells = [c for c in args.cells.split(",") if c]
    try:
        files = ev.read_all(args.evidence)
    except EvidenceError as exc:
        sys.stderr.write(f"matrix: {exc}\n")
        return 1
    if not cells:
        sys.stderr.write("matrix: no cells\n")
        return 1
    page = judge(files, cells, need_peer=args.need_peer)
    text = "## Isolation matrix\n\n" + markdown(page, need_peer=args.need_peer)
    sys.stdout.write(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as f:
            f.write(text)
    return 1 if page.failures() else 0


if __name__ == "__main__":
    sys.exit(main())
