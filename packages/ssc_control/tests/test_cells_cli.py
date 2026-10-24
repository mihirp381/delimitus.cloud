"""Ticket 19A: ``python -m ssc_control.cells``, the operator's writes to ``ssc.cell``, through
``main(argv)`` against postgres:18."""

import json
import logging
from pathlib import Path

import psycopg
import pytest
from ssc_testkit import Dsns, make_org, new_key

from ssc_contracts.ids import new_id
from ssc_control.cells import __main__ as cli
from ssc_control.db import bind_org_sync
from ssc_shared.hosts import PROOF_LABELS

OP = "op_dana"
JWKS = {"keys": [{"kid": "gw-1", "kty": "EC", "crv": "P-256", "x": "AA", "y": "BB"}]}
OTHER_JWKS = {"keys": [{"kid": "gw-2", "kty": "EC", "crv": "P-256", "x": "CC", "y": "DD"}]}

type Out = pytest.CaptureFixture[str]


@pytest.fixture(autouse=True)
def database(dsns: Dsns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSC_DATABASE_DSN", dsns.app)
    monkeypatch.delenv("SSC_CELLS", raising=False)


def fresh_label() -> str:
    return "t" + new_key().replace("-", "")[:11].lower()


def row(dsn: str, label: str) -> tuple[object, ...] | None:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "select stage, status, identity_jwks, project_number, org_id, last_error "
            "from ssc.cell where label = %s",
            (label,),
        ).fetchone()


def events(dsn: str, org_id: str) -> list[tuple[object, ...]]:
    with psycopg.connect(dsn) as conn, conn.transaction():
        bind_org_sync(conn, org_id)
        return conn.execute(
            "select action, actor_kind, actor_id, target_id, before, after from ssc.audit_event "
            "where org_id = %s and target_kind = 'cell' order by seq",
            (org_id,),
        ).fetchall()


def ok(argv: list[str], capsys: Out) -> str:
    assert cli.main(argv) == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    return out.strip()


def refused(argv: list[str], capsys: Out) -> str:
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    return captured.err.strip()


def jwks_file(tmp_path: Path, doc: object = JWKS) -> str:
    path = tmp_path / f"{new_key()}.json"
    path.write_text(json.dumps(doc))
    return str(path)


def request(label: str, *extra: str) -> list[str]:
    return ["request", "--label", label, "--stage", "prod", "--operator", OP, *extra]


def ready(label: str, path: str, number: str = "1234") -> list[str]:
    argv = ["ready", "--label", label, "--jwks-file", path, "--project-number", number]
    return [*argv, "--operator", OP]


def test_a_cell_is_requested_made_ready_and_retired(
    dsns: Dsns, tmp_path: Path, capsys: Out
) -> None:
    org = make_org(dsns.app, "Cells")
    label = fresh_label()
    assert ok(request(label, "--org-id", org.org_id), capsys).startswith(f"{label} prod building")
    assert row(dsns.app, label) == ("prod", "building", None, None, org.org_id, None)
    line = ok(ready(label, jwks_file(tmp_path)), capsys)
    assert line.startswith(f"{label} prod ready project=1234 org={org.org_id}")
    assert "gw-1" not in line
    assert row(dsns.app, label) == ("prod", "ready", JWKS, 1234, org.org_id, None)
    ok(["retire", "--label", label, "--operator", OP], capsys)
    assert row(dsns.app, label)[:2] == ("prod", "retired")  # type: ignore[index]
    assert events(dsns.app, org.org_id) == [
        ("cell.requested", "operator", OP, label, None, {"stage": "prod", "status": "building"}),
        (
            "cell.ready",
            "operator",
            OP,
            label,
            {"status": "building"},
            {"status": "ready", "project_number": 1234},
        ),
        ("cell.retired", "operator", OP, label, {"status": "ready"}, {"status": "retired"}),
    ]


def test_a_failed_build_is_recorded_and_can_still_be_made_ready(
    dsns: Dsns, tmp_path: Path, capsys: Out
) -> None:
    org = make_org(dsns.app, "Failing")
    label = fresh_label()
    ok(request(label, "--org-id", org.org_id), capsys)
    fail = ["fail", "--label", label, "--reason", "quota exceeded in region", "--operator", OP]
    assert ok(fail, capsys).startswith(f"{label} prod failed")
    assert row(dsns.app, label)[5] == "quota exceeded in region"  # type: ignore[index]
    assert "failed, not building" in refused(fail, capsys)
    ok(ready(label, jwks_file(tmp_path)), capsys)
    assert row(dsns.app, label)[1:6:4] == ("ready", None)  # type: ignore[index]
    assert [e[0] for e in events(dsns.app, org.org_id)] == [
        "cell.requested",
        "cell.failed",
        "cell.ready",
    ]
    assert events(dsns.app, org.org_id)[1][5] == {
        "status": "failed",
        "reason": "quota exceeded in region",
    }


def test_a_cell_with_no_org_yet_is_logged_not_audited(
    dsns: Dsns, tmp_path: Path, capsys: Out, caplog: pytest.LogCaptureFixture
) -> None:
    label, org_id = fresh_label(), new_id("org")
    with caplog.at_level(logging.INFO, logger="ssc.cells"):
        ok(request(label, "--org-id", org_id), capsys)
        ok(ready(label, jwks_file(tmp_path)), capsys)
    logged = [r for r in caplog.records if r.name == "ssc.cells"]
    assert [(r.getMessage(), r.__dict__["cell_label"]) for r in logged] == [
        (f"cell.requested {label} by {OP}", label),
        (f"cell.ready {label} by {OP}", label),
    ]
    assert all(r.__dict__["actor_id"] == OP for r in logged)


def test_request_refuses_a_bad_label_one_in_use_and_the_proof_cells(
    dsns: Dsns, capsys: Out
) -> None:
    org = make_org(dsns.app, "Requests")
    label = fresh_label()
    ok(request(label, "--org-id", org.org_id), capsys)
    assert f"cell {label} already exists" in refused(request(label), capsys)
    taken = refused(request(fresh_label(), "--org-id", org.org_id), capsys)
    assert f"{org.org_id} already has cell {label}" in taken
    for proof in sorted(PROOF_LABELS):
        assert "proof-run cell" in refused(request(proof), capsys)
        assert row(dsns.app, proof) is None
    with pytest.raises(SystemExit):
        cli.main(request("Not-A-Label"))
    assert "--label" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["request", "--label", fresh_label(), "--stage", "dev", "--operator", OP])
    with pytest.raises(SystemExit):
        cli.main(["request", "--label", fresh_label(), "--stage", "prod", "--operator", "dana"])
    assert "--operator" in capsys.readouterr().err
    assert [e[0] for e in events(dsns.app, org.org_id)] == ["cell.requested"]


def test_ready_checks_the_keys_and_the_status(dsns: Dsns, tmp_path: Path, capsys: Out) -> None:
    label = fresh_label()
    assert f"no cell {label}" in refused(ready(label, jwks_file(tmp_path)), capsys)
    ok(request(label), capsys)
    private = {"keys": [{**JWKS["keys"][0], "d": "secret"}]}
    for doc in ({"keys": []}, private, ["not", "an", "object"]):
        err = refused(ready(label, jwks_file(tmp_path, doc)), capsys)
        assert "secret" not in err
    missing = refused(ready(label, str(tmp_path / "absent.json")), capsys)
    assert "absent.json" in missing
    with pytest.raises(SystemExit):
        cli.main(ready(label, jwks_file(tmp_path), "0"))
    assert row(dsns.app, label)[1] == "building"  # type: ignore[index]
    ok(ready(label, jwks_file(tmp_path)), capsys)
    again = refused(ready(label, jwks_file(tmp_path, OTHER_JWKS)), capsys)
    assert "ready, not building or failed" in again
    assert row(dsns.app, label)[2] == JWKS  # type: ignore[index]


def test_fail_needs_a_building_cell_and_a_reason_fit_for_the_log(dsns: Dsns, capsys: Out) -> None:
    label = fresh_label()
    fail = ["fail", "--label", label, "--operator", OP, "--reason"]
    assert f"no cell {label}" in refused([*fail, "timed out"], capsys)
    ok(request(label), capsys)
    for reason in ("mail ops@example.com", "ticket 1234567", ""):
        with pytest.raises(SystemExit):
            cli.main([*fail, reason])
    capsys.readouterr()
    assert row(dsns.app, label)[1] == "building"  # type: ignore[index]


def test_retire_refuses_a_cell_an_org_still_names(dsns: Dsns, capsys: Out) -> None:
    org = make_org(dsns.app, "Named")
    label = org.cell_label
    ok(request(label), capsys)
    err = refused(["retire", "--label", label, "--operator", OP], capsys)
    assert f"{org.org_id} is on cell {label}" in err
    assert row(dsns.app, label)[1] == "building"  # type: ignore[index]
    other = fresh_label()
    assert f"no cell {other}" in refused(["retire", "--label", other, "--operator", OP], capsys)
    ok(request(other), capsys)
    ok(["retire", "--label", other, "--operator", OP], capsys)
    assert "already retired" in refused(["retire", "--label", other, "--operator", OP], capsys)
    assert row(dsns.app, other)[1] == "retired"  # type: ignore[index]


def test_import_env_writes_ready_cells_once(
    dsns: Dsns, tmp_path: Path, capsys: Out, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = sorted([fresh_label(), fresh_label()])
    cells = {a: {"identity_jwks": JWKS}, b: {"identity_jwks": OTHER_JWKS}}
    monkeypatch.setenv("SSC_CELLS", json.dumps(cells))
    argv = ["import-env", "--stage", "prod", "--operator", OP]
    assert ok(argv, capsys) == f"imported {a} {b}; unchanged none"
    assert row(dsns.app, a) == ("prod", "ready", JWKS, None, None, None)
    assert row(dsns.app, b) == ("prod", "ready", OTHER_JWKS, None, None, None)
    assert ok(argv, capsys) == f"imported none; unchanged {a} {b}"
    monkeypatch.delenv("SSC_CELLS")
    path = jwks_file(tmp_path, {a: {"identity_jwks": OTHER_JWKS}})
    err = refused([*argv, "--file", path], capsys)
    assert f"cell {a} is ready with other keys" in err
    assert row(dsns.app, a)[2] == JWKS  # type: ignore[index]
    staging = refused(["import-env", "--stage", "staging", "--operator", OP], capsys)
    assert "SSC_CELLS" in staging
    path = jwks_file(tmp_path, {a: {"identity_jwks": JWKS}})
    err = refused(["import-env", "--stage", "staging", "--operator", OP, "--file", path], capsys)
    assert f"cell {a} is prod" in err


def test_import_env_refuses_a_malformed_map_and_writes_nothing(
    dsns: Dsns, tmp_path: Path, capsys: Out
) -> None:
    good = fresh_label()
    doc = {good: {"identity_jwks": JWKS}, "BAD": {"identity_jwks": JWKS}}
    argv = ["import-env", "--stage", "prod", "--operator", OP, "--file"]
    assert "'BAD' is not a cell label" in refused([*argv, jwks_file(tmp_path, doc)], capsys)
    private = {good: {"identity_jwks": {"keys": [{**JWKS["keys"][0], "d": "secret"}]}}}
    err = refused([*argv, jwks_file(tmp_path, private)], capsys)
    assert "private key" in err
    assert "secret" not in err
    assert row(dsns.app, good) is None


def test_status_and_list_print_one_line_per_cell_and_no_keys(
    dsns: Dsns, tmp_path: Path, capsys: Out
) -> None:
    label = fresh_label()
    assert f"no cell {label}" in refused(["status", "--label", label], capsys)
    ok(request(label), capsys)
    ok(ready(label, jwks_file(tmp_path)), capsys)
    line = ok(["status", "--label", label], capsys)
    assert line.startswith(f"{label} prod ready project=1234 org=- updated=")
    assert cli.main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [x for x in lines if x.startswith(f"{label} ")] == [line]
    assert not [x for x in lines if "gw-1" in x or "keys" in x]


def test_the_commands_need_the_database_dsn(monkeypatch: pytest.MonkeyPatch, capsys: Out) -> None:
    monkeypatch.delenv("SSC_DATABASE_DSN")
    assert cli.main(["list"]) == 2
    assert "SSC_DATABASE_DSN" in capsys.readouterr().err
