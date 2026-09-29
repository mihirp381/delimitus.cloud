"""W0: the cross-lane ports' stubs are safe defaults, and the metric kinds match the database."""

import re
from dataclasses import dataclass
from typing import cast

import psycopg
from sqlalchemy.ext.asyncio import AsyncConnection
from ssc_testkit import Dsns

from ssc_contracts.audit import ActorKind
from ssc_contracts.ids import new_id
from ssc_control.audit import Actor
from ssc_control.ports import (
    GateResult,
    MetricKind,
    NullMetricsPort,
    NullSnapshotPort,
    NullTimersPort,
    RefusingProdGate,
)

CONN = cast(AsyncConnection, object())  # the stubs never touch it
ACTOR = Actor(ActorKind.USER, new_id("usr"))


@dataclass(frozen=True)
class Declared:
    name: str = "nightly"
    cron: str = "0 3 * * *"
    timezone: str = "UTC"
    path: str = "/tasks/nightly"
    method: str = "POST"
    timeout_seconds: int = 60


async def test_the_stub_production_gate_refuses() -> None:
    result = await RefusingProdGate().check(
        CONN,
        org_id=new_id("org"),
        app_id=new_id("app"),
        environment_id=new_id("env"),
        release_id=new_id("rel"),
    )
    assert result == GateResult(outcome="refused", approval_ids=(), policy_decision_id=None)


async def test_the_stub_snapshot_port_never_confirms() -> None:
    port = NullSnapshotPort()
    assert await port.request(CONN, new_id("org")) == 0
    assert await port.confirmed(new_id("org"), 0) is False


async def test_the_stub_timers_port_does_nothing() -> None:
    port = NullTimersPort()
    org, app = new_id("org"), new_id("app")
    await port.sync_schedules(
        CONN,
        org_id=org,
        environment_id=new_id("env"),
        declared=[Declared()],
        declared_by_user_id=ACTOR.id,
        actor=ACTOR,
    )
    paused = await port.pause_for_kill(CONN, org_id=org, app_id=app, reason="disable", actor=ACTOR)
    assert paused == []
    await port.resume_after_kill(
        CONN, org_id=org, app_id=app, schedule_ids=paused, reason="disable", actor=ACTOR
    )


async def test_the_stub_metrics_port_does_nothing() -> None:
    await NullMetricsPort().record_event(
        CONN, org_id=new_id("org"), kind=MetricKind.DEPLOY, properties={"n": 1}
    )


def test_metric_kinds_match_the_database(dsns: Dsns) -> None:
    with psycopg.connect(dsns.superuser) as conn:
        checks = conn.execute(
            "select pg_get_constraintdef(oid) from pg_constraint "
            "where conrelid = 'ssc.metrics_event'::regclass and contype = 'c'"
        ).fetchall()
    (kind_check,) = [d for (d,) in checks if d.startswith("CHECK ((kind ")]
    assert set(re.findall(r"'([a-z_]+)'", kind_check)) == {k.value for k in MetricKind}
