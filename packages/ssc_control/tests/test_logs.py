"""SSC-024: logs and health through the control plane. The API checks the caller, asks the cell
agent (in process, over the Cloud Logging and Cloud Run emulators) and redacts again.

Uses test_deploy's bench.

Ticket "done when" checks that run here (the live run is SSC-086):
  * ``ssc logs --follow`` shows a new line within 5 seconds -> test_a_follow_through_the_api_...
    (the agent leg: conformance's test_cloud_logging; the CLI leg: ssc_cli's test_logs_follow_...)
  * a ``user``-role caller gets a 403                       -> test_a_user_role_caller_gets_403
Plus: who else may read, agent and preview-scoped credentials, the build and deploy sources,
redaction in the API, the rate-limit and no-cell refusals, and health from the API.
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import cast

import httpx2
import psycopg
import pytest
import test_deploy
from fastapi import FastAPI
from ssc_testkit import SigningKey, assert_problem, mint, new_key
from test_deploy import (
    AGENT,
    CELL_RUNTIME,
    Bench,
    access_token,
    agent_token,
    build_release,
    deploy,
    execute,
    get,
)

from ssc_agent.app import create_app as create_agent
from ssc_agent.cloud_logging import CellLogHub, CloudLoggingEntries
from ssc_agent.cloud_run import CloudRunDriver
from ssc_conformance.cloud_logging_emulator import VIEW, CloudLoggingEmulator
from ssc_conformance.cloud_run_emulator import CloudRunEmulator
from ssc_conformance.contracts.runtime_driver import new_spec
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.db import bind_org_sync
from ssc_control.runtime.cell_logs import AgentCellLogs
from ssc_shared.logs import Health, LogLine, LogPage, LogQuery, LogsRateLimitedError
from ssc_shared.runtime import service_name

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b

BUILD_REF = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
FIRST = "sha256:" + "1" * 64


@dataclass
class Cell:
    logging: CloudLoggingEmulator
    run: CloudRunEmulator
    driver: CloudRunDriver
    logs: AgentCellLogs


@pytest.fixture
async def cell(b: Bench) -> AsyncIterator[Cell]:
    logging, run = CloudLoggingEmulator(), CloudRunEmulator(auto_settle=True)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "logging.googleapis.com":
            return logging.handler(request)
        return run.handler(request)

    def mock() -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))

    driver = CloudRunDriver(CELL_RUNTIME, access_token, client=mock())
    hub = CellLogHub(CloudLoggingEntries((VIEW,), access_token, client=mock()), driver)
    transport = httpx2.ASGITransport(app=create_agent(driver, logs=hub))
    logs = AgentCellLogs(AGENT, agent_token, client=httpx2.AsyncClient(transport=transport))
    _use(b, logs)
    yield Cell(logging, run, driver, logs)
    await logs.aclose()
    await driver.aclose()


def _use(b: Bench, logs: object) -> None:
    app = cast("FastAPI", b.client.app)
    app.state.runtime = replace(app.state.runtime, cell_logs=logs)


def logs_path(b: Bench, env: str, query: str = "") -> str:
    return f"/v1/apps/{b.w.app}/environments/{env}/logs{query}"


def user_role_token(b: Bench, signing_key: SigningKey, env: str) -> str:
    """A new member who holds a ``user`` grant on ``env``: may use the app, not read its logs."""
    uid = new_id("usr")
    with psycopg.connect(b.dsn) as conn:
        bind_org_sync(conn, b.w.org)
        conn.execute(
            "insert into ssc.user_account (id, org_id, display_name, email, role, status) "
            "values (%s, %s, 'App User', 'user@example.com', 'member', 'active')",
            (uid, b.w.org),
        )
        conn.execute(
            "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, "
            "user_id, granted_by_user_id) values (%s, %s, %s, 'user', 'user', %s, %s)",
            (new_id("gnt"), b.w.org, env, uid, b.w.admin),
        )
    return mint(signing_key, org=b.w.org, sub=uid, jti=f"cred_{new_key()[:16]}")


class StubLogs:
    """A cell that answers with raw text, or refuses as told."""

    def __init__(self, text: str = "", refuse: Exception | None = None) -> None:
        self.text = text
        self.refuse = refuse

    async def read(
        self, query: LogQuery, *, since_seconds: int, limit: int, caller: str
    ) -> LogPage:
        if self.refuse is not None:
            raise self.refuse
        line = LogLine(timestamp=datetime.now(UTC), severity="INFO", source="app", text=self.text)
        return LogPage(lines=(line,), cursor="0.0.1")

    async def follow(
        self, query: LogQuery, *, cursor: str | None, wait_seconds: float, caller: str
    ) -> LogPage:
        return await self.read(query, since_seconds=1, limit=1, caller=caller)

    async def health(self, service: str, *, caller: str) -> Health:
        raise NotImplementedError


# ── who may read ─────────────────────────────────────────────────────────────


async def test_a_user_role_caller_gets_403(b: Bench, cell: Cell, signing_key: SigningKey) -> None:
    cell.logging.app_line(service_name(b.w.preview), "hello")
    token = user_role_token(b, signing_key, b.w.preview)
    assert_problem(get(b, logs_path(b, b.w.preview), token), ErrorCode.FORBIDDEN)
    follow = logs_path(b, b.w.preview, "?after=0.0.1&wait=1")
    assert_problem(get(b, follow, token), ErrorCode.FORBIDDEN)
    assert_problem(get(b, logs_path(b, b.w.preview), b.t.member), ErrorCode.FORBIDDEN)
    assert cell.logging.calls == []


async def test_builders_the_owner_and_admins_read_redacted_lines(
    b: Bench, cell: Cell, signing_key: SigningKey
) -> None:
    service = service_name(b.w.preview)
    cell.logging.app_line(service, "listening on :8080")
    cell.logging.app_line(service, "db password=fake-pass-5678 ok", severity="WARNING")
    cell.logging.app_line(service_name(b.w.prod), "prod only")
    approver = mint(signing_key, org=b.w.org, sub=b.w.approver, jti=f"cred_{new_key()[:16]}")
    for token in (b.t.builder, b.t.admin, approver):
        r = get(b, logs_path(b, b.w.preview), token)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["environment_id"] == b.w.preview
        assert body["source"] == "app"
        assert [line["text"] for line in body["lines"]] == [
            "listening on :8080",
            "db password=[redacted] ok",
        ]
        assert body["lines"][1]["severity"] == "WARNING"
        assert body["cursor"]


async def test_an_agent_credential_reads_as_its_person(
    b: Bench, cell: Cell, signing_key: SigningKey
) -> None:
    cell.logging.app_line(service_name(b.w.preview), "hello")
    agent = mint(
        signing_key, org=b.w.org, sub=b.w.builder, jti=f"cred_{new_key()[:16]}", agent=True
    )
    r = get(b, logs_path(b, b.w.preview), agent)
    assert r.status_code == 200, r.text
    assert [line["text"] for line in r.json()["lines"]] == ["hello"]


async def test_a_preview_scoped_credential_reads_no_prod_logs(
    b: Bench, cell: Cell, signing_key: SigningKey
) -> None:
    preview = mint(
        signing_key, org=b.w.org, sub=b.w.builder, jti=f"cred_{new_key()[:16]}", scope="preview"
    )
    assert_problem(get(b, logs_path(b, b.w.prod), preview), ErrorCode.FORBIDDEN)
    assert get(b, logs_path(b, b.w.preview), preview).status_code == 200


async def test_an_unknown_environment_is_not_found(b: Bench, cell: Cell) -> None:
    r = get(b, logs_path(b, new_id("env")))
    assert_problem(r, ErrorCode.NOT_FOUND)


# ── follow ───────────────────────────────────────────────────────────────────


async def test_a_follow_through_the_api_shows_a_new_line_within_5_seconds(
    b: Bench, cell: Cell
) -> None:
    service = service_name(b.w.preview)
    cell.logging.app_line(service, "before")
    first = get(b, logs_path(b, b.w.preview, "?since=600"))
    assert [line["text"] for line in first.json()["lines"]] == ["before"]
    cursor = first.json()["cursor"]
    writer = threading.Timer(0.5, lambda: cell.logging.app_line(service, "a new line"))
    started = time.monotonic()
    writer.start()
    seen: list[str] = []
    while not seen and time.monotonic() - started < 5:
        r = get(b, logs_path(b, b.w.preview, f"?after={cursor}&wait=5"))
        assert r.status_code == 200, r.text
        seen = [line["text"] for line in r.json()["lines"]]
        cursor = r.json()["cursor"]
    writer.join()
    assert seen == ["a new line"]
    assert time.monotonic() - started < 5


# ── sources ──────────────────────────────────────────────────────────────────


async def test_build_lines_are_the_environments_own_builds(b: Bench, cell: Cell) -> None:
    await build_release(b, b.w.preview)
    execute(b.dsn, b.w.org, "update ssc.build set driver_ref = %s", BUILD_REF)
    cell.logging.build_line(BUILD_REF, "Step 1/4 : railpack build")
    cell.logging.build_line("11111111-2222-3333-4444-555555555555", "someone else's build")
    r = get(b, logs_path(b, b.w.preview, "?source=build"))
    assert r.status_code == 200, r.text
    assert [(x["source"], x["text"]) for x in r.json()["lines"]] == [
        ("build", "Step 1/4 : railpack build")
    ]
    assert BUILD_REF in cell.logging.calls[-1]["filter"]
    r = get(b, logs_path(b, b.w.prod, "?source=build"))
    assert r.json()["lines"] == []


async def test_deploy_lines_come_from_the_deployments(b: Bench) -> None:
    release = await build_release(b, b.w.preview)
    op, state = await deploy(b, b.w.preview, release)
    assert state == "healthy"
    r = get(b, logs_path(b, b.w.preview, "?source=deploy"))
    assert r.status_code == 200, r.text
    texts = [line["text"] for line in r.json()["lines"]]
    assert texts == [f"deploy {op} of {release} started", f"deploy {op} of {release} is live"]
    after = r.json()["cursor"]
    r = get(b, logs_path(b, b.w.preview, f"?source=deploy&after={after}&wait=1"))
    assert r.json()["lines"] == []
    assert r.json()["cursor"] == after


# ── refusals and redaction ───────────────────────────────────────────────────


async def test_without_a_cell_logs_are_unavailable_and_deploy_lines_still_read(b: Bench) -> None:
    assert_problem(get(b, logs_path(b, b.w.preview)), ErrorCode.LOGS_UNAVAILABLE)
    path = f"/v1/apps/{b.w.app}/environments/{b.w.preview}/health"
    assert_problem(get(b, path), ErrorCode.LOGS_UNAVAILABLE)
    assert get(b, logs_path(b, b.w.preview, "?source=deploy")).status_code == 200


async def test_the_cells_rate_limit_reaches_the_caller_with_retry_after(b: Bench) -> None:
    _use(b, StubLogs(refuse=LogsRateLimitedError("busy", 7)))
    r = get(b, logs_path(b, b.w.preview))
    assert_problem(r, ErrorCode.LOGS_RATE_LIMITED)
    assert r.headers["Retry-After"] == "7"


async def test_the_api_redacts_what_the_cell_sends_again(b: Bench) -> None:
    _use(b, StubLogs("token=fake-token-abcdef and Bearer fake.bearer.value-1234"))
    r = get(b, logs_path(b, b.w.preview))
    (line,) = r.json()["lines"]
    assert "fake-token-abcdef" not in line["text"]
    assert "value-1234" not in line["text"]


async def test_bad_queries_are_refused(b: Bench, cell: Cell) -> None:
    for query in ("?source=timers", "?since=0", "?limit=5000", "?after=x", "?wait=60"):
        assert_problem(get(b, logs_path(b, b.w.preview, query)), ErrorCode.VALIDATION_FAILED)
    assert cell.logging.calls == []


# ── health ───────────────────────────────────────────────────────────────────


async def test_health_through_the_api(b: Bench, cell: Cell, signing_key: SigningKey) -> None:
    path = f"/v1/apps/{b.w.app}/environments/{b.w.preview}/health"
    r = get(b, path)
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["reason"]) == (None, "not_deployed")
    service = service_name(b.w.preview)
    await cell.driver.apply(new_spec(FIRST, service=service))
    cell.run.settle()
    cell.logging.request(service, 200)
    token = user_role_token(b, signing_key, b.w.preview)
    r = get(b, path, token)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["state"], body["reason"]) == ("running", "serving")
    assert body["last_request_at"] is not None
