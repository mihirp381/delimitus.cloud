"""The data gateway's query pipeline end to end over ASGI (SSC-050): the snapshot is a real feed
over a local blob store, Google's keys a local RSA key, the database a fake connector."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from datagw_world import (
    AUDIENCE,
    BEN,
    CERTS,
    DOMAIN,
    ENV,
    FORGED_KEY,
    LABEL,
    LEDGER,
    ORG,
    PAY,
    PAYROLL,
    PREVIEW,
    PROD,
    PROJECT,
    SALES,
    SETTINGS,
    FakeConnector,
    bearer,
    certs_transport,
    note,
    publish,
    store,
)
from fastapi import FastAPI

from ssc_contracts.identity import IDENTITY_HEADER
from ssc_datagw.admission import RECHECK_SECONDS, OnDemandSnapshot
from ssc_datagw.connectors import (
    Connector,
    QueryFailedError,
    QueryRefusedError,
    UpstreamUnavailableError,
    encoded_size,
)
from ssc_datagw.limits import DEFAULT_MAX_ROWS, PLATFORM, Slots
from ssc_datagw.server import MAX_BODY, DataGateway, create_app, production_app
from ssc_datagw.workload import GoogleWorkloads
from ssc_shared.access import ViewHolder
from ssc_shared.blobstore_fs import FsBlobStore
from ssc_shared.hosts import app_origin
from ssc_shared.snapshot_feed import SnapshotFeed, latest_key

SQL = "select id, amount from sales where id > $1"
KILLS = {
    "a suspended connection": ({"sales": "suspended"}, "CONNECTION_SUSPENDED"),
    "a disabled app": ({"status": "disabled"}, "APP_NOT_ACTIVE"),
    "a quarantined app": ({"status": "quarantined"}, "APP_NOT_ACTIVE"),
}


class Clock:
    """A monotonic clock the test moves."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def rows(n: int) -> list[tuple[int, Decimal]]:
    return [(i, Decimal(f"{i}.50")) for i in range(n)]


def workloads(*, fail: bool = False) -> GoogleWorkloads:
    return GoogleWorkloads(
        audience=AUDIENCE,
        project_id=PROJECT,
        transport=certs_transport(fail=fail),
        certs_url=CERTS,
    )


def client(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://datagw")


@dataclass
class World:
    blobs: FsBlobStore
    connector: FakeConnector
    http: httpx2.AsyncClient

    async def ask(
        self,
        name: str = "sales",
        body: Mapping[str, Any] | None = None,
        *,
        env_id: str = PROD,
        headers: Mapping[str, str] | None = None,
        **token: Any,
    ) -> httpx2.Response:
        sent = {**bearer(env_id, **token), **(headers or {})}
        payload = {"sql": SQL, "params": [0]} if body is None else body
        return await self.http.post(f"/v1/connections/{name}/query", json=payload, headers=sent)


@asynccontextmanager
async def running(  # noqa: PLR0913  (the test's choice of collaborators)
    tmp_path: Path,
    *,
    connector: FakeConnector | None = None,
    connectors: Mapping[str, Connector] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    slots: Slots | None = None,
    grace: float = 2.0,
    watch_seconds: float = 1.0,
    google_down: bool = False,
    published: bool = True,
    **changes: Any,
) -> AsyncGenerator[World]:
    blobs = store(tmp_path)
    if published:
        await publish(blobs, 1, **changes)
    holder = ViewHolder(ORG)
    feed = SnapshotFeed(blobs, holder, monotonic=monotonic)
    snapshot = OnDemandSnapshot(feed, holder, max_stale=SETTINGS.max_stale)
    connector = connector or FakeConnector(rows=rows(2))
    gateway = DataGateway(
        settings=SETTINGS,
        workloads=workloads(fail=google_down),
        snapshot=snapshot,
        connectors={SALES: connector} if connectors is None else connectors,
        slots=slots,
        grace=grace,
        watch_seconds=watch_seconds,
    )
    async with client(create_app(gateway)) as http:
        try:
            yield World(blobs, connector, http)
        finally:
            await gateway.aclose()
            await snapshot.aclose()


def records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    prefix = "datagw query "
    return [
        json.loads(r.getMessage().removeprefix(prefix))
        for r in caplog.records
        if r.getMessage().startswith(prefix)
    ]


def error(response: httpx2.Response) -> dict[str, Any]:
    body = response.json()
    assert body["request_id"] == response.headers["x-request-id"]
    return body["error"]


async def test_a_granted_query_is_served_with_decimals_kept_exact(tmp_path: Path) -> None:
    async with running(tmp_path) as w:
        r = await w.ask(headers={"x-request-id": "req-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == [
        {"name": "id", "type": "integer", "db_type": "int4"},
        {"name": "amount", "type": "decimal", "db_type": "numeric"},
    ]
    assert body["rows"] == [[0, "0.50"], [1, "1.50"]]
    assert (body["row_count"], body["truncated"], body["truncated_reason"]) == (2, False, None)
    assert (body["request_id"], body["snapshot_version"]) == ("req-1", 1)
    assert isinstance(body["elapsed_ms"], int)
    assert r.headers["x-request-id"] == "req-1"
    (query,) = w.connector.queries
    assert (query.sql, query.params, query.tag) == (SQL, (0,), f"ssc:{LEDGER}:prod:req-1")
    assert (query.max_rows, query.timeout_ms) == (DEFAULT_MAX_ROWS, PLATFORM.timeout_ms)


FORGED = {
    "no token": {"authorization": ""},
    "signed by another key": bearer(key=FORGED_KEY),
    "for another service": bearer(aud="https://elsewhere.run.app"),
    "an app of another cell": bearer(email=f"ssc-a-{'p' * 20}@ssc-c-other.iam.gserviceaccount.com"),
}


@pytest.mark.parametrize("case", sorted(FORGED))
async def test_done_when_a_forged_workload_token_is_refused(tmp_path: Path, case: str) -> None:
    async with running(tmp_path) as w:
        r = await w.http.post(
            "/v1/connections/sales/query", json={"sql": SQL}, headers=FORGED[case]
        )
        not_a_query = await w.http.post(
            "/v1/connections/sales/query", content=b"{", headers=FORGED[case]
        )
    assert r.status_code == 401
    assert error(r) | {"message": ""} == {
        "code": "UNAUTHENTICATED",
        "stage": "workload",
        "message": "",
        "fix_owner": "app",
    }
    assert error(not_a_query)["code"] == "UNAUTHENTICATED"
    assert w.connector.queries == []


async def test_done_when_a_query_over_the_row_cap_is_truncated(tmp_path: Path) -> None:
    async with running(tmp_path, connector=FakeConnector(rows=rows(10))) as w:
        capped = (await w.ask(body={"sql": SQL, "max_rows": 3})).json()
        exact = (await w.ask(body={"sql": SQL, "max_rows": 10})).json()
    assert (capped["row_count"], capped["truncated"], capped["truncated_reason"]) == (
        3,
        True,
        "max_rows",
    )
    assert capped["rows"] == [[0, "0.50"], [1, "1.50"], [2, "2.50"]]
    assert (exact["row_count"], exact["truncated"]) == (10, False)
    assert [q.max_rows for q in w.connector.queries] == [3, 10]


async def test_the_default_row_cap_and_the_platform_ceiling(tmp_path: Path) -> None:
    connector = FakeConnector(rows=rows(DEFAULT_MAX_ROWS + 1))
    async with running(tmp_path, connector=connector) as w:
        default = (await w.ask()).json()
        asked = await w.ask(body={"sql": SQL, "max_rows": 10**9})
    assert (default["row_count"], default["truncated_reason"]) == (DEFAULT_MAX_ROWS, "max_rows")
    assert asked.json()["truncated"] is False
    assert connector.queries[1].max_rows == PLATFORM.max_rows


async def test_the_connection_and_the_grant_cap_rows_below_what_the_app_asks(
    tmp_path: Path,
) -> None:
    connector = FakeConnector(rows=rows(10))
    async with running(
        tmp_path, connector=connector, sales_limits={"max_rows": 4}, grant_limits={"max_rows": 2}
    ) as w:
        prod = (await w.ask(body={"sql": SQL, "max_rows": 10})).json()
        preview = (await w.ask(body={"sql": SQL, "max_rows": 10}, env_id=PREVIEW)).json()
    assert (prod["row_count"], prod["truncated_reason"]) == (2, "max_rows")
    assert (preview["row_count"], preview["truncated_reason"]) == (4, "max_rows")


async def test_a_result_over_the_byte_cap_is_cut_at_a_whole_row(tmp_path: Path) -> None:
    three = sum(encoded_size([i, f"{i}.50"]) for i in range(3))
    async with running(tmp_path, connector=FakeConnector(rows=rows(10))) as w:
        body = (await w.ask(body={"sql": SQL, "max_bytes": three + 1})).json()
        none = (await w.ask(body={"sql": SQL, "max_bytes": 0})).json()
    assert (body["row_count"], body["truncated_reason"]) == (3, "max_bytes")
    assert (none["row_count"], none["truncated_reason"]) == (0, "max_bytes")


async def test_done_when_a_suspended_connection_refuses_within_five_seconds(
    tmp_path: Path,
) -> None:
    async with running(tmp_path) as w:
        assert (await w.ask()).status_code == 200
        await publish(w.blobs, 2, sales="suspended")
        killed_at = time.monotonic()
        while (r := await w.ask()).status_code == 200:
            assert time.monotonic() - killed_at < 5
            await asyncio.sleep(0.1)
        took = time.monotonic() - killed_at
    assert took < 5
    assert (r.status_code, error(r)["code"], error(r)["fix_owner"]) == (
        403,
        "CONNECTION_SUSPENDED",
        "admin",
    )


@pytest.mark.parametrize("case", sorted(KILLS))
async def test_done_when_a_service_at_zero_when_the_kill_fired_refuses_its_first_query(
    tmp_path: Path, case: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ssc_datagw.server")
    changes, code = KILLS[case]
    blobs = store(tmp_path)
    await publish(blobs, 1)
    await publish(blobs, 2, **changes)
    connector = FakeConnector(rows=rows(1))
    app = production_app(ENV, store=blobs, connectors={SALES: connector}, workloads=workloads())
    started = time.monotonic()
    async with app.router.lifespan_context(app), client(app) as http:
        r = await http.post("/v1/connections/sales/query", json={"sql": SQL}, headers=bearer())
    assert time.monotonic() - started < 5
    assert (r.status_code, error(r)["code"]) == (403, code)
    assert connector.queries == []
    (record,) = records(caplog)
    assert (record["snapshot_version"], record["outcome"], record["cold"]) == (2, code, True)


@pytest.mark.parametrize("case", sorted(KILLS))
async def test_the_kill_watch_ends_a_running_query(tmp_path: Path, case: str) -> None:
    changes, code = KILLS[case]
    clock = Clock()
    connector = FakeConnector(rows=rows(1), hold=asyncio.Event())
    async with running(tmp_path, connector=connector, monotonic=clock, watch_seconds=0.02) as w:
        asking = asyncio.create_task(w.ask())
        await asyncio.wait_for(connector.started.wait(), 1)
        await asyncio.sleep(0.1)
        assert not asking.done()
        await publish(w.blobs, 2, **changes)
        clock.advance(RECHECK_SECONDS + 0.1)
        r = await asyncio.wait_for(asking, 2)
    assert r.status_code == 403
    assert (error(r)["code"], error(r)["stage"]) == (code, "execute")
    assert (connector.cancelled, connector.running, connector.closed) == (1, 0, 1)


async def test_done_when_a_running_query_is_ended_within_five_seconds_of_a_suspension(
    tmp_path: Path,
) -> None:
    connector = FakeConnector(rows=rows(1), hold=asyncio.Event())
    async with running(tmp_path, connector=connector) as w:
        asking = asyncio.create_task(w.ask())
        await asyncio.wait_for(connector.started.wait(), 1)
        await publish(w.blobs, 2, sales="suspended")
        killed_at = time.monotonic()
        r = await asyncio.wait_for(asking, 5)
        took = time.monotonic() - killed_at
    assert took < 5
    assert (r.status_code, error(r)["code"], connector.cancelled) == (
        403,
        "CONNECTION_SUSPENDED",
        1,
    )


async def test_done_when_the_first_query_after_a_quiet_spell_is_served_and_its_start_recorded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ssc_datagw.server")
    blobs = store(tmp_path)
    await publish(blobs, 1)
    connector = FakeConnector(rows=rows(2))
    app = production_app(ENV, store=blobs, connectors={SALES: connector}, workloads=workloads())
    async with app.router.lifespan_context(app), client(app) as http:
        first = await http.post("/v1/connections/sales/query", json={"sql": SQL}, headers=bearer())
        second = await http.post("/v1/connections/sales/query", json={"sql": SQL}, headers=bearer())
    assert (first.status_code, second.status_code) == (200, 200)
    ready = [r.getMessage() for r in caplog.records if r.getMessage().startswith("datagw ready ")]
    (start,) = [json.loads(m.removeprefix("datagw ready ")) for m in ready]
    assert start["snapshot_version"] == 1
    assert start["ready_ms"] >= 0
    cold, warm = records(caplog)
    assert cold["cold"] is True
    assert cold["outcome"] == "served"
    assert cold["instance_started_at"] == start["instance_started_at"]
    assert cold["ready_ms"] == start["ready_ms"]
    assert cold["received_at"] >= cold["instance_started_at"]
    assert warm["cold"] is False
    assert "ready_ms" not in warm
    assert warm["instance_started_at"] == cold["instance_started_at"]


async def test_a_snapshot_no_read_confirmed_for_120_seconds_admits_nothing(
    tmp_path: Path,
) -> None:
    clock = Clock()
    async with running(tmp_path, monotonic=clock) as w:
        assert (await w.ask()).status_code == 200
        await w.blobs.delete(latest_key(ORG))
        clock.advance(SETTINGS.max_stale)
        assert (await w.ask()).status_code == 200
        clock.advance(1)
        r = await w.ask()
    assert r.status_code == 503
    assert error(r) | {"message": ""} == {
        "code": "DATA_SNAPSHOT_STALE",
        "stage": "admission",
        "message": "",
        "fix_owner": "platform",
    }


async def test_no_snapshot_at_all_admits_nothing(tmp_path: Path) -> None:
    async with running(tmp_path, published=False) as w:
        r = await w.ask()
    assert (r.status_code, error(r)["code"]) == (503, "DATA_SNAPSHOT_STALE")
    assert w.connector.queries == []


async def test_a_connection_not_granted_and_one_that_does_not_exist_look_the_same(
    tmp_path: Path,
) -> None:
    async with running(tmp_path) as w:
        other_app = await w.ask(env_id=PAY)
        other_name = await w.ask("hr")
        no_such = await w.ask("nope")
        bad_name = await w.ask("NOT A NAME")
    for r in (other_app, other_name, no_such, bad_name):
        assert (r.status_code, error(r)["code"]) == (403, "CONNECTION_NOT_GRANTED")
    assert w.connector.queries == []


async def test_an_org_without_connections_grants_nothing(tmp_path: Path) -> None:
    async with running(tmp_path, connections=False) as w:
        r = await w.ask()
    assert (r.status_code, error(r)["code"]) == (403, "CONNECTION_NOT_GRANTED")


async def test_an_environment_not_in_the_snapshot_is_refused(tmp_path: Path) -> None:
    async with running(tmp_path) as w:
        r = await w.ask(env_id="env_" + "z" * 20)
    assert (r.status_code, error(r)["code"], error(r)["fix_owner"]) == (
        403,
        "UNKNOWN_ENVIRONMENT",
        "platform",
    )


PREVIEW_ORIGIN = app_origin("ledger", "preview", LABEL, DOMAIN)
NOTES = {
    "a user of the app": (lambda: note(), PROD, ("usr_" + "a" * 20, "verified")),
    "a user of its preview": (
        lambda: note(env="preview", aud=PREVIEW_ORIGIN),
        PREVIEW,
        ("usr_" + "a" * 20, "verified"),
    ),
    "a schedule": (
        lambda: note(sub="sch_" + "s" * 20),
        PROD,
        ("sch_" + "s" * 20, "schedule"),
    ),
}


@pytest.mark.parametrize("case", sorted(NOTES))
async def test_the_user_the_app_acts_for_is_logged(
    tmp_path: Path, case: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ssc_datagw.server")
    token, env_id, expect = NOTES[case]
    async with running(tmp_path) as w:
        r = await w.ask(env_id=env_id, headers={IDENTITY_HEADER: token()})
        bare = await w.ask(env_id=env_id)
    assert (r.status_code, bare.status_code) == (200, 200)
    with_note, without = records(caplog)
    assert (with_note["user"], with_note["user_context"]) == expect
    assert (without["user"], without["user_context"]) == (None, "app_only")


BAD_NOTES = {
    "another app's audience": lambda: note(aud=app_origin("payroll", "prod", LABEL, DOMAIN)),
    "another environment of the app": lambda: note(env="preview"),
    "another app": lambda: note(app=PAYROLL),
    "another org": lambda: note(org="org_" + "z" * 20),
    "a deactivated user": lambda: note(sub=BEN),
    "an unknown user": lambda: note(sub="usr_" + "z" * 20),
    "expired": lambda: note(iat=int(time.time()) - 900, exp=int(time.time()) - 600),
    "not a note": lambda: "not.a.note",
    "a workload token": lambda: bearer()["authorization"].removeprefix("Bearer "),
}


@pytest.mark.parametrize("case", sorted(BAD_NOTES))
async def test_a_note_that_does_not_verify_for_the_environment_is_refused(
    tmp_path: Path, case: str
) -> None:
    async with running(tmp_path) as w:
        r = await w.ask(headers={IDENTITY_HEADER: BAD_NOTES[case]()})
    assert (r.status_code, error(r)["code"], error(r)["stage"]) == (
        401,
        "IDENTITY_REFUSED",
        "user",
    )
    assert w.connector.queries == []


BAD_BODIES: dict[str, Any] = {
    "no sql": {},
    "empty sql": {"sql": ""},
    "sql not a string": {"sql": 1},
    "an extra field": {"sql": SQL, "database": "other"},
    "a negative cap": {"sql": SQL, "max_rows": -1},
    "a cap not an integer": {"sql": SQL, "max_bytes": 1.5},
    "an object parameter": {"sql": SQL, "params": [{"a": 1}]},
    "too many parameters": {"sql": SQL, "params": [0] * 1001},
}


@pytest.mark.parametrize("case", sorted(BAD_BODIES))
async def test_a_body_that_is_not_a_query_is_refused(tmp_path: Path, case: str) -> None:
    async with running(tmp_path) as w:
        r = await w.ask(body=BAD_BODIES[case])
    assert (r.status_code, error(r)["code"], error(r)["stage"]) == (
        422,
        "VALIDATION_FAILED",
        "request",
    )


async def test_a_body_that_is_not_json_or_too_large_is_refused(tmp_path: Path) -> None:
    async with running(tmp_path) as w:
        path = "/v1/connections/sales/query"
        broken = await w.http.post(path, content=b"{", headers=bearer())
        large = await w.http.post(path, content=b" " * (MAX_BODY + 1), headers=bearer())
    assert (broken.status_code, error(broken)["code"]) == (422, "VALIDATION_FAILED")
    assert (large.status_code, error(large)["code"]) == (413, "BODY_TOO_LARGE")


async def test_the_daily_budget_cuts_then_refuses_per_grant(tmp_path: Path) -> None:
    connector = FakeConnector(rows=rows(3))
    async with running(tmp_path, connector=connector, grant_limits={"daily_rows": 5}) as w:
        first = (await w.ask()).json()
        second = (await w.ask()).json()
        third = await w.ask()
        preview = await w.ask(env_id=PREVIEW)
    assert (first["row_count"], first["truncated"]) == (3, False)
    assert (second["row_count"], second["truncated_reason"]) == (2, "daily_rows")
    assert (third.status_code, error(third)["code"]) == (429, "DAILY_BUDGET_SPENT")
    assert preview.json()["row_count"] == 3
    assert len(connector.queries) == 3


async def test_the_daily_byte_budget_names_itself(tmp_path: Path) -> None:
    connector = FakeConnector(rows=rows(10))
    three = sum(encoded_size([i, f"{i}.50"]) for i in range(3))
    async with running(tmp_path, connector=connector, grant_limits={"daily_bytes": three}) as w:
        body = (await w.ask()).json()
        after = await w.ask()
    assert (body["row_count"], body["truncated_reason"]) == (3, "daily_bytes")
    assert (after.status_code, error(after)["code"]) == (429, "DAILY_BUDGET_SPENT")


async def test_a_grant_runs_at_most_its_concurrency(tmp_path: Path) -> None:
    connector = FakeConnector(rows=rows(1), hold=asyncio.Event())
    async with running(
        tmp_path, connector=connector, slots=Slots(wait=0.05), grant_limits={"concurrency": 1}
    ) as w:
        first = asyncio.create_task(w.ask())
        await asyncio.wait_for(connector.started.wait(), 1)
        busy = await w.ask()
        other_grant = asyncio.create_task(w.ask(env_id=PREVIEW))
        await asyncio.sleep(0.05)
        assert connector.running == 2
        assert connector.hold is not None
        connector.hold.set()
        served = await first
        await other_grant
    assert (busy.status_code, error(busy)["code"], error(busy)["fix_owner"]) == (
        429,
        "CONCURRENCY_LIMIT",
        "app",
    )
    assert served.status_code == 200


async def test_a_concurrency_of_zero_runs_nothing(tmp_path: Path) -> None:
    async with running(tmp_path, sales_limits={"concurrency": 0}) as w:
        r = await w.ask()
    assert (r.status_code, error(r)["code"]) == (429, "CONCURRENCY_LIMIT")
    assert w.connector.queries == []


CONNECTOR_ERRORS = {
    "refused": (QueryRefusedError("two statements"), 422, "QUERY_REFUSED", "classify", None),
    "failed": (
        QueryFailedError('relation "x" does not exist', sqlstate="42P01"),
        422,
        "QUERY_FAILED",
        "execute",
        "42P01",
    ),
    "unreachable": (
        UpstreamUnavailableError("refused"),
        503,
        "CONNECTION_UNAVAILABLE",
        "execute",
        None,
    ),
    "a bug": (RuntimeError("boom"), 503, "UNAVAILABLE", "execute", None),
}


@pytest.mark.parametrize("case", sorted(CONNECTOR_ERRORS))
async def test_connector_errors_map_to_their_codes(tmp_path: Path, case: str) -> None:
    exc, status, code, stage, sqlstate = CONNECTOR_ERRORS[case]
    async with running(tmp_path, connector=FakeConnector(error=exc)) as w:
        r = await w.ask()
    got = error(r)
    assert (r.status_code, got["code"], got["stage"], got.get("sqlstate")) == (
        status,
        code,
        stage,
        sqlstate,
    )
    assert str(exc) not in r.text


async def test_a_query_past_its_time_limit_is_ended(tmp_path: Path) -> None:
    connector = FakeConnector(rows=rows(1), hold=asyncio.Event())
    async with running(tmp_path, connector=connector, grace=0.0) as w:
        r = await w.ask(body={"sql": SQL, "timeout_ms": 20})
    assert (r.status_code, error(r)["code"]) == (408, "QUERY_TIMEOUT")
    assert connector.queries[0].timeout_ms == 20
    assert (connector.cancelled, connector.running) == (1, 0)


async def test_a_connection_without_a_connector_is_unavailable(tmp_path: Path) -> None:
    async with running(tmp_path, connectors={}) as w:
        r = await w.ask()
    assert (r.status_code, error(r)["code"]) == (503, "CONNECTION_UNAVAILABLE")


async def test_no_google_keys_is_unavailable(tmp_path: Path) -> None:
    async with running(tmp_path, google_down=True) as w:
        r = await w.ask()
    assert (r.status_code, error(r)["code"], error(r)["fix_owner"]) == (
        503,
        "UNAVAILABLE",
        "platform",
    )


async def test_logs_carry_the_outcome_and_never_the_sql_parameters_or_rows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    secret_sql = "select payroll_secret_column from t where x = $1"
    connector = FakeConnector(rows=[(1, Decimal("98765.43"))])
    async with running(tmp_path, connector=connector) as w:
        served = await w.ask(body={"sql": secret_sql, "params": ["param-value-xyz"]})
        w.connector.error = QueryFailedError("payroll_secret_column missing", sqlstate="42703")
        failed = await w.ask(body={"sql": secret_sql, "params": ["param-value-xyz"]})
        odd_id = await w.ask(headers={"x-request-id": "has spaces in it"})
    assert (served.status_code, failed.status_code) == (200, 422)
    for text in ("payroll_secret_column from", "param-value-xyz", "98765.43"):
        assert text not in caplog.text
    served_log, failed_log, odd_log = records(caplog)
    assert served_log | {"request_id": "", "received_at": "", "elapsed_ms": 0} == {
        "request_id": "",
        "connection": "sales",
        "env_id": PROD,
        "snapshot_version": 1,
        "user": None,
        "user_context": "app_only",
        "outcome": "served",
        "rows": 1,
        "bytes": served_log["bytes"],
        "truncated_reason": None,
        "received_at": "",
        "elapsed_ms": 0,
        "instance_started_at": served_log["instance_started_at"],
        "cold": True,
        "ready_ms": None,
    }
    assert (failed_log["outcome"], failed_log["cold"]) == ("QUERY_FAILED", False)
    assert odd_log["request_id"] != "has spaces in it"
    assert odd_id.headers["x-request-id"] == odd_log["request_id"]


async def test_health(tmp_path: Path) -> None:
    async with running(tmp_path) as w:
        r = await w.http.get("/healthz")
    assert (r.status_code, r.json()) == (200, {"status": "ok"})
