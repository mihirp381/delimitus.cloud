"""SSC-087: lazy cell resources, against the fake cell deployer, the fake runtime and postgres:18.

Ticket "done when" checks that run without a cloud:
  * a stateful deploy into an empty cell gets its database with no human step, waits, then goes
    live                                    -> test_a_stateful_deploy_into_an_empty_cell_...
  * two such deploys started together create one instance
                                            -> test_two_stateful_deploys_started_together_...
  * a job killed halfway converges on the next run
                                            -> test_a_deployer_run_killed_halfway_is_run_again,
                                               test_a_step_killed_before_recording_its_run_...
  * egress and connections triggers         -> test_an_approved_internet_host_turns_on_egress,
                                               test_an_approved_data_source_turns_on_connections
  * set by an admin through the API, audited -> test_an_admin_turns_a_resource_on_and_it_is_audited
  * nothing turns a resource off             -> test_nothing_turns_a_ready_resource_off
  * the worker passes only a label and a resource
                                            -> test_the_job_is_started_with_exactly_two_arguments
SSC-028: creating a cell's database writes exactly one fixed-resource event
                                            -> test_creating_the_cells_database_writes_exactly_...
The deployer's own refusals and its single flag are in ``infra/tests/test_deployer.py``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx2
import psycopg
import pytest
import test_deploy
from fastapi.routing import APIRoute
from ssc_testkit import SigningKey, assert_problem, auth, mint, new_key
from test_deploy import (
    Bench,
    SpyGate,
    build_release,
    get,
    manifest_of,
    operation,
    pointer,
    post,
    rows_of,
    run,
    set_prod_gate,
    start_deploy,
)

from ssc_contracts.audit import ActorKind
from ssc_contracts.cells import MONTHLY_USD, NOTICE, CellResource
from ssc_contracts.errors import ErrorCode
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.api.routes.v1 import cell as cell_api
from ssc_control.approvals import service
from ssc_control.audit import Actor
from ssc_control.cell import create, tasks
from ssc_control.cell.deployer import (
    DEPLOYER_ENV,
    DEPLOYER_JOB_ENV,
    CellDeployerError,
    CloudRunCellDeployer,
    FakeCellDeployer,
    cell_deployer_from_env,
    deployer_args,
    execution_status,
)
from ssc_control.cell.resources import CELL_RESOURCE_FAILED
from ssc_control.db import bound_org
from ssc_control.domain.approval_rules import Requirement, RequirementKind
from ssc_control.metrics import record_once
from ssc_control.ports import MetricKind
from ssc_control.runtime.app_databases import FakeAppDatabases
from ssc_control.worker import CompositionError, Ports, build_app, compose_ports

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b

STATEFUL = {"state": {"postgres": True}}
DB = CellResource.DATABASE
JOB = "projects/ssc-platform-0/locations/us-central1/jobs/ssc-cell-deployer"


@dataclass
class Cell:
    b: Bench
    fake: FakeCellDeployer
    ports: Ports
    label: str

    async def step(self, resource: CellResource = DB) -> str:
        """What a worker does: fetch the resource's job and run one step."""
        done(self.b.dsn, create_lock(self.b, resource))
        return await create.create_step(self.ports, org_id=self.b.w.org, resource=resource)


@pytest.fixture
def cell(b: Bench) -> Cell:
    fake = FakeCellDeployer()
    set_prod_gate(b, SpyGate("clear"))
    ports = replace(
        b.ports, cell_deployer=fake, prod_gate=SpyGate("clear"), app_databases=FakeAppDatabases()
    )
    (row,) = rows_of(b.dsn, b.w.org, "select cell_label from ssc.org")
    return Cell(b, fake, ports, str(row["cell_label"]))


def done(dsn: str, queueing_lock: str) -> None:
    """A worker fetched the waiting job and ran it to the end."""
    with psycopg.connect(dsn) as conn:
        conn.execute("set search_path to procrastinate")
        for status in ("doing", "succeeded"):
            conn.execute(
                "update procrastinate_jobs set status = %s "
                "where queueing_lock = %s and status = %s",
                (status, queueing_lock, "todo" if status == "doing" else "doing"),
            )


def resources_of(b: Bench) -> dict[str, dict[str, Any]]:
    rows = rows_of(b.dsn, b.w.org, "select * from ssc.cell_resource")
    return {str(r["resource"]): r for r in rows}


def waiters(b: Bench) -> list[str]:
    rows = rows_of(
        b.dsn, b.w.org, "select deployment_id from ssc.cell_resource_waiter order by created_at"
    )
    return [str(r["deployment_id"]) for r in rows]


def jobs(b: Bench, queueing_lock: str, status: str = "todo") -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select task_name, lock, args from procrastinate.procrastinate_jobs "
        "where queueing_lock = %s and status = %s",
        queueing_lock,
        status,
    )


def cell_audit(b: Bench) -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select action, actor_kind, actor_id, target_id, before, after "
        "from ssc.audit_event where target_kind = 'cell_resource' order by seq",
    )


def create_lock(b: Bench, resource: CellResource = DB) -> str:
    return f"cellres:{b.w.org}:{resource.value}"


async def stateful_deploy(cell: Cell, env: str) -> str:
    b = cell.b
    token = b.t.admin if env == b.w.prod else None
    release = await build_release(b, env, manifest_of(**STATEFUL), token)
    r = start_deploy(b, env, release, token=token)
    assert r.status_code == 202, r.text
    op = str(r.json()["operation_id"])
    done(b.dsn, f"dep:{op}")
    return op


async def finish_creation(cell: Cell) -> None:
    """Start the run, see it running, end it, see it ready."""
    assert await cell.step() == "creating"
    assert await cell.step() == "creating"
    cell.fake.finish()
    assert await cell.step() == "ready"


# ── the deploy trigger ───────────────────────────────────────────────────────


async def test_a_stateful_deploy_into_an_empty_cell_creates_the_database_and_continues(
    cell: Cell,
) -> None:
    b = cell.b
    op = await stateful_deploy(cell, b.w.preview)
    assert await run(b, op, cell.ports) == "running"
    assert b.runtime.calls == []
    out = operation(b, op)
    assert (out["state"], out["notice"]) == ("running", NOTICE[DB])
    assert out["notice"].startswith("Creating your company's database, about ten minutes")
    assert resources_of(b)["database"]["state"] == "requested"
    assert waiters(b) == [op]
    (job,) = jobs(b, create_lock(b))
    assert (job["task_name"], job["lock"]) == (tasks.CREATE_RESOURCE, f"cell:{b.w.org}")
    assert job["args"] == {"org_id": b.w.org, "resource": "database"}
    (asked,) = cell_audit(b)
    assert (asked["action"], asked["actor_id"], asked["target_id"]) == (
        "cell.resource_requested",
        b.w.builder,
        "database",
    )
    assert asked["after"] == {"state": "requested", "cause": "deploy", "deployment_id": op}

    # A rerun of the waiting deployment changes nothing and asks for nothing new.
    assert await run(b, op, cell.ports) == "running"
    assert len(cell_audit(b)) == 1

    await finish_creation(cell)
    assert cell.fake.runs == [(cell.label, DB)]
    row = resources_of(b)["database"]
    assert (row["state"], row["attempts"]) == ("ready", 1)
    assert row["execution"].startswith(f"{JOB}/executions/")
    assert row["ready_at"] is not None
    assert [a["action"] for a in cell_audit(b)] == [
        "cell.resource_requested",
        "cell.resource_ready",
    ]
    assert cell_audit(b)[-1]["actor_id"] == b.w.builder
    (woken,) = jobs(b, f"dep:{op}")
    assert woken["args"] == {"org_id": b.w.org, "deployment_id": op}
    assert woken["lock"] == f"env:{b.w.preview}"

    assert await run(b, op, cell.ports) == "healthy"
    assert pointer(b, b.w.preview) == op
    assert operation(b, op)["notice"] is None
    assert waiters(b) == []

    # The next stateful deploy finds the database ready and does not wait.
    again = await stateful_deploy(cell, b.w.preview)
    assert await run(b, again, cell.ports) == "healthy"
    assert cell.fake.runs == [(cell.label, DB)]


async def test_a_stateless_deploy_asks_for_nothing(cell: Cell) -> None:
    b = cell.b
    release = await build_release(b, b.w.preview)
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    assert await run(b, op, cell.ports) == "healthy"
    assert resources_of(b) == {}
    assert operation(b, op)["notice"] is None


async def test_the_accepted_deploy_says_what_it_sets_off_until_the_database_is_ready(
    cell: Cell,
) -> None:
    b = cell.b
    stateful = await build_release(b, b.w.preview, manifest_of(**STATEFUL))
    stateless = await build_release(b, b.w.preview)
    first = start_deploy(b, b.w.preview, stateful)
    assert first.status_code == 202, first.text
    assert first.json()["notice"] == NOTICE[DB]
    op = str(first.json()["operation_id"])
    done(b.dsn, f"dep:{op}")
    assert await run(b, op, cell.ports) == "running"
    await finish_creation(cell)
    assert await run(b, op, cell.ports) == "healthy"
    again = start_deploy(b, b.w.preview, stateful)
    assert again.status_code == 202, again.text
    assert again.json()["notice"] is None
    assert await run(b, again.json()["operation_id"], cell.ports) == "healthy"
    plain = start_deploy(b, b.w.preview, stateless)
    assert plain.json()["notice"] is None


async def test_two_stateful_deploys_started_together_create_one_database(cell: Cell) -> None:
    b = cell.b
    first = await stateful_deploy(cell, b.w.preview)
    second = await stateful_deploy(cell, b.w.prod)
    states = await asyncio.gather(run(b, first, cell.ports), run(b, second, cell.ports))
    assert states == ["running", "running"]
    assert len(resources_of(b)) == 1
    assert len(jobs(b, create_lock(b))) == 1
    assert [a["action"] for a in cell_audit(b)] == ["cell.resource_requested"]
    assert sorted(waiters(b)) == sorted([first, second])

    await finish_creation(cell)
    assert cell.fake.runs == [(cell.label, DB)]
    assert len(jobs(b, f"dep:{first}")) == len(jobs(b, f"dep:{second}")) == 1
    assert await run(b, first, cell.ports) == "healthy"
    assert await run(b, second, cell.ports) == "healthy"
    assert waiters(b) == []


async def test_a_second_request_while_one_is_in_flight_joins_it(cell: Cell) -> None:
    b = cell.b
    first = await stateful_deploy(cell, b.w.preview)
    assert await run(b, first, cell.ports) == "running"
    assert await cell.step() == "creating"
    second = await stateful_deploy(cell, b.w.prod)
    assert await run(b, second, cell.ports) == "running"
    assert len(cell.fake.runs) == 1
    assert [a["action"] for a in cell_audit(b)] == ["cell.resource_requested"]
    cell.fake.finish()
    assert await cell.step() == "ready"
    assert len(cell.fake.runs) == 1


# ── killed halfway ───────────────────────────────────────────────────────────


async def test_a_deployer_run_killed_halfway_is_run_again(cell: Cell) -> None:
    b = cell.b
    op = await stateful_deploy(cell, b.w.preview)
    assert await run(b, op, cell.ports) == "running"
    assert await cell.step() == "creating"
    cell.fake.fail_next = 1
    cell.fake.finish()
    assert await cell.step() == "creating"
    row = resources_of(b)["database"]
    assert (row["state"], row["attempts"], row["execution"]) == ("creating", 1, None)
    assert row["last_error"] == "the deployer run failed"
    (retry,) = jobs(b, create_lock(b))
    assert retry["task_name"] == tasks.CREATE_RESOURCE
    assert await cell.step() == "creating"
    assert len(cell.fake.runs) == 2
    cell.fake.finish()
    assert await cell.step() == "ready"
    assert resources_of(b)["database"]["attempts"] == 2
    assert await run(b, op, cell.ports) == "healthy"


async def test_a_step_killed_before_recording_its_run_converges(cell: Cell) -> None:
    b = cell.b

    class Killed(FakeCellDeployer):
        async def start(self, label: str, resource: CellResource) -> str:
            await super().start(label, resource)
            raise asyncio.CancelledError

    killed = Killed()
    op = await stateful_deploy(cell, b.w.preview)
    assert await run(b, op, cell.ports) == "running"
    with pytest.raises(asyncio.CancelledError):
        await create.create_step(
            replace(cell.ports, cell_deployer=killed), org_id=b.w.org, resource=DB
        )
    row = resources_of(b)["database"]
    assert (row["state"], row["attempts"], row["execution"]) == ("creating", 1, None)
    assert await cell.step() == "creating"
    assert resources_of(b)["database"]["attempts"] == 2
    cell.fake.finish()
    assert await cell.step() == "ready"
    assert await cell.step() == "ready"
    assert len(cell.fake.runs) == 1
    assert [a["action"] for a in cell_audit(b)] == [
        "cell.resource_requested",
        "cell.resource_ready",
    ]


def fixed_resource_events(b: Bench) -> list[dict[str, Any]]:
    return rows_of(
        b.dsn,
        b.w.org,
        "select dedup_key, properties, app_id, environment_id, pseudonym "
        "from ssc.metrics_event where kind = 'fixed_resource' order by at",
    )


async def test_creating_the_cells_database_writes_exactly_one_fixed_resource_event(
    cell: Cell,
) -> None:
    b = cell.b
    op = await stateful_deploy(cell, b.w.preview)
    assert await run(b, op, cell.ports) == "running"
    assert await cell.step() == "creating"
    cell.fake.fail_next = 1
    cell.fake.finish()
    assert await cell.step() == "creating"
    assert fixed_resource_events(b) == []
    assert await cell.step() == "creating"
    cell.fake.finish()
    assert await cell.step() == "ready"
    assert await cell.step() == "ready"
    async with bound_org(b.ports.engine, b.w.org) as conn:
        again_once = await record_once(
            conn,
            org_id=b.w.org,
            kind=MetricKind.FIXED_RESOURCE,
            dedup_key="database",
            properties={"resource": "database"},
        )
    assert again_once is False
    assert fixed_resource_events(b) == [
        {
            "dedup_key": "database",
            "properties": {"resource": "database"},
            "app_id": None,
            "environment_id": None,
            "pseudonym": None,
        }
    ]
    again = await stateful_deploy(cell, b.w.prod)
    assert await run(b, again, cell.ports) == "healthy"
    await approve(b, RequirementKind.ENABLE_INTERNET_HOSTS, "api.stripe.com")
    assert await cell.step(CellResource.EGRESS) == "creating"
    cell.fake.finish()
    assert await cell.step(CellResource.EGRESS) == "ready"
    assert [e["dedup_key"] for e in fixed_resource_events(b)] == ["database", "egress"]


async def test_a_resource_that_keeps_failing_fails_its_waiters_and_a_later_deploy_asks_again(
    cell: Cell,
) -> None:
    b = cell.b
    op = await stateful_deploy(cell, b.w.preview)
    assert await run(b, op, cell.ports) == "running"
    cell.fake.fail_next = create.MAX_ATTEMPTS
    for _ in range(create.MAX_ATTEMPTS):
        assert await cell.step() == "creating"
        cell.fake.finish()
        await cell.step()
    row = resources_of(b)["database"]
    assert (row["state"], row["failure_code"]) == ("failed", create.CELL_DEPLOYER_FAILED)
    assert cell_audit(b)[-1]["action"] == "cell.resource_failed"
    assert len(jobs(b, f"dep:{op}")) == 1
    assert await run(b, op, cell.ports) == "failed"
    assert operation(b, op)["failure_code"] == CELL_RESOURCE_FAILED
    assert b.runtime.calls == []
    assert waiters(b) == []

    again = await stateful_deploy(cell, b.w.preview)
    assert await run(b, again, cell.ports) == "running"
    row = resources_of(b)["database"]
    assert (row["state"], row["attempts"], row["failure_code"]) == ("requested", 0, None)
    asked = cell_audit(b)[-1]
    assert asked["action"] == "cell.resource_requested"
    assert asked["before"] == {"state": "failed", "failure_code": create.CELL_DEPLOYER_FAILED}


async def test_with_no_deployer_the_resource_fails_at_once(cell: Cell) -> None:
    b = cell.b
    op = await stateful_deploy(cell, b.w.preview)
    ports = replace(cell.ports, cell_deployer=None)
    assert await run(b, op, ports) == "running"
    assert await create.create_step(ports, org_id=b.w.org, resource=DB) == "failed"
    assert resources_of(b)["database"]["failure_code"] == create.CELL_DEPLOYER_UNAVAILABLE
    assert await run(b, op, ports) == "failed"


async def test_a_deployer_that_will_not_start_is_retried(cell: Cell) -> None:
    b = cell.b
    op = await stateful_deploy(cell, b.w.preview)
    assert await run(b, op, cell.ports) == "running"
    cell.fake.refuse_start = 1
    assert await cell.step() == "creating"
    assert cell.fake.runs == []
    assert resources_of(b)["database"]["attempts"] == 1
    assert await cell.step() == "creating"
    assert len(cell.fake.runs) == 1


# ── approval triggers ────────────────────────────────────────────────────────


async def approve(b: Bench, kind: RequirementKind, subject: str, outcome: str = "approved") -> None:
    async with bound_org(b.ports.engine, b.w.org) as conn:
        asked, _ = await service.request(
            conn,
            org_id=b.w.org,
            environment_id=b.w.prod,
            requirement=Requirement(kind, subject),
            requested_by=b.w.builder,
            via_agent=False,
            payload={},
            actor=Actor(ActorKind.USER, b.w.builder),
        )
        await service.decide(
            conn,
            org_id=b.w.org,
            approval_id=asked.id,
            decider=service.Decider(
                user_id=b.w.approver,
                via_agent=False,
                recorded_by_operator=None,
                channel="console",
                reason="Considered.",
                outcome=outcome,  # type: ignore[arg-type]
            ),
            actor=Actor(ActorKind.USER, b.w.approver),
        )


async def test_an_approved_internet_host_turns_on_egress(cell: Cell) -> None:
    b = cell.b
    await approve(b, RequirementKind.ENABLE_INTERNET_HOSTS, "api.stripe.com")
    assert set(resources_of(b)) == {"egress"}
    (asked,) = cell_audit(b)
    assert (asked["action"], asked["actor_id"]) == ("cell.resource_requested", b.w.approver)
    assert asked["after"]["cause"] == "egress_approved"
    assert len(jobs(b, create_lock(b, CellResource.EGRESS))) == 1
    await approve(b, RequirementKind.ENABLE_INTERNET_HOSTS, "api.github.com")
    assert len(cell_audit(b)) == 1
    assert await cell.step(CellResource.EGRESS) == "creating"
    cell.fake.finish()
    assert await cell.step(CellResource.EGRESS) == "ready"
    assert cell.fake.runs == [(cell.label, CellResource.EGRESS)]


async def test_an_approved_data_source_turns_on_connections(cell: Cell) -> None:
    b = cell.b
    await approve(b, RequirementKind.CONNECT_DATA_SOURCE, "finance")
    assert set(resources_of(b)) == {"connections"}
    assert cell_audit(b)[0]["after"]["cause"] == "connection_granted"
    assert len(jobs(b, create_lock(b, CellResource.CONNECTIONS))) == 1


async def test_a_denied_or_unrelated_approval_turns_nothing_on(cell: Cell) -> None:
    b = cell.b
    await approve(b, RequirementKind.WIDEN_AUDIENCE, "org")
    await approve(b, RequirementKind.ENABLE_INTERNET_HOSTS, "api.stripe.com", "denied")
    assert resources_of(b) == {}


# ── admin API ────────────────────────────────────────────────────────────────


def enable(b: Bench, resource: str, token: str) -> Any:
    return post(b, f"/v1/cell/resources/{resource}/enable", {}, token)


async def test_an_admin_turns_a_resource_on_and_it_is_audited(
    cell: Cell, signing_key: SigningKey
) -> None:
    b = cell.b
    cell_view = get(b, "/v1/cell", b.t.admin)
    assert cell_view.status_code == 200, cell_view.text
    body = cell_view.json()
    assert body["cell_label"] == cell.label
    assert [(r["resource"], r["state"], r["monthly_usd"]) for r in body["resources"]] == [
        ("database", "off", 13),
        ("egress", "off", 7),
        ("connections", "off", 0),
    ]
    assert {r.value: v for r, v in MONTHLY_USD.items()} == {
        "database": 13,
        "egress": 7,
        "connections": 0,
    }
    assert_problem(get(b, "/v1/cell", b.t.member), ErrorCode.FORBIDDEN)
    assert_problem(enable(b, "egress", b.t.member), ErrorCode.FORBIDDEN)
    agent = mint(signing_key, org=b.w.org, sub=b.w.admin, jti=f"cred_{new_key()[:16]}", agent=True)
    assert_problem(enable(b, "egress", agent), ErrorCode.AGENT_SESSION_REFUSED)
    assert enable(b, "disk", b.t.admin).status_code == 422
    assert resources_of(b) == {}

    r = enable(b, "egress", b.t.admin)
    assert r.status_code == 200, r.text
    states = {x["resource"]: x for x in r.json()["resources"]}
    assert (states["egress"]["state"], states["egress"]["cause"]) == ("requested", "admin")
    (asked,) = cell_audit(b)
    assert (asked["action"], asked["actor_kind"], asked["actor_id"]) == (
        "cell.resource_requested",
        "user",
        b.w.admin,
    )
    assert asked["after"]["cause"] == "admin"
    assert len(jobs(b, create_lock(b, CellResource.EGRESS))) == 1
    assert enable(b, "egress", b.t.admin).status_code == 200
    assert len(cell_audit(b)) == 1


async def test_nothing_turns_a_ready_resource_off(cell: Cell) -> None:
    b = cell.b
    assert enable(b, "database", b.t.admin).status_code == 200
    await finish_creation(cell)
    assert enable(b, "database", b.t.admin).json()["resources"][0]["state"] == "ready"
    await approve(b, RequirementKind.CONNECT_DATA_SOURCE, "finance")
    assert await cell.step() == "ready"
    assert resources_of(b)["database"]["state"] == "ready"
    assert [a["action"] for a in cell_audit(b) if a["target_id"] == "database"] == [
        "cell.resource_requested",
        "cell.resource_ready",
    ]
    cell_routes = {
        (method, route.path)
        for route in cell_api.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert cell_routes == {("GET", "/cell"), ("POST", "/cell/resources/{resource}/enable")}
    spec = json.loads((Path(__file__).parents[3] / "docs" / "api" / "openapi.json").read_text())
    published = {(m, p) for p, item in spec["paths"].items() if "/cell" in p for m in item}
    assert published == {("get", "/v1/cell"), ("post", "/v1/cell/resources/{resource}/enable")}
    for method in ("DELETE", "PUT", "PATCH"):
        r = b.client.request(
            method,
            "/v1/cell/resources/database",
            headers=auth(b.t.admin, **{IDEMPOTENCY_HEADER: new_key()}),
        )
        assert r.status_code in (404, 405)


# ── the deployer client and the worker ───────────────────────────────────────


async def test_the_job_is_started_with_exactly_two_arguments() -> None:
    seen: list[httpx2.Request] = []
    execution = f"{JOB}/executions/ssc-cell-deployer-abcde"

    def run_api(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx2.Response(200, json={"name": "op", "metadata": {"name": execution}})
        if request.url.path.endswith("abcde"):
            return httpx2.Response(200, json={"succeededCount": 1, "completionTime": "t"})
        return httpx2.Response(404, json={})

    async def token() -> str:
        return "tok"

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(run_api))
    deployer = CloudRunCellDeployer(JOB, token, client=client)
    assert await deployer.start("testcell05", CellResource.EGRESS) == execution
    post_req = seen[0]
    assert str(post_req.url) == f"https://run.googleapis.com/v2/{JOB}:run"
    assert json.loads(post_req.content) == {
        "overrides": {"containerOverrides": [{"args": ["testcell05", "egress"]}]}
    }
    assert post_req.headers["Authorization"] == "Bearer tok"
    assert await deployer.status(execution) == "succeeded"
    assert await deployer.status(f"{JOB}/executions/gone") == "failed"
    with pytest.raises(CellDeployerError):
        await deployer.status("projects/other/locations/x/jobs/y/executions/z")
    for label in ("other-project-1", "Testcell05", "testcell05 --target x", ""):
        with pytest.raises(ValueError, match="."):
            deployer_args(label, CellResource.DATABASE)
    with pytest.raises(ValueError, match="."):
        deployer_args("testcell05", "gateway_min")  # type: ignore[arg-type]
    await deployer.aclose()


def test_an_execution_status_reads_one_task() -> None:
    assert execution_status({}) == "running"
    assert execution_status({"runningCount": 1}) == "running"
    assert execution_status({"succeededCount": 1, "completionTime": "t"}) == "succeeded"
    assert execution_status({"failedCount": 1, "completionTime": "t"}) == "failed"
    assert execution_status({"cancelledCount": 1}) == "failed"
    assert execution_status({"completionTime": "t"}) == "failed"


def test_the_worker_registers_the_cell_task_and_its_deployer() -> None:
    app = build_app("postgresql://ssc_app@localhost/ssc")
    assert tasks.CREATE_RESOURCE in app.tasks
    base = {"SSC_DATABASE_DSN": "postgresql+psycopg://ssc_app@localhost/ssc"}
    assert compose_ports(base).cell_deployer is None
    with pytest.raises(CompositionError, match="cell_deployer"):
        compose_ports({**base, DEPLOYER_ENV: "fake"})
    fake = compose_ports({**base, DEPLOYER_ENV: "fake", "SSC_ENV": "test"})
    assert isinstance(fake.cell_deployer, FakeCellDeployer)
    real = compose_ports({**base, DEPLOYER_ENV: "cloud_run", DEPLOYER_JOB_ENV: JOB})
    assert isinstance(real.cell_deployer, CloudRunCellDeployer)
    for job in ("", "jobs/x", f"{JOB}/extra", "projects//locations/r/jobs/j"):
        with pytest.raises(CompositionError):
            compose_ports({**base, DEPLOYER_ENV: "cloud_run", DEPLOYER_JOB_ENV: job})
    with pytest.raises(CompositionError):
        compose_ports({**base, DEPLOYER_ENV: "gcloud"})
    assert cell_deployer_from_env({}) is None
