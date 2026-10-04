"""SSC-092: the warm option, against the fake runtime, the fake cell deployer and postgres:18.

Ticket "done when" checks that run without a cloud:
  * on for one environment brings exactly that service to min 1 within one reconciler pass, and
    nothing else changes; off returns it to 0
                                  -> test_one_warm_environment_takes_one_pass_and_nothing_else
  * a deploy of a warm environment keeps it warm
                                  -> test_a_deploy_of_a_warm_environment_keeps_one_instance
  * non-admin and agent sessions are refused
                                  -> test_members_agents_previews_and_a_wrong_cost_are_refused
  * the audit shows who turned it on and the cost shown
                                  -> test_one_warm_environment_takes_one_pass_and_nothing_else,
                                     test_the_gateway_is_set_through_the_cell_deployer
  * the gateway part is the cell stack's warm flag, set by the cell deployer
                                  -> test_the_gateway_is_set_through_the_cell_deployer,
                                     test_a_gateway_change_during_a_run_runs_again,
                                     test_gateway_runs_that_keep_failing_end_failed,
                                     test_no_deployer_fails_the_gateway_at_once
  * suggested where an app is opened most working days and its users keep meeting cold starts
                                  -> test_the_hint_needs_most_working_days_and_cold_starts,
                                     test_the_view_suggests_a_busy_environment_with_cold_starts
The first request to a warm app behind a warm gateway has no "waking up" page only on a live cell.
No MCP tool sets the flag: ``test_mcp.test_no_tool_sets_the_warm_or_a_resource_flag``. The
deployer's ``warm=true`` and ``warm=false`` are in ``infra/tests/test_deployer.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import test_deploy
from httpx2 import Response
from ssc_testkit import SigningKey, assert_problem, auth, mint, new_key
from test_cell_resources import done
from test_deploy import (
    Bench,
    SpyGate,
    build_release,
    deploy,
    execute,
    get,
    rows_of,
    run,
    set_prod_gate,
    start_deploy,
)

from ssc_contracts.cells import WarmGateway
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.cell import create, tasks, warm
from ssc_control.cell.deployer import FakeCellDeployer
from ssc_control.db import bound_org
from ssc_control.metrics.warm_hint import WarmHint, hint_working_days, warm_hints, working_days
from ssc_control.runtime.fake import changed
from ssc_control.runtime.reconciler import reconcile_env
from ssc_control.worker import Ports
from ssc_shared.runtime import service_name

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b


@dataclass
class Warm:
    b: Bench
    fake: FakeCellDeployer
    ports: Ports
    label: str

    async def step(self) -> str:
        """What a worker does: fetch the gateway's job and run one step."""
        done(self.b.dsn, f"cellwarm:{self.b.w.org}")
        return await warm.gateway_step(self.ports, org_id=self.b.w.org)


@pytest.fixture
async def cell(b: Bench) -> Warm:
    """Both environments deployed and healthy on the fake runtime, with a fake cell deployer."""
    fake = FakeCellDeployer()
    set_prod_gate(b, SpyGate("clear"))
    ports = replace(b.ports, cell_deployer=fake, prod_gate=SpyGate("clear"))
    _, state = await deploy(b, b.w.preview, await build_release(b, b.w.preview))
    assert state == "healthy"
    op = start_deploy(b, b.w.prod, await build_release(b, b.w.prod)).json()["operation_id"]
    assert await run(b, op, ports) == "healthy"
    (row,) = rows_of(b.dsn, b.w.org, "select cell_label from ssc.org")
    return Warm(b, fake, ports, str(row["cell_label"]))


def put_warm(
    b: Bench, env_ids: list[str], *, gateway: bool = False, shown: int, token: str | None = None
) -> Response:
    body = {"environment_ids": env_ids, "gateway": gateway, "monthly_usd_shown": shown}
    return b.client.put("/v1/warm", json=body, headers=auth(token or b.t.admin))


def warm_audit(b: Bench) -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select action, actor_kind, actor_id, target_id, before, after "
        "from ssc.audit_event where target_kind = 'warm' order by seq",
    )


def min_instances(b: Bench, env: str) -> int:
    return b.runtime.services[service_name(env)].min_instances


def revisions(b: Bench, env: str) -> tuple[list[str], dict[str, int]]:
    svc = b.runtime.services[service_name(env)]
    return [r.name for r in svc.revisions], dict(svc.traffic)


async def one_pass(b: Bench) -> list[str | None]:
    """One reconciler pass over both environments; the change each made, if any."""
    kinds: list[str | None] = []
    for env in (b.w.prod, b.w.preview):
        outcome = await reconcile_env(
            b.ports.engine, b.runtime, b.ports.release_specs, org_id=b.w.org, env_id=env
        )
        kinds.append(None if outcome.change is None else outcome.change.kind)
    return kinds


async def test_one_warm_environment_takes_one_pass_and_nothing_else(cell: Warm) -> None:
    b = cell.b
    view = get(b, "/v1/warm", b.t.admin)
    assert view.status_code == 200, view.text
    body = view.json()
    assert [(e["environment_id"], e["app_slug"], e["warm"]) for e in body["environments"]] == [
        (b.w.prod, "ledger", False)
    ]
    assert body["gateway"] == {"warm": False, "state": "off", "failure_code": None}
    assert (body["environment_monthly_usd"], body["gateway_monthly_usd"], body["monthly_usd"]) == (
        10,
        10,
        0,
    )
    assert await one_pass(b) == [None, None]
    before = {env: revisions(b, env) for env in (b.w.prod, b.w.preview)}
    b.runtime.reset_calls()

    r = put_warm(b, [b.w.prod], shown=10)
    assert r.status_code == 200, r.text
    assert [e["warm"] for e in r.json()["environments"]] == [True]
    assert r.json()["monthly_usd"] == 10
    assert await one_pass(b) == ["apply", None]
    assert (min_instances(b, b.w.prod), min_instances(b, b.w.preview)) == (1, 0)
    assert changed(b.runtime.calls, service_name(b.w.prod)) == ["apply"]
    assert changed(b.runtime.calls, service_name(b.w.preview)) == []
    assert {env: revisions(b, env) for env in (b.w.prod, b.w.preview)} == before
    assert await one_pass(b) == [None, None]
    assert cell.fake.runs == []

    off = put_warm(b, [], shown=0)
    assert off.status_code == 200, off.text
    assert await one_pass(b) == ["apply", None]
    assert (min_instances(b, b.w.prod), min_instances(b, b.w.preview)) == (0, 0)
    assert {env: revisions(b, env) for env in (b.w.prod, b.w.preview)} == before

    on, back = warm_audit(b)
    assert (on["action"], on["actor_kind"], on["actor_id"], on["target_id"]) == (
        "org.updated",
        "user",
        b.w.admin,
        b.w.org,
    )
    assert on["before"] == {"environment_ids": [], "gateway": False}
    assert on["after"] == {"environment_ids": [b.w.prod], "gateway": False, "monthly_usd_shown": 10}
    assert back["after"] == {"environment_ids": [], "gateway": False, "monthly_usd_shown": 0}
    assert put_warm(b, [], shown=0).status_code == 200
    assert len(warm_audit(b)) == 2


async def test_a_deploy_of_a_warm_environment_keeps_one_instance(cell: Warm) -> None:
    b = cell.b
    assert put_warm(b, [b.w.prod], shown=10).status_code == 200
    op = start_deploy(b, b.w.prod, await build_release(b, b.w.prod)).json()["operation_id"]
    assert await run(b, op, cell.ports) == "healthy"
    assert min_instances(b, b.w.prod) == 1
    _, state = await deploy(b, b.w.preview, await build_release(b, b.w.preview))
    assert state == "healthy"
    assert min_instances(b, b.w.preview) == 0


async def test_members_agents_previews_and_a_wrong_cost_are_refused(
    cell: Warm, signing_key: SigningKey
) -> None:
    b = cell.b
    assert_problem(get(b, "/v1/warm", b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(put_warm(b, [b.w.prod], shown=10, token=b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(put_warm(b, [b.w.prod], shown=10, token=b.t.builder), ErrorCode.FORBIDDEN)
    agent = mint(signing_key, org=b.w.org, sub=b.w.admin, jti=f"cred_{new_key()[:16]}", agent=True)
    assert_problem(put_warm(b, [b.w.prod], shown=10, token=agent), ErrorCode.AGENT_SESSION_REFUSED)
    assert_problem(put_warm(b, [b.w.preview], shown=10), ErrorCode.VALIDATION_FAILED)
    unknown = new_id("env")
    assert_problem(put_warm(b, [unknown], shown=10), ErrorCode.REFERENCE_NOT_FOUND)
    assert_problem(put_warm(b, [b.w.prod], gateway=True, shown=10), ErrorCode.VALIDATION_FAILED)
    assert rows_of(b.dsn, b.w.org, "select id from ssc.environment where warm") == []
    assert rows_of(b.dsn, b.w.org, "select org_id from ssc.warm_gateway where wanted") == []
    assert warm_audit(b) == []
    assert await one_pass(b) == [None, None]


async def test_the_gateway_is_set_through_the_cell_deployer(cell: Warm) -> None:
    b = cell.b
    r = put_warm(b, [b.w.prod], gateway=True, shown=20)
    assert r.status_code == 200, r.text
    assert r.json()["gateway"] == {"warm": True, "state": "turning_on", "failure_code": None}
    assert r.json()["monthly_usd"] == 20
    (job,) = rows_of(
        b.dsn,
        b.w.org,
        "select task_name, lock, args from procrastinate.procrastinate_jobs "
        "where queueing_lock = %s and status = 'todo'",
        f"cellwarm:{b.w.org}",
    )
    assert (job["task_name"], job["lock"], job["args"]) == (
        tasks.WARM_GATEWAY,
        f"cell:{b.w.org}",
        {"org_id": b.w.org},
    )
    (audited,) = warm_audit(b)
    assert audited["after"] == {
        "environment_ids": [b.w.prod],
        "gateway": True,
        "monthly_usd_shown": 20,
    }

    assert await cell.step() == "turning_on"
    assert cell.fake.runs == [(cell.label, WarmGateway.ON)]
    assert await cell.step() == "turning_on"
    cell.fake.finish()
    assert await cell.step() == "on"
    assert get(b, "/v1/warm", b.t.admin).json()["gateway"]["state"] == "on"
    assert await cell.step() == "on"
    assert len(cell.fake.runs) == 1

    assert put_warm(b, [b.w.prod], shown=10).status_code == 200
    assert await cell.step() == "turning_off"
    assert cell.fake.runs[-1] == (cell.label, WarmGateway.OFF)
    cell.fake.finish()
    assert await cell.step() == "off"
    assert get(b, "/v1/warm", b.t.admin).json()["gateway"] == {
        "warm": False,
        "state": "off",
        "failure_code": None,
    }
    assert min_instances(b, b.w.preview) == 0


async def test_a_gateway_change_during_a_run_runs_again(cell: Warm) -> None:
    b = cell.b
    assert put_warm(b, [], gateway=True, shown=10).status_code == 200
    assert await cell.step() == "turning_on"
    assert put_warm(b, [], gateway=False, shown=0).status_code == 200
    cell.fake.finish()
    assert await cell.step() == "turning_off"
    assert await cell.step() == "turning_off"
    assert [flag for _, flag in cell.fake.runs] == [WarmGateway.ON, WarmGateway.OFF]
    cell.fake.finish()
    assert await cell.step() == "off"


async def test_gateway_runs_that_keep_failing_end_failed(cell: Warm) -> None:
    b = cell.b
    assert put_warm(b, [], gateway=True, shown=10).status_code == 200
    cell.fake.fail_next = create.MAX_ATTEMPTS
    for _ in range(create.MAX_ATTEMPTS - 1):
        assert await cell.step() == "turning_on"
        cell.fake.finish()
        assert await cell.step() == "turning_on"
    assert await cell.step() == "turning_on"
    cell.fake.finish()
    assert await cell.step() == "failed"
    assert len(cell.fake.runs) == create.MAX_ATTEMPTS
    gateway = get(b, "/v1/warm", b.t.admin).json()["gateway"]
    assert gateway == {"warm": True, "state": "failed", "failure_code": "CELL_DEPLOYER_FAILED"}
    assert await cell.step() == "failed"

    assert put_warm(b, [], gateway=False, shown=0).status_code == 200
    assert put_warm(b, [], gateway=True, shown=10).status_code == 200
    assert await cell.step() == "turning_on"
    cell.fake.finish()
    assert await cell.step() == "on"


async def test_no_deployer_fails_the_gateway_at_once(cell: Warm) -> None:
    b = cell.b
    cell.ports = replace(cell.ports, cell_deployer=None)
    assert put_warm(b, [], gateway=True, shown=10).status_code == 200
    assert await cell.step() == "failed"
    gateway = get(b, "/v1/warm", b.t.admin).json()["gateway"]
    assert gateway["failure_code"] == "CELL_DEPLOYER_UNAVAILABLE"


@pytest.mark.parametrize(
    ("opened", "cold", "suggested"),
    [(11, 6, True), (20, 10, True), (10, 10, False), (11, 0, False), (11, 5, False)],
)
def test_the_hint_needs_most_working_days_and_cold_starts(
    opened: int, cold: int, suggested: bool
) -> None:
    assert WarmHint(opened, cold, 20).suggested is suggested
    assert working_days(date(2026, 9, 28), date(2026, 10, 5)) == 5
    assert hint_working_days(datetime(2026, 10, 2, 12, tzinfo=UTC)) == 20


def usage(b: Bench, env: str, at: datetime, seconds: float) -> None:
    execute(
        b.dsn,
        b.w.org,
        "insert into ssc.metrics_event (org_id, at, kind, environment_id, properties) "
        "values (%s, %s, 'usage_hour', %s, %s::jsonb)",
        b.w.org,
        at,
        env,
        f'{{"instance_seconds": {seconds}}}',
    )


def cold_start(b: Bench, env: str, at: datetime) -> None:
    execute(
        b.dsn,
        b.w.org,
        "insert into ssc.metrics_event (org_id, at, kind, environment_id, properties) "
        "values (%s, %s, 'cold_start', %s, '{}'::jsonb)",
        b.w.org,
        at,
        env,
    )


async def test_the_hint_counts_working_days_from_the_events(b: Bench) -> None:
    now = datetime(2026, 10, 2, 12, tzinfo=UTC)
    first = datetime(2026, 9, 7, 9, tzinfo=UTC)
    days = [first + timedelta(days=n) for n in range(26)]
    weekdays = [d for d in days if d.weekday() < 5]
    for n, day in enumerate(weekdays[:12]):
        usage(b, b.w.prod, day, 600.0)
        if n % 2 == 0:
            cold_start(b, b.w.prod, day + timedelta(minutes=5))
    for day in days:
        if day.weekday() >= 5:
            usage(b, b.w.preview, day, 600.0)
            cold_start(b, b.w.preview, day)
    usage(b, b.w.preview, weekdays[0], 0.0)
    usage(b, b.w.prod, first - timedelta(days=7), 600.0)
    async with bound_org(b.ports.engine, b.w.org) as conn:
        hints = await warm_hints(conn, b.w.org, now)
    assert hints == {b.w.prod: WarmHint(12, 6, 20), b.w.preview: WarmHint(0, 0, 20)}
    assert hints[b.w.prod].suggested
    assert not hints[b.w.preview].suggested


async def test_the_view_suggests_a_busy_environment_with_cold_starts(cell: Warm) -> None:
    b = cell.b
    today = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), UTC)
    for n in range(1, 28):
        day = today - timedelta(days=n)
        if day.weekday() < 5:
            usage(b, b.w.prod, day + timedelta(hours=9), 900.0)
            cold_start(b, b.w.prod, day + timedelta(hours=9))
    (env,) = get(b, "/v1/warm", b.t.admin).json()["environments"]
    assert (env["suggested"], env["cold_start_days"]) == (True, env["opened_days"])
    assert env["opened_days"] >= 19
    assert put_warm(b, [b.w.prod], shown=10).status_code == 200
    (env,) = get(b, "/v1/warm", b.t.admin).json()["environments"]
    assert (env["warm"], env["suggested"]) == (True, False)
