"""SSC-060: the pilot success sheet (``docs/pilot/success-sheet.md``).

Ticket "done when" (a template that measures the six numbers from SSC-028) ->
test_sheet_sql_matches_the_report and test_sheet_targets_match_report. The dogfood fill is a live
step and the sheet's section for it stays empty (test_dogfood_section_stays_empty).
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
import test_metrics
from sqlalchemy import text
from ssc_testkit import Dsns
from test_metrics import DAY0, DAY60, KEYS, facts, new_org, report_json

from ssc_contracts.ids import new_id
from ssc_control.api.routes.v1.cell import PLACES_TOTAL
from ssc_control.db import bind_org_sync, bound_org, make_engine
from ssc_control.metrics import Metrics
from ssc_control.metrics import report as rep
from ssc_control.ports import MetricKind

SHEET = Path(__file__).resolve().parents[3] / "docs" / "pilot" / "success-sheet.md"
INVITED = 40
BLOCK = re.compile(r"^```sql (\w+)\n(.*?)^```$", re.DOTALL | re.MULTILINE)
PARAM = re.compile(r":'(\w+)'")
pilot40 = test_metrics.pilot40


def sheet() -> str:
    return SHEET.read_text()


def queries() -> dict[str, str]:
    """The sheet's named SQL blocks, with psql's ``:'name'`` turned into a bind."""
    return {name: PARAM.sub(r":\1", sql) for name, sql in BLOCK.findall(sheet())}


def section(heading: str) -> str:
    body = sheet().split(f"\n## {heading}\n", 1)[1]
    return body.split("\n## ", 1)[0]


def table(body: str) -> list[list[str]]:
    lines = [line for line in body.splitlines() if line.startswith("|")]
    return [[c.strip() for c in line.strip("|").split("|")] for line in lines][2:]


async def run_sheet(dsn: str, org: str, names: tuple[str, ...]) -> dict[str, list[Any]]:
    sql = queries()
    params = {"org": org, "day1": DAY0.isoformat(), "asof": DAY60}
    engine = make_engine(dsn)
    try:
        async with bound_org(
            engine.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True),
            org,
        ) as conn:
            return {
                n: [tuple(r) for r in (await conn.execute(text(sql[n]), params)).all()]
                for n in names
            }
    finally:
        await engine.dispose()


def test_sheet_targets_match_report() -> None:
    window = rep.Window(DAY0, DAY0 + timedelta(days=59))
    found = {c.id: c for c in rep.criteria(facts(), window, rep.PilotInputs())}
    rows = table(section("The six numbers"))
    assert [r[1] for r in rows] == list(found)
    for _, id_, _, target, due in rows:
        criterion = found[id_]
        assert int(due) == criterion.due_day
        if criterion.target is None:
            assert target == "yes"
        else:
            assert float(target) == criterion.target
    assert rep.WEEK6 == (35, 42)
    assert rep.DAY60 == 60
    assert "days 36-42" in sheet()


def test_sheet_sql_matches_the_report(
    pilot40: str, dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ("--org", pilot40, "--since", DAY0.isoformat(), "--as-of", DAY60)
    pilot = ("--invited-builders", str(INVITED), "--approved-path", "yes", "--paid", "yes")
    body = report_json(monkeypatch, capsys, dsns.app, *args, *pilot)
    want = {c["id"]: c for c in body["kill_criteria"]}
    got = asyncio.run(
        run_sheet(
            dsns.app,
            pilot40,
            ("active_builders_week6", "apps_per_builder_day60", "apps_using_data_day60")
            + ("tools_side_by_side", "apps_with_database"),
        )
    )

    ((active,),) = got["active_builders_week6"]
    assert want["active_builders_week6"]["observed"] == active / INVITED == 0.5

    ((mean, builders),) = got["apps_per_builder_day60"]
    assert (mean, builders) == (1.5, 40)
    assert (want["apps_per_builder_day60"]["observed"], want["apps_per_builder_day60"]["n"]) == (
        mean,
        builders,
    )

    ((used, deployed),) = got["apps_using_data_day60"]
    assert (used, deployed) == (19, 40)
    assert want["apps_using_data_day60"]["observed"] == used / deployed
    assert want["apps_using_data_day60"]["n"] == deployed

    ((tools, running),) = got["tools_side_by_side"]
    assert (tools, running) == (2, 40)
    assert (want["tools_side_by_side"]["observed"], want["tools_side_by_side"]["n"]) == (2.0, 40)

    assert got["apps_with_database"] == [(8,)]


def test_sheet_counts_places_per_environment(pilot40: str, dsns: Dsns) -> None:
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, pilot40)
        apps = [
            r[0]
            for r in conn.execute(
                "select app_id from ssc.metrics_event where kind = 'database_use' order by app_id"
            )
        ]
        assert len(apps) == 8
        for i, app in enumerate(apps):
            for name in ("prod", "preview") if i == 0 else ("prod",):
                env = new_id("env")
                conn.execute(
                    "insert into ssc.environment (id, org_id, app_id, name) "
                    "values (%s, %s, %s, %s)",
                    (env, pilot40, app, name),
                )
                conn.execute(
                    "insert into ssc.app_database (org_id, environment_id, host, port, "
                    "connection_limit) values (%s, %s, 'db.example', 5432, 2)",
                    (pilot40, env),
                )
    got = asyncio.run(run_sheet(dsns.app, pilot40, ("places_used", "places_by_app")))
    assert got["places_used"] == [(9,)]
    assert got["places_by_app"] == [(apps[0], 2), *((a, 1) for a in sorted(apps[1:]))]
    assert PLACES_TOTAL == 10
    assert "ten places" in sheet()


async def seed_week6_edges(dsn: str) -> str:
    """Four builders, one event each: on the first instant of week 6, one second before it,
    one second before its end, and on its (exclusive) end."""
    org = (await new_org(dsn, "Week 6 edges")).org_id
    start = datetime.combine(DAY0, time(), UTC) + timedelta(days=rep.WEEK6[0])
    end = datetime.combine(DAY0, time(), UTC) + timedelta(days=rep.WEEK6[1])
    second = timedelta(seconds=1)
    engine = make_engine(dsn)
    try:
        async with bound_org(engine, org) as conn:
            for at in (start, start - second, end - second, end):
                await Metrics(KEYS).record_event(
                    conn, org_id=org, kind=MetricKind.SHARE, user_id=new_id("usr"), at=at
                )
    finally:
        await engine.dispose()
    return org


def test_sheet_week6_edges_agree_with_the_report(
    dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    org = asyncio.run(seed_week6_edges(dsns.app))
    args = ("--org", org, "--since", DAY0.isoformat(), "--as-of", DAY60, "--invited-builders", "4")
    body = report_json(monkeypatch, capsys, dsns.app, *args)
    (week6,) = [c for c in body["kill_criteria"] if c["id"] == "active_builders_week6"]
    got = asyncio.run(run_sheet(dsns.app, org, ("active_builders_week6",)))
    assert got["active_builders_week6"] == [(2,)]
    assert week6["observed"] == 2 / 4


def test_dogfood_section_stays_empty() -> None:
    body = section("Filled with dogfood numbers")
    assert "Not filled" in body
    assert all(not any(cell for cell in r[1:]) for r in table(body))
    assert [r[0] for r in table(body)] == [r[1] for r in table(section("The six numbers"))]


def test_the_customer_line_names_the_surprises() -> None:
    body = section("What the customer agrees to")
    for phrase in ("sleep", "several seconds", "SSC-086 T7", "60 minutes"):
        assert phrase in body
    assert "two misses by day 60 means stop" in sheet().lower()
