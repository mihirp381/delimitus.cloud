"""SSC-024: the cell agent's logs and health against the Cloud Logging and Cloud Run emulators,
directly and as the control plane reaches them: ``AgentCellLogs`` -> agent app -> hub.

Ticket "done when" checks that run here (the live run is SSC-086):
  * ``ssc logs --follow`` shows a new line within 5 seconds -> test_a_follow_sees_a_new_line_...
    (the API and CLI legs: ssc_control's test_logs and ssc_cli's test_logs_follow_...)
Plus: followers share one upstream read and the cell stays under the quota, the fair share per
caller, filters built from the service only, redaction, and the three health states with no
request to the app.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from ssc_agent import cloud_logging
from ssc_agent.app import create_app
from ssc_agent.cloud_logging import CellLogHub, CloudLoggingEntries, query_filter
from ssc_agent.cloud_run import CellRuntime, CloudRunDriver
from ssc_conformance.cloud_logging_emulator import VIEW, CloudLoggingEmulator, parse_filter
from ssc_conformance.cloud_run_emulator import PROJECT, REGION, CloudRunEmulator
from ssc_conformance.contracts.runtime_driver import new_spec
from ssc_contracts.ids import new_id
from ssc_control.runtime.cell_logs import AgentCellLogs
from ssc_shared.logs import LogQuery, LogsNotConfiguredError, LogsRateLimitedError
from ssc_shared.runtime import service_name

FIRST = "sha256:" + "1" * 64
BAD = "sha256:" + "9" * 64
AGENT = "https://ssc-cell-agent.test"
BUILD = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
CELL = CellRuntime(
    project=PROJECT,
    region=REGION,
    network=f"projects/{PROJECT}/global/networks/ssc-cell",
    subnetwork=f"projects/{PROJECT}/regions/{REGION}/subnetworks/apps",
    image_repository=f"{REGION}-docker.pkg.dev/{PROJECT}/ssc-apps/apps",
    invoker=f"ssc-gateway@{PROJECT}.iam.gserviceaccount.com",
)
ALLOWED_HOSTS = {"run.googleapis.com", "iam.googleapis.com", "logging.googleapis.com"}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Cell:
    logging: CloudLoggingEmulator
    run: CloudRunEmulator
    hosts: list[str] = field(default_factory=list[str])
    driver: CloudRunDriver = field(init=False)

    def __post_init__(self) -> None:
        self.driver = CloudRunDriver(CELL, _token, client=self.client())

    def hub(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        follow_interval: float = cloud_logging.FOLLOW_INTERVAL,
    ) -> CellLogHub:
        entries = CloudLoggingEntries(self.logging.views, _token, client=self.client())
        return CellLogHub(entries, self.driver, clock=clock, follow_interval=follow_interval)

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self._route))

    def _route(self, request: httpx2.Request) -> httpx2.Response:
        host = request.url.host
        self.hosts.append(host)
        assert host in ALLOWED_HOSTS, f"health or logs reached {host}"
        if host == "logging.googleapis.com":
            return self.logging.handler(request)
        return self.run.handler(request)


async def _token() -> str:
    return "access-token"


@pytest.fixture
async def cell() -> AsyncIterator[Cell]:
    made = Cell(CloudLoggingEmulator(), CloudRunEmulator(auto_settle=True))
    yield made
    await made.driver.aclose()


def _app(service: str | None = None) -> LogQuery:
    return LogQuery(service=service or service_name(new_id("env")), source="app")


async def _agent_logs(hub: CellLogHub, driver: CloudRunDriver) -> AgentCellLogs:
    async def id_token(_: str) -> str:
        return "id-token"

    org = "org_" + "l" * 20
    transport = httpx2.ASGITransport(app=create_app(driver, logs=hub, org_id=org))
    client = httpx2.AsyncClient(transport=transport)
    return AgentCellLogs(AGENT, id_token, org_id=org, client=client)


# ── follow ───────────────────────────────────────────────────────────────────


async def test_a_follow_sees_a_new_line_within_five_seconds(cell: Cell) -> None:
    query = _app()
    logs = await _agent_logs(cell.hub(), cell.driver)
    cell.logging.app_line(query.service, "before")
    page = await logs.read(query, since_seconds=600, limit=100, caller="org/alice")
    assert [line.text for line in page.lines] == ["before"]

    async def later() -> None:
        await asyncio.sleep(0.5)
        cell.logging.app_line(query.service, "hello from the app")

    started = time.monotonic()
    writer = asyncio.create_task(later())
    cursor = page.cursor
    seen: list[str] = []
    while not seen and time.monotonic() - started < 5:
        nxt = await logs.follow(query, cursor=cursor, wait_seconds=5, caller="org/alice")
        seen = [line.text for line in nxt.lines]
        cursor = nxt.cursor
    await writer
    assert seen == ["hello from the app"]
    assert time.monotonic() - started < 5
    again = await logs.follow(query, cursor=cursor, wait_seconds=0, caller="org/alice")
    assert again.lines == ()


async def test_many_followers_share_one_upstream_read(cell: Cell) -> None:
    interval = 0.05
    hub = cell.hub(follow_interval=interval)
    queries = [_app() for _ in range(5)]
    for query in queries:
        cell.logging.app_line(query.service, f"old line of {query.service}")
    started = time.monotonic()
    cursors = {
        q: (await hub.read(q, since_seconds=60, limit=10, caller=f"seed/{q.service}")).cursor
        for q in queries
    }
    before = len(cell.logging.calls)

    async def write() -> None:
        for n in range(10):
            await asyncio.sleep(0.05)
            for query in queries:
                cell.logging.app_line(query.service, f"{query.service} line {n}")

    async def follower(query: LogQuery, who: str) -> list[str]:
        cursor, got = cursors[query], list[str]()
        deadline = time.monotonic() + 3
        while len(got) < 10 and time.monotonic() < deadline:
            page = await hub.follow(query, cursor=cursor, wait_seconds=1, caller=who)
            got += [line.text for line in page.lines]
            cursor = page.cursor
        return got

    tasks = [follower(q, f"org/user-{i}-{n}") for i, q in enumerate(queries) for n in range(3)]
    results = await asyncio.gather(write(), *tasks)
    elapsed = time.monotonic() - started
    for (i, query), got in zip(
        [(i, q) for i, q in enumerate(queries) for _ in range(3)], results[1:], strict=True
    ):
        assert got == [f"{query.service} line {n}" for n in range(10)], i
    follow_calls = cell.logging.calls[before:]
    assert len(follow_calls) <= elapsed / interval + 1
    assert len(follow_calls) < 15 * 10
    assert all(q.service in follow_calls[-1]["filter"] for q in queries)


async def test_the_cell_stays_under_the_quota_whatever_the_callers(cell: Cell) -> None:
    clock = FakeClock()
    hub = cell.hub(clock=clock)
    query = _app()
    ok = limited = 0
    for n in range(100):
        try:
            await hub.read(query, since_seconds=60 + n, limit=10, caller=f"org/user-{n}")
            ok += 1
        except LogsRateLimitedError as exc:
            assert exc.retry_after >= 1
            limited += 1
    assert (ok, limited) == (cloud_logging.READ_BURST, 100 - cloud_logging.READ_BURST)
    clock.t += 60
    for n in range(100):
        try:
            await hub.read(query, since_seconds=600 + n, limit=10, caller=f"org/other-{n}")
        except LogsRateLimitedError:
            pass
    assert len(cell.logging.calls) <= cloud_logging.READ_BURST + cloud_logging.READS_PER_MINUTE
    worst = 60 / cloud_logging.FOLLOW_INTERVAL + cloud_logging.READS_PER_MINUTE
    assert worst + cloud_logging.READ_BURST < 60


async def test_one_caller_gets_a_fair_share_and_cache_hits_are_free(cell: Cell) -> None:
    clock = FakeClock()
    hub = cell.hub(clock=clock)
    query = _app()
    for since in (60, 120, 180):
        await hub.read(query, since_seconds=since, limit=10, caller="org/greedy")
    with pytest.raises(LogsRateLimitedError) as refused:
        await hub.read(query, since_seconds=240, limit=10, caller="org/greedy")
    assert refused.value.retry_after == 10
    await hub.read(query, since_seconds=60, limit=10, caller="org/greedy")
    assert len(cell.logging.calls) == 3
    await hub.read(query, since_seconds=240, limit=10, caller="org/patient")
    clock.t += 10
    await hub.read(query, since_seconds=300, limit=10, caller="org/greedy")


async def test_one_caller_follows_at_most_three_at_once(cell: Cell) -> None:
    hub = cell.hub()
    queries = [_app() for _ in range(4)]
    waits = [
        asyncio.create_task(hub.follow(q, cursor=None, wait_seconds=1, caller="org/many"))
        for q in queries[:3]
    ]
    await asyncio.sleep(0.05)
    with pytest.raises(LogsRateLimitedError):
        await hub.follow(queries[3], cursor=None, wait_seconds=1, caller="org/many")
    await hub.follow(queries[3], cursor=None, wait_seconds=0, caller="org/someone-else")
    await asyncio.gather(*waits)


async def test_upstream_429_backs_the_cell_off(cell: Cell) -> None:
    hub = cell.hub()
    cell.logging.refuse = 429
    with pytest.raises(LogsRateLimitedError) as refused:
        await hub.read(_app(), since_seconds=60, limit=10, caller="org/a")
    assert refused.value.retry_after == cloud_logging.BACKOFF_SECONDS
    cell.logging.refuse = None
    with pytest.raises(LogsRateLimitedError):
        await hub.read(_app(), since_seconds=60, limit=10, caller="org/b")
    assert len(cell.logging.calls) == 1


# ── filters and redaction ────────────────────────────────────────────────────


async def test_only_the_named_service_and_builds_are_read(cell: Cell) -> None:
    hub = cell.hub()
    mine, other = _app(), _app()
    cell.logging.app_line(mine.service, "mine")
    cell.logging.app_line(other.service, "not mine")
    cell.logging.build_line(BUILD, "Step 1/3")
    cell.logging.build_line("11111111-2222-3333-4444-555555555555", "another build")
    page = await hub.read(mine, since_seconds=60, limit=10, caller="org/a")
    assert [line.text for line in page.lines] == ["mine"]
    builds = LogQuery(service=mine.service, source="build", builds=(BUILD,))
    page = await hub.read(builds, since_seconds=60, limit=10, caller="org/a")
    assert [(line.source, line.text) for line in page.lines] == [("build", "Step 1/3")]
    for call in cell.logging.calls:
        assert other.service not in call["filter"]
        assert call["resourceNames"] == [VIEW]
    for bad in ('ssc-a-x" OR resource.type="gce_instance', "ssc-b-" + "a" * 20):
        with pytest.raises(ValueError, match="service name"):
            LogQuery(service=bad, source="app")
    with pytest.raises(ValueError, match="Cloud Build"):
        LogQuery(service=mine.service, source="build", builds=('x") OR ("y',))


async def test_every_log_view_is_read_and_only_views_are_taken() -> None:
    builds = VIEW.replace("ssc-app-logs", "ssc-build-logs")
    cell = Cell(CloudLoggingEmulator(views=(VIEW, builds)), CloudRunEmulator(auto_settle=True))
    query = _app()
    cell.logging.app_line(query.service, "two views")
    page = await cell.hub().read(query, since_seconds=60, limit=10, caller="org/a")
    assert [line.text for line in page.lines] == ["two views"]
    assert cell.logging.calls[-1]["resourceNames"] == [VIEW, builds]
    await cell.driver.aclose()
    for bad in ((), (VIEW, "projects/p/logs/run")):
        with pytest.raises(ValueError, match="log view"):
            CloudLoggingEntries(bad, _token)


async def test_no_builds_reads_nothing(cell: Cell) -> None:
    hub = cell.hub()
    page = await hub.read(
        LogQuery(service=_app().service, source="build"), since_seconds=60, limit=5, caller="org/a"
    )
    assert page.lines == ()
    assert cell.logging.calls == []


async def test_every_line_is_redacted(cell: Cell) -> None:
    query = _app()
    logs = await _agent_logs(cell.hub(), cell.driver)
    cell.logging.app_line(query.service, "connecting with password=fake-pass-1234 now")
    cell.logging.app_line(query.service, "Authorization: Bearer fake.token.value-abcdef")
    page = await logs.read(query, since_seconds=60, limit=10, caller="org/a")
    texts = [line.text for line in page.lines]
    assert all("fake-pass-1234" not in t and "value-abcdef" not in t for t in texts)
    assert all("[redacted]" in t for t in texts)


async def test_request_logs_read_as_one_line_without_query_strings(cell: Cell) -> None:
    query = _app()
    entry = cell.logging.request(query.service, 200, path="/orders?token=fake-secret-q")
    assert "fake-secret-q" in entry["httpRequest"]["requestUrl"]
    page = await cell.hub().read(query, since_seconds=60, limit=10, caller="org/a")
    assert [line.text for line in page.lines] == ["GET /orders 200 0.012s"]


def test_the_emulator_reads_or_tighter_than_and() -> None:
    entry = {"severity": "INFO", "a": "1", "b": "2"}
    assert parse_filter('a="1" AND b="9" OR b="2"')(entry)
    assert not parse_filter('a="9" AND b="9" OR b="2"')(entry)
    assert query_filter(_app("ssc-a-" + "a" * 20)).count('"') == 4


async def test_without_a_log_view_reads_are_refused(cell: Cell) -> None:
    hub = CellLogHub(None, cell.driver)
    with pytest.raises(LogsNotConfiguredError):
        await hub.read(_app(), since_seconds=60, limit=10, caller="org/a")
    with pytest.raises(LogsNotConfiguredError):
        await hub.follow(_app(), cursor=None, wait_seconds=0, caller="org/a")


# ── health ───────────────────────────────────────────────────────────────────


async def _deployed(cell: Cell, digest: str = FIRST, **changes: int) -> str:
    spec = replace(new_spec(digest), **changes)
    await cell.driver.apply(spec)
    cell.run.settle()
    return spec.service


async def test_health_has_three_states_and_never_calls_the_app(cell: Cell) -> None:
    now = datetime.now(UTC)
    clock = FakeClock()
    hub = cell.hub(clock=clock)
    asleep = await _deployed(cell)
    cell.logging.request(asleep, 200, at=now - timedelta(minutes=40))
    running = await _deployed(cell)
    cell.logging.request(running, 200, at=now - timedelta(minutes=2))
    erroring = await _deployed(cell)
    cell.logging.request(erroring, 200, at=now - timedelta(minutes=5))
    for minutes in (3, 2, 1):
        cell.logging.request(erroring, 503, at=now - timedelta(minutes=minutes))
    one_error = await _deployed(cell)
    cell.logging.request(one_error, 500, at=now - timedelta(minutes=1))
    blip = await _deployed(cell)
    for minutes, status in ((4, 200), (3, 503), (2, 200), (1, 503)):
        cell.logging.request(blip, status, at=now - timedelta(minutes=minutes))
    crashed = await _deployed(cell)
    cell.logging.request(crashed, 200, at=now - timedelta(minutes=3))
    cell.logging.system_error(crashed, "Container called exit(1).", at=now - timedelta(minutes=1))
    cell.hosts.clear()

    seen = {}
    for service in (asleep, running, erroring, one_error, blip, crashed):
        clock.t += 60
        health = await hub.health(service, caller="org/a")
        seen[service] = (health.state, health.reason)
    assert seen == {
        asleep: ("asleep", "idle"),
        running: ("running", "serving"),
        erroring: ("failing", "server_error"),
        one_error: ("failing", "server_error"),
        blip: ("running", "serving"),
        crashed: ("failing", "crashed"),
    }
    health = await hub.health(running, caller="org/a")
    assert health.last_request_at is not None
    assert now - health.last_request_at < timedelta(minutes=3)
    assert set(cell.hosts) <= {"run.googleapis.com", "logging.googleapis.com"}
    assert not any(h.endswith(".run.app") or h.endswith(".internal") for h in cell.hosts)


async def test_health_of_a_failed_revision_stopped_and_missing_services(cell: Cell) -> None:
    hub = cell.hub()
    cell.run.unhealthy(BAD)
    broken = await _deployed(cell, BAD)
    health = await hub.health(broken, caller="org/a")
    assert (health.state, health.reason) == ("failing", "revision_failed")
    stopped = await _deployed(cell)
    await cell.driver.scale_to_zero(stopped)
    cell.run.settle()
    health = await hub.health(stopped, caller="org/a")
    assert (health.state, health.reason) == (None, "stopped")
    health = await hub.health(service_name(new_id("env")), caller="org/a")
    assert (health.state, health.reason) == (None, "not_deployed")


async def test_health_without_logs_says_it_cannot_tell(cell: Cell) -> None:
    service = await _deployed(cell)
    health = await CellLogHub(None, cell.driver).health(service, caller="org/a")
    assert (health.state, health.reason) == (None, "logs_unavailable")
    warm = await _deployed(cell, min_instances=1)
    health = await CellLogHub(None, cell.driver).health(warm, caller="org/a")
    assert (health.state, health.reason) == ("running", "always_on")


async def test_health_reads_are_cached_and_through_the_agent(cell: Cell) -> None:
    service = await _deployed(cell)
    cell.logging.request(service, 200)
    logs = await _agent_logs(cell.hub(), cell.driver)
    for _ in range(5):
        health = await logs.health(service, caller="org/a")
        assert health.state == "running"
    assert len(cell.logging.calls) == 1
