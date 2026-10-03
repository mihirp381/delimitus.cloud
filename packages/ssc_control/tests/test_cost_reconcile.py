"""SSC-096: the cost model as data and the monthly reconciliation, on a small fixture month.

October 2026 (31 days). The fixture, priced so each case lands on a known figure:

* ``ssc-c-cellalpha01``, made before the month, its database added on 16 October, one app of
  each usage type, billed;
* ``ssc-c-cellbravo01``, made on 11 October with no events at all, billed: a bill and no events;
* ``ssc-c-ghostcell01``, billed, unknown to the control plane: a bill with no record;
* ``ssc-c-cellcharlie``, full, one rare app, and no bill: events with no bill;
* ``ssc-control-prod``, whose Cloud Run line is 25 % over the model; ``ssc-control-staging``
  exactly on it.

Ticket "done when" lines that need a real bill (the first run after SSC-086 T9, the second on
dogfood) are steps in ``docs/runbooks/ssc-096-cost-reconciliation.md``; nothing here reaches a
cloud or a billing API.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from ssc_testkit import ISSUER, Dsns

from ssc_contracts.cells import MONTHLY_USD
from ssc_contracts.ids import new_id
from ssc_control.db import NewOrg, bound_org, create_org, make_engine
from ssc_control.metrics import cost_model as cm
from ssc_control.metrics import reconcile as rc
from ssc_control.metrics import record_once
from ssc_control.metrics.usage import FixedResource
from ssc_control.ports import MetricKind

MONTH = date(2026, 10, 1)
MODEL = cm.load()
ALPHA, BRAVO, GHOST, CHARLIE = (
    "ssc-c-cellalpha01",
    "ssc-c-cellbravo01",
    "ssc-c-ghostcell01",
    "ssc-c-cellcharlie",
)
PROD, STAGING = "ssc-control-prod", "ssc-control-staging"
RARE, DAILY, SESSION, HEAVY, LONE = (f"app_{c * 20}" for c in "rdshl")
HOURS_176 = 176 * 3600.0


def at(day: int, month: int = 10) -> datetime:
    return datetime(2026, month, day, tzinfo=UTC)


ALPHA_APPS = (
    rc.AppMonth(RARE, 1, request_seconds=7_800.0, cold_starts=600),
    rc.AppMonth(DAILY, 2, request_seconds=79_200.0, cold_starts=40),
    rc.AppMonth(
        SESSION, 1, instance_billed_seconds=HOURS_176, session_seconds=HOURS_176, cold_starts=22
    ),
    rc.AppMonth(HEAVY, 1, request_seconds=300_000.0, cold_starts=5),
)
CELLS = (
    rc.CellMonth(
        ALPHA,
        at(1, 9),
        (FixedResource("database", at(16)),),
        ALPHA_APPS,
        gateway_session_seconds=HOURS_176,
    ),
    rc.CellMonth(BRAVO, at(11)),
    rc.CellMonth(
        CHARLIE,
        at(1, 8),
        (FixedResource("database", at(2, 8)), FixedResource("egress", at(3, 8))),
        (rc.AppMonth(LONE, 1, request_seconds=1_300.0, cold_starts=100),),
    ),
)
BILL_CSV = """month,project_id,service,usd,ignored
2026-10,ssc-control-prod,Cloud Run,50.00,x
2026-10,ssc-control-prod,Cloud SQL,10.00,
2026-10,ssc-control-prod,Networking,18.25,
2026-10,ssc-control-prod,Secret Manager,0.42,
2026-10,ssc-control-staging,Cloud Run,30.00,
2026-10,ssc-control-staging,Cloud SQL,10.00,
2026-10,ssc-control-staging,Secret Manager,0.42,
2026-10,ssc-c-cellalpha01,Networking,22.25,
2026-10,ssc-c-cellalpha01,Cloud SQL,6.71,
2026-10,ssc-c-cellalpha01,Cloud Key Management Service (KMS),0.50,
2026-10,ssc-c-cellalpha01,Cloud DNS,0.60,
2026-10,ssc-c-cellalpha01,Artifact Registry,0.30,
2026-10,ssc-c-cellalpha01,Artifact Registry,0.20,
2026-10,ssc-c-cellalpha01,Cloud Storage,0.40,
2026-10,ssc-c-cellalpha01,Cloud Run,40.00,
2026-10,ssc-c-cellalpha01,Cloud Build,1.20,
2026-10,ssc-c-cellalpha01,Cloud Logging,0.30,
2026-10,ssc-c-cellbravo01,Networking,15.07,
2026-10,ssc-c-cellbravo01,Cloud Key Management Service (KMS),1.36,
2026-10,ssc-c-cellbravo01,Cloud Run,0.40,
2026-10,ssc-c-ghostcell01,Networking,20.00,
"""


def found(reasons: dict[str, str] | None = None, bill: str = BILL_CSV) -> rc.Reconciliation:
    return rc.reconcile(
        MONTH, rc.read_bill(bill, MONTH), CELLS, MODEL, reasons=reasons, bill_name="bill.csv"
    )


def line(rec: rc.Reconciliation, id_: str) -> rc.Line:
    return next(x for x in rec.lines() if x.id == id_)


def project(rec: rc.Reconciliation, pid: str) -> rc.ProjectRow:
    return next(p for p in rec.projects if p.project_id == pid)


def app(rec: rc.Reconciliation, app_id: str) -> rc.AppRow:
    return next(a for a in rec.apps if a.app.app_id == app_id)


def test_a_cell_made_mid_month_carries_its_fixed_parts_from_that_day() -> None:
    bravo = next(f for f in found().fixed if f.project_id == BRAVO)
    assert {p.name for p in bravo.parts} == {"load_balancer", "nat", "basics"}
    assert {round(p.fraction, 6) for p in bravo.parts} == {round(21 / 31, 6)}
    assert bravo.line.low == pytest.approx(24.25 * 21 / 31)
    assert bravo.line.measured == pytest.approx(16.43)
    assert bravo.line.status == "ok"
    assert bravo.shape == "empty"


def test_a_database_added_later_counts_from_the_day_it_was_ready() -> None:
    alpha = next(f for f in found().fixed if f.project_id == ALPHA)
    shares = {p.name: p.fraction for p in alpha.parts}
    assert shares == {
        "load_balancer": 1.0,
        "nat": 1.0,
        "basics": 1.0,
        "database": pytest.approx(16 / 31),
    }
    assert alpha.line.low == pytest.approx(24.25 + 13 * 16 / 31)
    assert alpha.line.measured == pytest.approx(30.96)
    assert alpha.line.status == "ok"
    assert alpha.shape == "database"
    charlie = next(f for f in found().fixed if f.project_id == CHARLIE)
    assert charlie.shape == "full"
    assert charlie.line.low == pytest.approx(MODEL.cell_month(("database", "egress")))


def test_a_resource_never_counts_from_before_its_cell() -> None:
    cell = rc.CellMonth(BRAVO, at(20), (FixedResource("database", at(5)),))
    shares = {p.name: p.since for p in rc.fixed_parts(MODEL, cell, MONTH)}
    assert shares["database"] == at(20)


def test_one_app_of_each_usage_type_with_its_hours_cold_starts_and_cost() -> None:
    rec = found()
    assert [app(rec, a).app.usage_type for a in (RARE, DAILY, SESSION, HEAVY)] == [
        "rare",
        "daily",
        "session",
        "heavy",
    ]
    request, instance = MODEL.per_second("request"), MODEL.per_second("instance")
    assert app(rec, RARE).model_usd == pytest.approx(7_800 * request)
    assert app(rec, DAILY).model_usd == pytest.approx(2.0, abs=0.01)
    assert app(rec, SESSION).model_usd == pytest.approx(176 * 0.0684, abs=0.001)
    assert app(rec, HEAVY).model_usd == pytest.approx(300_000 * request)
    assert instance < request
    for a in ALPHA_APPS:
        row = app(rec, a.app_id)
        assert row.attributed_usd == pytest.approx(row.model_usd)
    session = as_json(rec)["apps"]
    assert {(a["app_id"], a["session_hours"], a["cold_starts"]) for a in session} >= {
        (SESSION, 176.0, 22),
        (RARE, 0.0, 600),
    }
    types = {t.id: (t, n) for t, n in rec.types}
    assert [types[f"type:{k}"][0].status for k in rc.TYPES] == ["ok"] * 4
    assert types["type:rare"][1] == 1


def test_the_cloud_run_bill_left_after_the_apps_is_its_own_line_against_the_gateway() -> None:
    rec = found()
    priced = sum(a.price(MODEL) for a in ALPHA_APPS)
    rest = line(rec, f"cloud_run_rest:{ALPHA}")
    assert rest.measured == pytest.approx(40.0 - priced)
    assert rest.low == pytest.approx(176 * MODEL.hourly("request"))
    assert rest.status == "ok"
    others = {x.id: x for x in rec.services if x.id.startswith(f"other:{ALPHA}")}
    assert set(others) == {f"other:{ALPHA}:Cloud Build", f"other:{ALPHA}:Cloud Logging"}
    assert not any(x.flagged for x in others.values())
    small = line(rec, f"cloud_run_rest:{BRAVO}")
    assert (small.measured, small.low, small.status) == (pytest.approx(0.40), 0.0, "ok")


def test_apps_priced_over_the_cloud_run_bill_share_exactly_the_bill() -> None:
    shares, rest = rc.attribute(10.0, {"a": 15.0, "b": 5.0})
    assert shares == {"a": 7.5, "b": 2.5}
    assert rest == 0.0
    shares, rest = rc.attribute(30.0, {"a": 15.0, "b": 5.0})
    assert shares == {"a": 15.0, "b": 5.0}
    assert rest == 10.0
    assert rc.attribute(0.0, {}) == ({}, 0.0)


def test_a_line_off_by_25_percent_is_flagged_with_a_place_for_its_reason() -> None:
    rec = found()
    cloud_run = line(rec, f"service:{PROD}:Cloud Run")
    assert (cloud_run.measured, cloud_run.low) == (50.0, 40.0)
    assert cloud_run.off == pytest.approx(0.25)
    assert cloud_run.flagged
    prod = project(rec, PROD).line
    assert prod.off == pytest.approx(10.0 / 68.67)
    assert not prod.flagged
    assert not line(rec, f"service:{PROD}:Cloud SQL").flagged
    assert not project(rec, STAGING).line.flagged
    page = rc.render(rec)
    assert (
        f"| `service:{PROD}:Cloud Run` | $50.00 | $40.00 | +25.0 % | _reason to be written_ |"
        in page
    )
    reasoned = found({f"service:{PROD}:Cloud Run": "the API kept a second instance warm"})
    assert "| +25.0 % | the API kept a second instance warm |" in rc.render(reasoned)
    flag = next(f for f in as_json(reasoned)["flags"] if f["id"] == f"service:{PROD}:Cloud Run")
    assert flag["reason"] == "the API kept a second instance warm"
    assert flag["off"] == 0.25


def test_just_under_the_tolerance_is_not_flagged() -> None:
    bill = BILL_CSV.replace("ssc-control-prod,Cloud Run,50.00", "ssc-control-prod,Cloud Run,48.00")
    assert not line(found(bill=bill), f"service:{PROD}:Cloud Run").flagged


def test_a_project_with_a_bill_and_no_events() -> None:
    rec = found()
    ghost = project(rec, GHOST)
    assert (ghost.kind, ghost.line.measured, ghost.line.low) == ("cell, no record", 20.0, None)
    assert ghost.line.status == "no_model"
    assert ghost.line.flagged
    bravo = project(rec, BRAVO)
    assert bravo.kind == "cell"
    assert bravo.line.measured == pytest.approx(16.83)
    assert bravo.line.low == pytest.approx(24.25 * 21 / 31)
    assert not bravo.line.flagged
    assert not [a for a in rec.apps if a.project_id == BRAVO]


def test_events_with_no_bill() -> None:
    rec = found()
    charlie = project(rec, CHARLIE)
    assert charlie.line.measured is None
    assert charlie.line.status == "no_bill"
    assert line(rec, f"fixed:{CHARLIE}").status == "no_bill"
    assert app(rec, LONE).attributed_usd is None
    assert app(rec, LONE).app.usage_type == "rare"
    assert line(rec, f"cloud_run_rest:{CHARLIE}").status == "ok"
    staging_missing = found(bill="project_id,service,usd\n")
    assert project(staging_missing, STAGING).line.status == "no_bill"


def test_cost_per_app_and_the_split_against_70_20_10() -> None:
    rec = found()
    billed = 78.67 + 72.46 + 16.83
    assert rec.per_app.measured == pytest.approx(billed / 4)
    assert (rec.per_app.low, rec.per_app.high) == (4.50, 7.10)
    assert rec.per_app.flagged
    assert "4 apps with usage in billed cells" in (rec.per_app.note or "")
    split = {s.id: (s, n) for s, n in rec.split}
    assert {k: (round(s.measured or 0, 2), n) for k, (s, n) in split.items()} == {
        "split:rare": (0.4, 2),
        "split:daily": (0.4, 2),
        "split:heavy": (0.2, 1),
    }
    assert split["split:rare"][0].off == pytest.approx(0.4 / 0.7 - 1)
    assert all(s.flagged for s, _ in split.values())
    assert split["split:rare"][0].note == "small sample (n=5, need 20)"


def test_the_page_holds_every_section_and_the_a6_footer() -> None:
    rec = found()
    page = rc.render(rec)
    for heading in (
        "# SSC cost reconciliation, 2026-10",
        "## 1. Projects",
        "## 2. Fixed cost per cell",
        "## 3. Usage per app",
        "## 4. Not attributed to apps, and platform services",
        "## 5. Cost per app",
        "## 6. Usage split against 70/20/10",
        "## 7. Model arithmetic",
        f"## 8. Flagged lines ({len(rec.flags)})",
    ):
        assert heading in page, heading
    assert page.rstrip().endswith(rc.FOOTER)
    assert "Nothing on this page bills a customer (A6)" in page
    assert f"| {GHOST} | cell, no record | $20.00 | — | — | **no model** |" in page
    assert f"| {CHARLIE} | cell | — |" in page
    assert "**no bill**" in page
    body = as_json(rec)
    assert body["format"] == rc.FORMAT
    assert body["footer"] == rc.FOOTER
    assert [f["id"] for f in body["flags"]] == [f.id for f in rec.flags]
    assert len(page.splitlines()) < 120


def test_flags_are_every_line_more_than_20_percent_off_and_nothing_else() -> None:
    rec = found()
    assert sorted(f.id for f in rec.flags) == sorted(
        [
            f"project:{CHARLIE}",
            f"project:{GHOST}",
            f"fixed:{CHARLIE}",
            f"service:{PROD}:Cloud Run",
            "per_app",
            "split:rare",
            "split:daily",
            "split:heavy",
        ]
    )


def as_json(rec: rc.Reconciliation) -> dict[str, Any]:
    return json.loads(json.dumps(rc.as_json(rec), allow_nan=False))


def test_the_bill_input_is_checked() -> None:
    lines = rc.read_bill(BILL_CSV, MONTH)
    registry = [x for x in lines if x.project_id == ALPHA and x.service == "Artifact Registry"]
    assert [x.usd for x in registry] == [pytest.approx(0.5)]
    for raw, message in (
        ("project_id,usd\nx,1\n", "no column service"),
        ("month,project_id,service,usd\n2026-09,p,Cloud Run,1\n", "is for 2026-09, not 2026-10"),
        ("project_id,service,usd\np,Cloud Run,-2\n", "cost before credits"),
        ("project_id,service,usd\np,Cloud Run,nan\n", "cost before credits"),
        ("project_id,service,usd\np,Cloud Run,lots\n", "is not a number"),
        ("project_id,service,usd\n,Cloud Run,1\n", "project_id and service are required"),
        ("[1]", "bill entry 1 is not an object"),
        ('[{"project_id": "p", "usd": 1}]', "bill entry 1 has no service"),
        ('[{"project_id": "p", "service": "Cloud Run", "usd": -1}]', "cost before credits"),
        ("[nope", "the bill is not JSON"),
    ):
        with pytest.raises(rc.InputError, match=message):
            rc.read_bill(raw, MONTH)


def test_a_json_bill_reads_the_same_as_its_csv() -> None:
    rows = list(csv.DictReader(io.StringIO(BILL_CSV)))
    as_json = json.dumps([{**row, "usd": float(row["usd"])} for row in rows])
    assert rc.read_bill(as_json, MONTH) == rc.read_bill(BILL_CSV, MONTH)


def test_declared_cells_and_reasons_are_checked() -> None:
    cells = rc.read_cells(
        "project_id,resource,created_at\n"
        f"{ALPHA},database,2026-10-16T00:00:00Z\n"
        f"{ALPHA},cell,2026-09-01T00:00:00+00:00\n"
    )
    assert cells == (rc.CellMonth(ALPHA, at(1, 9), (FixedResource("database", at(16)),)),)
    for raw, message in (
        ("project_id,resource\n", "no column created_at"),
        (f"project_id,resource,created_at\n{ALPHA},disk,2026-10-01T00:00:00Z\n", "disk"),
        (f"project_id,resource,created_at\n{ALPHA},cell,2026-10-01\n", "no time zone"),
        ("project_id,resource,created_at\nssc-control-prod,cell,2026-10-01T00:00Z\n", "not a cell"),
        (f"project_id,resource,created_at\n{ALPHA},database,2026-10-01T00:00Z\n", "no cell row"),
    ):
        with pytest.raises(rc.InputError, match=message):
            rc.read_cells(raw)
    assert rc.read_reasons('{"per_app": "ten apps, not 200"}') == {"per_app": "ten apps, not 200"}
    for raw in ("[1]", '{"a": 1}', "not json"):
        with pytest.raises(rc.InputError):
            rc.read_reasons(raw)
    with pytest.raises(rc.InputError, match="not on the page: project:nowhere"):
        found({"project:nowhere": "?"})


def test_the_model_is_one_file_and_every_figure_says_where_it_comes_from() -> None:
    assert MODEL.format == cm.FORMAT
    assert all(p.source for p in MODEL.cell_parts)
    assert all(q.source for p in MODEL.platforms.values() for q in p.parts)
    assert all(MODEL.sources.values())
    assert {r.value: v for r, v in MONTHLY_USD.items()} == {
        p.created_with: p.usd for p in MODEL.cell_parts if p.created_with != cm.CELL_BASE
    }
    assert round(MODEL.hourly("request"), 4) == 0.0909
    assert round(MODEL.hourly("instance"), 4) == 0.0684
    assert MODEL.rates["request"].vcpu_second == 0.000024
    assert MODEL.split == {"rare": 0.70, "daily": 0.20, "heavy": 0.10}
    assert MODEL.target == (4.50, 7.10)
    assert MODEL.platforms[PROD].total == pytest.approx(68.67)


def test_the_model_arithmetic_names_every_stated_figure_its_parts_do_not_reach() -> None:
    short = {c.id: round(c.difference, 2) for c in cm.checks(MODEL) if not MODEL.adds_up(c)}
    assert short == {
        "cell_empty": 1.25,
        "cell_database": 1.25,
        "cell_full": 1.25,
        "scenario_platform_prod_low": -6.33,
        "scenario_platform_prod_high": -6.33,
        "scenario_cells_fixed_low": 35.0,
        "scenario_session_low": -39.62,
        "scenario_session_high": -79.23,
        "scenario_rare_high": 26.93,
    }
    page = rc.render(found())
    assert "- empty cell: stated $23.00, parts and rates give $24.25 (+$1.25)" in page


def test_a_model_file_that_is_not_one_is_refused() -> None:
    with pytest.raises(cm.CostModelError, match="not ssc-cost-model/v1"):
        cm.parse({"format": "v0"})
    with pytest.raises(cm.CostModelError, match="missing or mistypes"):
        cm.parse({"format": cm.FORMAT})


async def _org(dsns: Dsns, created: datetime) -> tuple[str, str]:
    engine = make_engine(dsns.app)
    try:
        org = await create_org(
            engine, NewOrg("Cost", "Ada Admin", "ada@example.com", ISSUER, new_id("usr"))
        )
    finally:
        await engine.dispose()
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute("update ssc.org set created_at = %s where id = %s", (created, org.org_id))
        row = conn.execute("select cell_label from ssc.org where id = %s", (org.org_id,)).fetchone()
    assert row is not None
    return org.org_id, str(row[0])


async def _seed(dsns: Dsns, org: str) -> None:
    engine = make_engine(dsns.app)
    env_r, env_s = "env_" + "r" * 20, "env_" + "s" * 20
    try:
        async with bound_org(engine, org) as conn:

            async def once(kind: MetricKind, key: str, when: datetime, **fields: Any) -> None:
                assert await record_once(
                    conn, org_id=org, kind=kind, dedup_key=key, at=when, **fields
                )

            for i, (env, app_id, billing, seconds, session) in enumerate(
                (
                    (env_r, RARE, "request", 7_800.0, 0.0),
                    (env_s, SESSION, "instance", 3_600.0, 3_600.0),
                    (env_s, SESSION, "instance", 3_600.0, 1_800.0),
                )
            ):
                when = at(20).replace(hour=10 + i)
                await once(
                    MetricKind.USAGE_HOUR,
                    f"{env}:{int(when.timestamp())}",
                    when,
                    app_id=app_id,
                    environment_id=env,
                    properties={
                        "instance_seconds": seconds,
                        "session_seconds": session,
                        "billing": billing,
                    },
                )
            outside = at(1, 11)
            await once(
                MetricKind.USAGE_HOUR,
                f"{env_r}:{int(outside.timestamp())}",
                outside,
                app_id=RARE,
                environment_id=env_r,
                properties={"instance_seconds": 99.0, "session_seconds": 0, "billing": "request"},
            )
            await once(
                MetricKind.COLD_START,
                f"{env_r}:{int(at(20).timestamp())}",
                at(20),
                app_id=RARE,
                environment_id=env_r,
                properties={"count": 7, "duration_ms": 4000.0},
            )
            await once(
                MetricKind.FIXED_RESOURCE,
                "database",
                at(16),
                properties={"resource": "database"},
            )
    finally:
        await engine.dispose()


def test_the_command_reads_the_control_database_and_writes_both_pages(
    dsns: Dsns,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    org, label = asyncio.run(_org(dsns, at(11)))
    asyncio.run(_seed(dsns, org))
    pid = f"ssc-c-{label}"
    bill = tmp_path / "bill.csv"
    bill.write_text(f"project_id,service,usd\n{pid},Networking,20.00\n{pid},Cloud Run,5.00\n")
    declared = tmp_path / "cells.csv"
    declared.write_text(f"project_id,resource,created_at\n{BRAVO},cell,2026-10-11T00:00:00Z\n")
    monkeypatch.setenv(rc.DSN_ENV, dsns.app)
    argv = ["--month", "2026-10", "--bill", str(bill), "--org", org, "--cells", str(declared)]
    assert rc.main([*argv, "--out", str(tmp_path / "out")]) == 0
    written = capsys.readouterr().out.split()
    assert [Path(p).name for p in written] == ["ssc-cost-2026-10.md", "ssc-cost-2026-10.json"]
    body = json.loads(Path(written[1]).read_text())
    cell = next(f for f in body["fixed"] if f["project_id"] == pid)
    assert {p["name"]: p["fraction"] for p in cell["parts"]}["database"] == round(16 / 31, 4)
    assert {p["name"]: p["fraction"] for p in cell["parts"]}["nat"] == round(21 / 31, 4)
    apps = {a["app_id"]: a for a in body["apps"] if a["project_id"] == pid}
    assert apps[RARE]["request_seconds"] == 7_800.0
    assert apps[RARE]["cold_starts"] == 7
    assert apps[RARE]["usage_type"] == "rare"
    assert apps[SESSION]["instance_billed_seconds"] == 7_200.0
    assert apps[SESSION]["session_hours"] == 1.5
    assert apps[SESSION]["usage_type"] == "session"
    rest = next(s for s in body["services"] if s["id"] == f"cloud_run_rest:{pid}")
    assert rest["model_low"] == round(5_400 * MODEL.per_second("request"), 4)
    assert BRAVO in {p["project_id"] for p in body["projects"]}
    assert Path(written[0]).read_text().rstrip().endswith(rc.FOOTER)
    assert rc.main([*argv, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == body


def test_the_command_line_refusals(
    dsns: Dsns,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bill = tmp_path / "bill.csv"
    bill.write_text("project_id,service,usd\n")
    cells = tmp_path / "cells.csv"
    cells.write_text(f"project_id,resource,created_at\n{BRAVO},cell,2026-10-11T00:00:00Z\n")
    monkeypatch.delenv(rc.DSN_ENV, raising=False)
    org = "org_" + "z" * 20
    for argv, message in (
        (["--month", "2026-10", "--bill", str(bill)], "no --cells was given"),
        (["--month", "2026-13", "--bill", str(bill)], "--month"),
        (
            ["--month", "2026-10", "--bill", str(tmp_path / "none.csv"), "--cells", str(cells)],
            "cannot read none.csv",
        ),
        (
            ["--month", "2026-10", "--bill", str(bill), "--cells", str(cells), "--org", org],
            "--org reads the control database",
        ),
    ):
        with pytest.raises(SystemExit):
            rc.main(argv)
        assert message in capsys.readouterr().err
    assert rc.main(["--month", "2026-10", "--bill", str(bill), "--cells", str(cells)]) == 0
    assert "1 declared cells, no control database" in capsys.readouterr().out
    monkeypatch.setenv(rc.DSN_ENV, dsns.app)
    with pytest.raises(SystemExit):
        rc.main(["--month", "2026-10", "--bill", str(bill), "--org", org])
    assert f"org {org} has no cell in 2026-10" in capsys.readouterr().err
