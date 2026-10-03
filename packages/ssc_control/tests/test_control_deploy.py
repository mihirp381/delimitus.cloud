"""SSC-064: what the control-plane deploy runs. The API's process entry point, and the directory
sync registered in the worker.

Ticket "done when" checks:
  * the sync runs every minute in the worker -> test_the_worker_syncs_every_directory_every_minute
        and test_one_pass_syncs_every_connected_org (the live minute is the SSC-064 runbook)
"""

from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from ssc_testkit import ISSUER, Dsns
from test_identity import FOUNDER_IDP, new_world

from ssc_control.api import __main__ as api_main
from ssc_control.db import NewOrg, create_org, make_engine
from ssc_control.identity import jobs as identity_jobs
from ssc_control.identity.workos import WorkOSClient
from ssc_control.worker import CompositionError, build_app, compose_ports

DSN = "postgresql://ssc_app@localhost/ssc"


def test_the_worker_syncs_every_directory_every_minute() -> None:
    app = build_app(DSN)
    assert identity_jobs.SYNC_TASK in app.tasks
    ((periodic,),) = [
        [
            p
            for key, p in app.periodic_registry.periodic_tasks.items()
            if key[0] == identity_jobs.SYNC_TASK
        ]
    ]
    ticks = [periodic.croniter.get_next(float, start_time=1_790_000_000.0)]
    for _ in range(5):
        ticks.append(periodic.croniter.get_next(float, start_time=ticks[-1]))
    assert {b - a for a, b in zip(ticks, ticks[1:], strict=False)} == {60.0}
    assert app.tasks[identity_jobs.SYNC_TASK].lock == "directory_sync"


async def test_the_sync_reads_workos_with_both_settings_or_none() -> None:
    base = {"SSC_DATABASE_DSN": DSN}
    assert compose_ports(base).directory is None
    for one in ({"SSC_WORKOS_API_KEY": "k"}, {"SSC_WORKOS_CLIENT_ID": "c"}):
        with pytest.raises(CompositionError, match="SSC_WORKOS_API_KEY"):
            compose_ports({**base, **one})
    ports = compose_ports({**base, "SSC_WORKOS_API_KEY": "k", "SSC_WORKOS_CLIENT_ID": "c"})
    assert isinstance(ports.directory, WorkOSClient)
    await ports.directory.aclose()


async def test_one_pass_syncs_every_connected_org(
    dsns: Dsns, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await new_world(dsns)
    plain = NewOrg("Plain", "Bo Plain", "bo@example.com", ISSUER, "usr_plain")
    unconnected = (await create_org(world.engine, plain)).org_id

    async def these_orgs(engine: AsyncEngine) -> list[str]:
        """Only this test's orgs: the database is shared with other tests."""
        return [world.org, unconnected]

    monkeypatch.setattr(identity_jobs, "all_org_ids", these_orgs)
    client = world.wo.client()
    try:
        assert await identity_jobs.sync_all(world.engine, client) == 1
        assert (await world.person(FOUNDER_IDP))[0] == world.founder
        real_tick = identity_jobs.sync.tick

        async def tick(engine: AsyncEngine, c: WorkOSClient, org_id: str) -> Any:
            """The plain org's tick raises; the connected org must still sync."""
            if org_id == unconnected:
                raise RuntimeError("boom")
            return await real_tick(engine, c, org_id)

        monkeypatch.setattr(identity_jobs.sync, "tick", tick)
        world.wo.fail = 500
        assert await identity_jobs.sync_all(world.engine, client) == 1
        (row,) = await world.rows(
            "select last_error from ssc.directory_connection where org_id = :org"
        )
        assert row[0] is not None and "500" in row[0]
    finally:
        await client.aclose()
        await world.engine.dispose()


def test_the_api_process_serves_the_app_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: dict[str, Any] = {}
    monkeypatch.setattr(api_main.uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))
    for key, value in {
        "SSC_DATABASE_DSN": DSN,
        "SSC_API_JWKS": '{"keys": []}',
        "SSC_API_ISSUER": "https://auth.delimitus.com",
        "PORT": "8123",
    }.items():
        monkeypatch.setenv(key, value)
    assert api_main.main() == 0
    assert isinstance(served["app"], FastAPI)
    assert (served["host"], served["port"], served["proxy_headers"]) == ("0.0.0.0", 8123, True)


def test_the_engine_url_takes_the_cloud_sql_socket() -> None:
    engine = make_engine("postgresql://ssc_app:pw@/ssc?host=/cloudsql/p:us-central1:ssc-control")
    assert engine.url.query["host"] == "/cloudsql/p:us-central1:ssc-control"
    assert (engine.url.database, engine.url.host) == ("ssc", None)
