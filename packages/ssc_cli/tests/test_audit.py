"""``ssc audit``: export the org's audit log and check an export's hash chain offline (SSC-012,
decision 012, GA-3.5)."""

import base64
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import httpx2
import pytest

from ssc_cli.audit_chain import GENESIS_HASH, canonical_bytes, check_lines
from ssc_cli.credentials import SERVICE
from ssc_cli.errors import ExitCode
from ssc_cli.session import Session
from ssc_cli.shapes import AuditExportResult, AuditVerifyResult

ORG = "org_aaaaaaaaaaaaaaaaaaaa"
USR = "usr_aaaaaaaaaaaaaaaaaaaa"


def _chain(n: int, *, start: int = 1, org: str = ORG) -> list[dict[str, Any]]:
    """``n`` export rows as the control plane writes them, from ``start``."""
    rows: list[dict[str, Any]] = []
    prev = GENESIS_HASH if start == 1 else hashlib.sha256(b"earlier").digest()
    for seq in range(start, start + n):
        at = f"2026-10-08T13:08:{seq % 60:02d}.5+00:00"
        actor = {"kind": "user", "id": USR, "via_agent": False, "client_id": None}
        target = {"kind": "app", "id": f"app_{seq}"}
        after = {"status": "disabled", "n": seq}
        canonical = canonical_bytes(
            {
                "org_id": org,
                "seq": seq,
                "at": at,
                "action": "app.disabled",
                "actor": actor | {"ip": "203.0.113.7"},
                "target": target,
                "before": None,
                "after": after,
                "policy_decision_id": None,
            }
        )
        digest = hashlib.sha256(prev + canonical).digest()
        rows.append(
            {
                "seq": seq,
                "at": at.replace("+00:00", "Z"),
                "action": "app.disabled",
                "actor": actor,
                "target": target,
                "before": None,
                "after": after,
                "policy_decision_id": None,
                "prev_hash": prev.hex(),
                "hash": digest.hex(),
                "canonical": base64.b64encode(canonical).decode(),
            }
        )
        prev = digest
    return rows


def _lines(rows: list[dict[str, Any]]) -> list[str]:
    return [canonical_bytes(r).decode() + "\n" for r in rows]


def _write(tmp_path: Path, rows: list[dict[str, Any]] | list[str]) -> Path:
    path = tmp_path / f"{uuid.uuid4().hex}.jsonl"
    lines = [r if isinstance(r, str) else canonical_bytes(r).decode() + "\n" for r in rows]
    path.write_text("".join(lines))
    return path


def test_an_untouched_chain_from_genesis_checks_out():
    rows = _chain(5)
    report = check_lines(_lines(rows))
    assert report.ok and report.from_genesis
    assert (report.checked, report.first_seq, report.last_seq) == (5, 1, 5)
    assert (report.org_id, report.last_hash) == (ORG, rows[-1]["hash"])


def test_a_filtered_export_checks_from_its_first_row():
    report = check_lines(_lines(_chain(3, start=40)))
    assert report.ok and not report.from_genesis
    assert (report.first_seq, report.last_seq) == (40, 42)


@pytest.mark.parametrize(
    ("tamper", "line", "seq", "cause"),
    [
        (lambda rows: rows.__setitem__(2, rows[2] | {"action": "app.enabled"}), 3, 3, "fields"),
        (lambda rows: rows[2]["after"].__setitem__("n", 99), 3, 3, "fields"),
        (
            lambda rows: rows.__setitem__(2, rows[2] | {"at": "2026-10-08T14:00:00Z"}),
            3,
            3,
            "fields",
        ),
        (lambda rows: rows.__setitem__(2, rows[2] | {"hash": "00" * 32}), 3, 3, "hash"),
        (lambda rows: rows.__delitem__(2), 3, 3, "missing"),
        (lambda rows: rows.__setitem__(0, rows[0] | {"prev_hash": "11" * 32}), 1, 1, "prev_link"),
    ],
)
def test_a_change_names_the_first_broken_link(tamper, line, seq, cause):
    rows = _chain(5)
    tamper(rows)
    report = check_lines(_lines(rows))
    assert not report.ok
    assert (report.broken_line, report.broken_seq, report.cause) == (line, seq, cause)
    assert report.checked == line - 1


def test_a_recomputed_hash_still_breaks_the_next_link():
    """Rewriting a row and its own hash leaves the next row pointing at the old hash."""
    rows = _chain(4)
    canonical = json.loads(base64.b64decode(rows[1]["canonical"]))
    canonical["after"] = rows[1]["after"] = {"status": "enabled", "n": 2}
    raw = canonical_bytes(canonical)
    rows[1]["canonical"] = base64.b64encode(raw).decode()
    rows[1]["hash"] = hashlib.sha256(bytes.fromhex(rows[1]["prev_hash"]) + raw).hexdigest()
    report = check_lines(_lines(rows))
    assert (report.broken_line, report.cause) == (3, "prev_link")


def test_rows_of_another_org_and_garbage_are_refused():
    mixed = _lines(_chain(2))
    other = _chain(3, org="org_bbbbbbbbbbbbbbbbbbbb")
    mixed.append(_lines(other)[2].replace(other[1]["hash"], json.loads(mixed[1])["hash"]))
    assert check_lines(mixed).cause in {"hash", "fields"}
    assert check_lines([*_lines(_chain(1)), "{not json\n"]).cause == "unreadable"
    assert check_lines(["seq,at,action\n"]).cause == "unreadable"


def test_verify_prints_the_span_and_the_last_hash(cli, tmp_path):
    rows = _chain(3)
    path = _write(tmp_path, rows)
    r = cli("audit", "verify", str(path))
    assert r.code == 0, r.stderr
    assert f"Chain intact: 3 events of {ORG}, seq 1 to 3." in r.stdout
    assert rows[-1]["hash"] in r.stdout
    filtered = cli("audit", "verify", str(_write(tmp_path, _chain(2, start=9))))
    assert "links before it were not checked" in filtered.stdout


def test_verify_exits_1_on_a_break_and_says_where(cli, tmp_path):
    rows = _chain(4)
    rows[3]["target"] = {"kind": "app", "id": "app_other"}
    path = _write(tmp_path, rows)
    r = cli("audit", "verify", str(path), "--json")
    assert r.code == ExitCode.FAILED
    result = AuditVerifyResult.model_validate(r.json())
    assert (result.ok, result.broken_line, result.cause, result.checked) == (False, 4, "fields", 3)
    human = cli("audit", "verify", str(path))
    assert "Chain broken at line 4 (seq 4): its canonical bytes" in human.stdout
    assert "The 3 events before it check out." in human.stdout


def test_verify_needs_no_login(cli, tmp_path, isolated):
    assert isolated.get_password(SERVICE, "https://api.test") is None
    assert cli("audit", "verify", str(_write(tmp_path, _chain(1)))).code == 0


@pytest.fixture
def scripted(fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    return fake_api


def _export_response(body: bytes) -> httpx2.Response:
    disposition = f'attachment; filename="audit-{ORG}.jsonl"'
    return httpx2.Response(
        200,
        content=body,
        headers={"content-type": "application/x-ndjson", "content-disposition": disposition},
    )


def test_export_writes_the_file_the_api_names(cli, scripted, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = "".join(_lines(_chain(3))).encode()
    scripted.add("GET", "/v1/audit/export", _export_response(body))
    r = cli("audit", "export", "--since", "2026-10-01T00:00:00Z", session=scripted.session())
    assert r.code == 0, r.stderr
    sent = scripted.seen[-1].url.params
    assert (sent["format"], sent["since"], "until" in sent) == (
        "jsonl",
        "2026-10-01T00:00:00Z",
        False,
    )
    assert (tmp_path / f"audit-{ORG}.jsonl").read_bytes() == body
    assert "Wrote 3 events" in r.stdout
    assert f"ssc audit verify audit-{ORG}.jsonl" in r.stdout


def test_export_never_overwrites(cli, scripted, tmp_path):
    out = tmp_path / "a.jsonl"
    out.write_text("kept\n")
    scripted.add("GET", "/v1/audit/export", _export_response(b"{}\n"))
    r = cli("audit", "export", "--out", str(out), "--json", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == "FILE_EXISTS"
    assert out.read_text() == "kept\n"
    assert not scripted.seen


def test_export_refusal_is_the_apis(cli, scripted, fake_problem, tmp_path):
    scripted.add("GET", "/v1/audit/export", fake_problem(403, "FORBIDDEN"))
    out = tmp_path / "a.jsonl"
    r = cli("audit", "export", "--out", str(out), "--json", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == "FORBIDDEN"
    assert not out.exists()


def test_live_export_verifies_from_genesis(cli, live, isolated, tmp_path):
    """The control plane's own export, events written by real requests, checks out offline."""
    isolated.set_password(SERVICE, live.url, live.token())
    session = Session(api_override=live.url)
    assert cli("apps", "create", f"t{uuid.uuid4().hex[:12]}", session=session).code == 0
    out = tmp_path / "live.jsonl"
    exported = cli("audit", "export", "--out", str(out), "--json", session=session)
    assert exported.code == 0, (exported.stdout, exported.stderr)
    assert AuditExportResult.model_validate(exported.json()).events >= 1
    r = cli("audit", "verify", str(out), "--json")
    assert r.code == 0, r.stdout
    result = AuditVerifyResult.model_validate(r.json())
    assert result.ok and result.from_genesis and result.org_id
    csv_out = tmp_path / "live.csv"
    csv = cli("audit", "export", "--format", "csv", "--out", str(csv_out), session=session)
    assert csv.code == 0 and csv_out.read_text().startswith("seq,at,action")
    assert "ssc audit verify" not in csv.stdout
