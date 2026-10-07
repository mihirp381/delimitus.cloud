"""SSC-028: usage events from the cell, the usage API and the hourly collection. The collector
asks the cell agent (in process, over the Cloud Monitoring emulator with a fake clock) and writes
``usage_hour`` and ``cold_start`` events.

Uses test_deploy's bench. The agent's own leg is conformance's test_cloud_monitoring; the report
is test_metrics; the fixed-resource events are test_cell_resources.

Ticket "done when" checks that run here (the live numbers wait for SSC-030 dogfood data):
  * a session app open an hour gives about one session hour and one instance hour
        -> test_a_session_app_open_an_hour_gives_about_one_session_hour_and_one_instance_hour
  * a cold start appears with its duration -> test_a_cold_start_appears_with_its_duration
Plus: collecting again writes nothing twice, one read is three Monitoring calls and never reaches
an app, request-billed apps have no session hours, no source means no events and a log line, the
job's cadence, the API's refusals and the usage-type thresholds.
"""

from __future__ import annotations

import importlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import psycopg
import pytest
import test_deploy
from sqlalchemy import make_url
from ssc_testkit import Dsns, assert_problem, with_cell
from test_deploy import (
    AGENT,
    Bench,
    World,
    agent_token,
    build_release,
    deploy,
    get,
    manifest_of,
    rows_of,
)

from ssc_agent.app import create_app as create_agent
from ssc_agent.cloud_monitoring import CellUsageReader, CloudMonitoringSeries
from ssc_conformance.cloud_monitoring_emulator import CloudMonitoringEmulator
from ssc_conformance.cloud_run_emulator import PROJECT
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.db import MIGRATE_ROLE, bound_org, downgrade, upgrade
from ssc_control.metrics import collect, jobs, record_once
from ssc_control.metrics.collect import LAG, collect_all, collect_org, next_window
from ssc_control.metrics.usage import HEAVY_MIN_SECONDS, RARE_MAX_SECONDS, usage_type
from ssc_control.ports import MetricKind
from ssc_control.runtime.cell_usage import AgentCellUsage
from ssc_control.runtime.cells import StaticCells
from ssc_control.worker import build_app, cells_of
from ssc_control.worker_ports import PORTS_KEY
from ssc_shared.runtime import service_name
from ssc_shared.usage import UsageWindow

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b

DAY = datetime(2026, 9, 14, tzinfo=UTC)
MONTH = "2026-09"
SESSION = manifest_of(runtime={"sessions": True})
USAGE_KINDS = "('usage_hour', 'cold_start')"


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute, second=second)


async def _token() -> str:
    return "access-token"


@dataclass
class Clock:
    now: datetime


@dataclass
class Cell:
    clock: Clock
    monitoring: CloudMonitoringEmulator
    usage: AgentCellUsage
    hosts: list[str]


def _cell(org_id: str, source: bool = True) -> Cell:
    clock = Clock(at(0))
    monitoring = CloudMonitoringEmulator(now=lambda: clock.now)
    hosts: list[str] = []

    def route(request: httpx2.Request) -> httpx2.Response:
        hosts.append(request.url.host)
        return monitoring.handler(request)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(route))
    reader = CellUsageReader(
        CloudMonitoringSeries(PROJECT, _token, client=client) if source else None
    )
    agent = create_agent(cast("Any", None), usage=reader, org_id=org_id)
    transport = httpx2.ASGITransport(app=agent)
    usage = AgentCellUsage(
        AGENT, agent_token, org_id=org_id, client=httpx2.AsyncClient(transport=transport)
    )
    return Cell(clock, monitoring, usage, hosts)


@pytest.fixture
async def cell(world: World) -> AsyncIterator[Cell]:
    made = _cell(world.org)
    yield made
    await made.usage.aclose()


async def _collect(b: Bench, cell: Cell, now: datetime) -> collect.Collected:
    cell.clock.now = now
    return await collect_org(b.ports.engine, cell.usage, b.w.org, now)


def _events(b: Bench, kinds: str = USAGE_KINDS) -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select kind, app_id, environment_id, dedup_key, properties, at, pseudonym "
        f"from ssc.metrics_event where kind in {kinds} order by at, kind",
    )


def _cursor(b: Bench) -> datetime | None:
    rows = rows_of(b.dsn, b.w.org, "select collected_until from ssc.usage_collection")
    return rows[0]["collected_until"] if rows else None


def _usage(b: Bench, env: str, token: str | None = None) -> httpx2.Response:
    return cast(
        "httpx2.Response",
        get(b, f"/v1/apps/{b.w.app}/environments/{env}/usage?month={MONTH}", token),
    )


async def _session_open_an_hour(b: Bench, cell: Cell) -> str:
    """A session app opened at 10:00, held open an hour, left to idle out 15 minutes later."""
    release = await build_release(b, b.w.preview, SESSION)
    assert (await deploy(b, b.w.preview, release))[1] == "healthy"
    svc = service_name(b.w.preview)
    cell.monitoring.instance(svc, started=at(10), stopped=at(11, 15, 22), startup_ms=21_500)
    cell.monitoring.busy(svc, start=at(10, 0, 22), end=at(11, 0, 22))
    return svc


async def test_a_session_app_open_an_hour_gives_about_one_session_hour_and_one_instance_hour(
    b: Bench, cell: Cell
) -> None:
    await _session_open_an_hour(b, cell)
    first = await _collect(b, cell, at(11, 20))
    assert first.window == UsageWindow(start=at(10), end=at(11))
    second = await _collect(b, cell, at(12, 20))
    assert second.window == UsageWindow(start=at(11), end=at(12))
    hours = [e for e in _events(b) if e["kind"] == "usage_hour"]
    assert [(e["at"], e["properties"]) for e in hours] == [
        (at(10), {"instance_seconds": 3600.0, "session_seconds": 3600, "billing": "instance"}),
        (at(11), {"instance_seconds": 922.0, "session_seconds": 60, "billing": "instance"}),
    ]
    assert {(e["app_id"], e["environment_id"], e["pseudonym"]) for e in hours} == {
        (b.w.app, b.w.preview, None)
    }
    r = _usage(b, b.w.preview)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_hours"] == pytest.approx(1.0, abs=0.05)
    assert 1.0 <= body["instance_hours"] <= 1.3
    assert (body["session_hours"], body["instance_hours"]) == (1.02, 1.26)
    assert (body["usage_type"], body["month"], body["active_days"]) == ("session", MONTH, 1)
    assert body["billing"] == "instance"


async def test_a_cold_start_appears_with_its_duration(b: Bench, cell: Cell) -> None:
    await _session_open_an_hour(b, cell)
    await _collect(b, cell, at(11, 20))
    (start,) = [e for e in _events(b) if e["kind"] == "cold_start"]
    assert (start["at"], start["environment_id"]) == (at(10), b.w.preview)
    assert start["properties"] == {"count": 1, "duration_ms": 21500.0}
    body = _usage(b, b.w.preview).json()
    assert body["cold_starts"] == 1
    assert body["small_sample"] is True
    assert (body["cold_start_p50_seconds"], body["cold_start_p95_seconds"]) == (None, None)


async def test_collecting_again_writes_nothing_twice(b: Bench, cell: Cell, dsns: Dsns) -> None:
    await _session_open_an_hour(b, cell)
    assert (await _collect(b, cell, at(11, 20))).written == 2
    before = _events(b)
    with psycopg.connect(dsns.superuser) as conn:
        conn.execute("delete from ssc.usage_collection where org_id = %s", (b.w.org,))
    again = await _collect(b, cell, at(11, 20))
    assert (again.window, again.written) == (UsageWindow(start=at(10), end=at(11)), 0)
    assert _events(b) == before
    assert (await _collect(b, cell, at(11, 40))).window is None
    assert _cursor(b) == at(11)


async def test_one_read_is_three_monitoring_calls_and_never_reaches_an_app(
    b: Bench, cell: Cell
) -> None:
    await _session_open_an_hour(b, cell)
    driver_calls = list(b.runtime.calls)
    await _collect(b, cell, at(11, 20))
    assert len(cell.monitoring.calls) == 3
    assert set(cell.hosts) == {"monitoring.googleapis.com"}
    for call in cell.monitoring.calls:
        assert 'service_name = starts_with("ssc-a-")' in call["filter"]
        assert (call["interval.startTime"], call["interval.endTime"]) == (
            "2026-09-14T10:00:00Z",
            "2026-09-14T11:00:00Z",
        )
    assert b.runtime.calls == driver_calls


async def test_a_long_gap_is_read_six_hours_at_a_time(b: Bench, cell: Cell) -> None:
    await _session_open_an_hour(b, cell)
    await _collect(b, cell, at(9, 20))
    late = await _collect(b, cell, at(20, 20))
    assert late.window == UsageWindow(start=at(9), end=at(15))
    rest = await _collect(b, cell, at(20, 21))
    assert rest.window == UsageWindow(start=at(15), end=at(20))
    assert len(cell.monitoring.calls) == 9
    assert len([e for e in _events(b) if e["kind"] == "usage_hour"]) == 2


async def test_a_request_billed_app_has_instance_hours_and_no_session_hours(
    b: Bench, cell: Cell
) -> None:
    release = await build_release(b, b.w.preview)
    assert (await deploy(b, b.w.preview, release))[1] == "healthy"
    svc = service_name(b.w.preview)
    cell.monitoring.instance(
        svc, started=at(10), stopped=at(10, 15), startup_ms=2000, billing="request"
    )
    cell.monitoring.busy(svc, start=at(10, 0, 2), end=at(10, 0, 12))
    await _collect(b, cell, at(11, 20))
    (hour,) = [e for e in _events(b) if e["kind"] == "usage_hour"]
    assert hour["properties"] == {
        "instance_seconds": 12.0,
        "session_seconds": 0,
        "billing": "request",
    }
    body = _usage(b, b.w.preview).json()
    assert (body["usage_type"], body["session_hours"], body["billing"]) == ("rare", 0.0, "request")


async def test_billing_is_the_latest_hours_and_hours_recorded_before_it_count_as_request(
    b: Bench,
) -> None:
    """SSC-057 shows request-billed or instance-billed per app (SSC-090)."""
    hours = (
        (at(9), {"instance_seconds": 60.0, "session_seconds": 0}),
        (at(10), {"instance_seconds": 60.0, "session_seconds": 0}),
    )
    async with bound_org(b.ports.engine, b.w.org) as conn:
        for moment, properties in hours:
            await record_once(
                conn,
                org_id=b.w.org,
                kind=MetricKind.USAGE_HOUR,
                dedup_key=f"{b.w.prod}:{moment.hour}",
                app_id=b.w.app,
                environment_id=b.w.prod,
                properties=properties,
                at=moment,
            )
    assert _usage(b, b.w.prod).json()["billing"] == "request"
    async with bound_org(b.ports.engine, b.w.org) as conn:
        await record_once(
            conn,
            org_id=b.w.org,
            kind=MetricKind.USAGE_HOUR,
            dedup_key=f"{b.w.prod}:11",
            app_id=b.w.app,
            environment_id=b.w.prod,
            properties={"instance_seconds": 60.0, "session_seconds": 60, "billing": "instance"},
            at=at(11),
        )
    assert _usage(b, b.w.prod).json()["billing"] == "instance"
    cell_usage = get(b, f"/v1/usage?month={MONTH}", b.t.admin).json()
    assert [(e["environment_id"], e["billing"]) for e in cell_usage["environments"]] == [
        (b.w.prod, "instance")
    ]


async def test_without_a_usage_source_nothing_is_written_and_the_log_says_why(
    b: Bench, caplog: pytest.LogCaptureFixture
) -> None:
    for refuse in (None, 403):
        cell = _cell(b.w.org, source=refuse is not None)
        cell.monitoring.refuse = refuse
        await _session_open_an_hour(b, cell)
        with caplog.at_level(logging.WARNING, logger=collect.__name__):
            done = await _collect(b, cell, at(11, 20))
        await cell.usage.aclose()
        assert (done.written, done.skipped) == (0, "not_configured")
        assert "usage events skipped" in caplog.text
        assert _events(b) == []
        assert _cursor(b) is None
        caplog.clear()


async def test_a_failed_read_is_retried_by_the_next_run(
    b: Bench, cell: Cell, caplog: pytest.LogCaptureFixture
) -> None:
    await _session_open_an_hour(b, cell)
    assert (await _collect(b, cell, at(10, 20))).written == 0
    cell.monitoring.refuse = 500
    with caplog.at_level(logging.WARNING, logger=collect.__name__):
        failed = await _collect(b, cell, at(11, 20))
    assert failed.skipped == "error"
    assert "the next run tries again" in caplog.text
    assert _cursor(b) == at(10)
    cell.monitoring.refuse = None
    assert (await _collect(b, cell, at(12, 20))).window == UsageWindow(start=at(10), end=at(12))
    assert len(_events(b)) == 3


async def test_an_org_with_no_environments_moves_on_without_a_call(b: Bench, cell: Cell) -> None:
    test_deploy.execute(b.dsn, b.w.org, "delete from ssc.app_grant")
    test_deploy.execute(b.dsn, b.w.org, "delete from ssc.environment")
    done = await _collect(b, cell, at(11, 20))
    assert (done.window, done.written) == (UsageWindow(start=at(10), end=at(11)), 0)
    assert cell.monitoring.calls == []
    assert _cursor(b) == at(11)


async def test_the_job_collects_every_org_and_logs_when_there_is_no_source(
    b: Bench, cell: Cell, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def these_orgs(engine: object) -> list[str]:
        """Only this test's org: the database is shared with other tests."""
        return [b.w.org]

    monkeypatch.setattr(collect, "all_org_ids", these_orgs)
    await _session_open_an_hour(b, cell)
    task = build_app(b.dsn).tasks[jobs.COLLECT_TASK]
    none = SimpleNamespace(additional_context={PORTS_KEY: b.ports})
    with caplog.at_level(logging.WARNING, logger=jobs.__name__):
        assert await task.func(none, timestamp=0) == 0
    assert "no cell usage source" in caplog.text
    cell.clock.now = at(11, 20)
    ports = replace(with_cell(b.ports, usage=cell.usage), clock=lambda: at(11, 20))
    some = SimpleNamespace(additional_context={PORTS_KEY: ports})
    assert await task.func(some, timestamp=0) == 2
    assert ports.cells is not None
    assert await collect_all(ports.engine, ports.cells, at(11, 20)) == 0


def test_the_worker_collects_usage_at_twenty_past_every_hour() -> None:
    app = build_app("postgresql://ssc_app@localhost/ssc")
    ((periodic,),) = [
        [
            p
            for key, p in app.periodic_registry.periodic_tasks.items()
            if key[0] == jobs.COLLECT_TASK
        ]
    ]
    first = periodic.croniter.get_next(datetime, start_time=at(10, 21))
    second = periodic.croniter.get_next(datetime, start_time=first)
    assert (first.astimezone(UTC), second - first) == (at(11, 20), timedelta(hours=1))
    assert app.tasks[jobs.COLLECT_TASK].lock == "usage_collect"


def test_the_worker_reads_usage_through_the_cell_agent_only() -> None:
    engine = cast("Any", None)
    assert cells_of({}, engine) is None
    fake = cells_of({"SSC_RUNTIME_DRIVER": "fake"}, engine)
    assert isinstance(fake, StaticCells)
    assert fake.cell is not None
    assert fake.cell.usage is None


def test_an_hour_is_read_once_it_is_lag_old_and_at_most_six_hours_at_a_time() -> None:
    assert LAG == timedelta(minutes=15)
    assert next_window(None, at(11, 14)) == UsageWindow(start=at(9), end=at(10))
    assert next_window(None, at(11, 15)) == UsageWindow(start=at(10), end=at(11))
    assert next_window(at(11), at(11, 59)) is None
    assert next_window(at(2), at(20)) == UsageWindow(start=at(2), end=at(8))


def test_the_usage_type_thresholds() -> None:
    assert (RARE_MAX_SECONDS, HEAVY_MIN_SECONDS) == (19_500.0, 262_800.0)
    assert usage_type(0, 0) is None
    assert usage_type(0, 13.0) == "rare"
    assert usage_type(0, RARE_MAX_SECONDS) == "rare"
    assert usage_type(0, RARE_MAX_SECONDS + 1) == "daily"
    assert usage_type(0, HEAVY_MIN_SECONDS - 1) == "daily"
    assert usage_type(0, HEAVY_MIN_SECONDS) == "heavy"
    assert usage_type(1, HEAVY_MIN_SECONDS * 2) == "session"


async def test_an_environment_with_no_usage_reads_zero_and_an_unknown_one_is_not_found(
    b: Bench,
) -> None:
    body = _usage(b, b.w.prod).json()
    assert body == {
        "environment_id": b.w.prod,
        "app_id": b.w.app,
        "month": MONTH,
        "usage_type": None,
        "session_hours": 0.0,
        "instance_hours": 0.0,
        "cold_starts": 0,
        "cold_start_p50_seconds": None,
        "cold_start_p95_seconds": None,
        "small_sample": True,
        "active_days": 0,
        "billing": None,
    }
    assert_problem(_usage(b, new_id("env")), ErrorCode.NOT_FOUND)
    r = get(b, f"/v1/apps/{b.w.app}/environments/{b.w.prod}/usage?month=2026-13")
    assert_problem(r, ErrorCode.VALIDATION_FAILED)


async def test_only_admins_read_the_cells_usage_and_fixed_resources(b: Bench, cell: Cell) -> None:
    await _session_open_an_hour(b, cell)
    await _collect(b, cell, at(11, 20))
    async with bound_org(b.ports.engine, b.w.org) as conn:
        for minute, resource in enumerate(("database", "egress", "database"), start=21):
            await record_once(
                conn,
                org_id=b.w.org,
                kind=MetricKind.FIXED_RESOURCE,
                dedup_key=resource,
                properties={"resource": resource},
                at=at(11, minute),
            )
    assert_problem(get(b, f"/v1/usage?month={MONTH}", b.t.member), ErrorCode.FORBIDDEN)
    r = get(b, f"/v1/usage?month={MONTH}", b.t.admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["month"] == MONTH
    assert [(e["environment_id"], e["usage_type"]) for e in body["environments"]] == [
        (b.w.preview, "session")
    ]
    assert [f["resource"] for f in body["fixed_resources"]] == ["database", "egress"]


def test_0023_downgrades_and_upgrades(dsns: Dsns) -> None:
    rev = importlib.import_module("ssc_control.db.migrations.versions.0023_usage_metrics")
    name = f"m{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database {name} owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database=name).render_as_string(hide_password=False)
    shape = (
        "select (select count(*) from information_schema.columns where table_schema = 'ssc' "
        "and table_name = 'metrics_event' and column_name in ('environment_id', 'dedup_key')), "
        "to_regclass('ssc.usage_collection') is not null, "
        "to_regclass('ssc.metrics_event_once') is not null"
    )

    def present() -> tuple[int, bool, bool]:
        with psycopg.connect(dsn) as conn:
            row = conn.execute(shape).fetchone()
            assert row is not None
            return (row[0], row[1], row[2])

    upgrade(dsn)
    assert present() == (2, True, True)
    downgrade(dsn, rev.down_revision)
    assert present() == (0, False, False)
    upgrade(dsn)
    assert present() == (2, True, True)


def test_a_dedup_key_is_written_once_per_org_and_kind(b: Bench) -> None:
    insert = (
        "insert into ssc.metrics_event (org_id, kind, dedup_key, environment_id, properties) "
        "values (%s, 'usage_hour', %s, %s, '{}'::jsonb)"
    )
    key = f"{b.w.preview}:1789380000"
    test_deploy.execute(b.dsn, b.w.org, insert, b.w.org, key, b.w.preview)
    with pytest.raises(psycopg.errors.UniqueViolation):
        test_deploy.execute(b.dsn, b.w.org, insert, b.w.org, key, b.w.preview)
    for bad_key, env in (("Has Spaces", b.w.preview), ("ok", "env_short")):
        with pytest.raises(psycopg.errors.CheckViolation):
            test_deploy.execute(b.dsn, b.w.org, insert, b.w.org, bad_key, env)
    with pytest.raises(psycopg.errors.CheckViolation):
        test_deploy.execute(
            b.dsn,
            b.w.org,
            "insert into ssc.usage_collection (org_id, collected_until) values (%s, %s)",
            b.w.org,
            at(10, 30),
        )
