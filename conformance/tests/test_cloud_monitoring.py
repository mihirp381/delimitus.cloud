"""SSC-028: the cell agent's usage reads against the Cloud Monitoring emulator, directly and as the
control plane reaches them: ``AgentCellUsage`` -> agent app -> ``CellUsageReader``.

The control plane's leg (usage events, the API, the report) is ssc_control's test_usage_metrics.
Here: one read is three calls for the whole cell, even at 200 apps; pages are followed; only SSC
app services count; busy minutes, instance time and cold starts come out as the API gives them;
nothing but Monitoring is called; and each refusal reaches the control plane as it should.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx2
import pytest

from ssc_agent.app import create_app
from ssc_agent.cloud_monitoring import (
    MAX_PAGES,
    CellUsageReader,
    CloudMonitoringSeries,
    usage_report,
)
from ssc_agent.cloud_run import CellRuntime, CloudRunDriver
from ssc_conformance.cloud_monitoring_emulator import CloudMonitoringEmulator
from ssc_conformance.cloud_run_emulator import PROJECT, REGION, CloudRunEmulator
from ssc_contracts.ids import new_id
from ssc_control.runtime.cell_usage import AgentCellUsage
from ssc_shared.runtime import service_name
from ssc_shared.usage import (
    UsageError,
    UsageNotConfiguredError,
    UsageReport,
    UsageWindow,
    report_from_wire,
    report_to_wire,
    window_from_wire,
    window_to_wire,
)

AGENT = "https://ssc-cell-agent.test"
CELL = CellRuntime(
    project=PROJECT,
    region=REGION,
    network=f"projects/{PROJECT}/global/networks/ssc-cell",
    subnetwork=f"projects/{PROJECT}/regions/{REGION}/subnetworks/apps",
    image_repository=f"{REGION}-docker.pkg.dev/{PROJECT}/ssc-apps/apps",
    invoker=f"ssc-gateway@{PROJECT}.iam.gserviceaccount.com",
)
DAY = datetime(2026, 9, 14, tzinfo=UTC)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute, second=second)


def window(start: int, end: int) -> UsageWindow:
    return UsageWindow(start=at(start), end=at(end))


@dataclass
class Cell:
    monitoring: CloudMonitoringEmulator
    hosts: list[str] = field(default_factory=list[str])

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self._route))

    def reader(self) -> CellUsageReader:
        return CellUsageReader(CloudMonitoringSeries(PROJECT, _token, client=self.client()))

    def _route(self, request: httpx2.Request) -> httpx2.Response:
        self.hosts.append(request.url.host)
        assert request.url.host == "monitoring.googleapis.com", f"usage reached {request.url}"
        return self.monitoring.handler(request)


async def _token() -> str:
    return "access-token"


async def _id_token(_: str) -> str:
    return "id-token"


@pytest.fixture
def cell() -> Cell:
    return Cell(CloudMonitoringEmulator(now=lambda: at(12)))


@pytest.fixture
async def agent(cell: Cell) -> AsyncIterator[AgentCellUsage]:
    driver = CloudRunDriver(
        CELL,
        _token,
        client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(CloudRunEmulator(auto_settle=True).handler)
        ),
    )
    app = create_app(driver, usage=cell.reader())
    usage = AgentCellUsage(
        AGENT, _id_token, client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app))
    )
    yield usage
    await usage.aclose()
    await driver.aclose()


def _service() -> str:
    return service_name(new_id("env"))


async def test_a_session_open_an_hour_reads_as_one_busy_hour_and_its_instance_time(
    cell: Cell, agent: AgentCellUsage
) -> None:
    svc = _service()
    cell.monitoring.instance(svc, started=at(10), stopped=at(11), startup_ms=4000)
    cell.monitoring.busy(svc, start=at(10, 0, 5), end=at(11))
    report = await agent.read(window(10, 11))
    ((hour,), (start,)) = report.hours, report.cold_starts
    assert (hour.service, hour.hour, hour.instance_seconds, hour.active_seconds) == (
        svc,
        at(10),
        3600.0,
        3600,
    )
    assert (start.minute, start.count, start.duration_ms) == (at(10), 1, 4000.0)
    assert set(cell.hosts) == {"monitoring.googleapis.com"}


async def test_two_hundred_apps_are_read_in_three_calls(cell: Cell) -> None:
    services = [_service() for _ in range(200)]
    for svc in services:
        cell.monitoring.instance(svc, started=at(10, 5), stopped=at(10, 20), startup_ms=900)
        cell.monitoring.busy(svc, start=at(10, 6), end=at(10, 8))
    report = await cell.reader().read(window(10, 11))
    assert len(cell.monitoring.calls) == 3
    assert {h.service for h in report.hours} == set(services)
    assert {(h.instance_seconds, h.active_seconds) for h in report.hours} == {(900.0, 120)}
    assert len(report.cold_starts) == 200
    for call in cell.monitoring.calls:
        assert 'resource.labels.service_name = starts_with("ssc-a-")' in call["filter"]
        assert call["aggregation.crossSeriesReducer"] == "REDUCE_SUM"
        assert call["aggregation.groupByFields"] == "resource.labels.service_name"


async def test_pages_are_followed(cell: Cell) -> None:
    cell.monitoring.series_per_page = 2
    services = [_service() for _ in range(5)]
    for svc in services:
        cell.monitoring.instance(svc, started=at(10), stopped=at(10, 30), startup_ms=1000)
    report = await cell.reader().read(window(10, 11))
    assert {h.service for h in report.hours} == set(services)
    assert len(cell.monitoring.calls) == 3 * 3
    assert [c.get("pageToken") for c in cell.monitoring.calls[:3]] == [None, "2", "4"]


async def test_too_many_pages_is_an_error(cell: Cell) -> None:
    cell.monitoring.series_per_page = 1
    for _ in range(MAX_PAGES + 1):
        cell.monitoring.instance(_service(), started=at(10), stopped=at(10, 1), startup_ms=1)
    with pytest.raises(UsageError, match="pages"):
        await cell.reader().read(window(10, 11))


async def test_other_services_in_the_project_are_not_counted(cell: Cell) -> None:
    cell.monitoring.instance("ssc-cell-agent", started=at(10), stopped=at(11), startup_ms=1)
    report = await cell.reader().read(window(10, 11))
    assert report == UsageReport(hours=(), cold_starts=())


async def test_a_request_billed_app_counts_only_its_start_and_busy_seconds(cell: Cell) -> None:
    svc = _service()
    cell.monitoring.instance(
        svc, started=at(10), stopped=at(10, 15), startup_ms=2000, billing="request"
    )
    cell.monitoring.busy(svc, start=at(10, 0, 2), end=at(10, 0, 12))
    ((hour,),) = [(await cell.reader().read(window(10, 11))).hours]
    assert (hour.instance_seconds, hour.active_seconds) == (12.0, 60)


async def test_starts_in_one_minute_give_their_count_and_mean(cell: Cell) -> None:
    svc = _service()
    for revision, ms in (("r1", 1000), ("r2", 3000)):
        cell.monitoring.instance(
            svc, started=at(10, 7), stopped=at(10, 9), startup_ms=ms, revision=revision
        )
    (start,) = (await cell.reader().read(window(10, 11))).cold_starts
    assert (start.minute, start.count, start.duration_ms) == (at(10, 7), 2, 2000.0)


async def test_points_not_yet_written_are_not_read() -> None:
    """Why the control plane waits ``collect.LAG`` after an hour ends before reading it."""
    svc = _service()
    seen: list[float] = []
    for now in (at(11, 30), at(12), at(12, 15)):
        monitoring = CloudMonitoringEmulator(now=lambda now=now: now)
        monitoring.instance(svc, started=at(11), stopped=at(12), startup_ms=1)
        ((hour,),) = [(await Cell(monitoring).reader().read(window(11, 12))).hours]
        seen.append(hour.instance_seconds)
    assert seen == [27 * 60.0, 57 * 60.0, 3600.0]


def test_odd_answers_are_skipped_not_trusted() -> None:
    end = "2026-09-14T11:00:00Z"
    svc = _service()

    def series(name: object, value: dict[str, object], end_time: object = end) -> dict[str, object]:
        return {
            "resource": {"labels": {"service_name": name}},
            "points": [{"interval": {"endTime": end_time}, "value": value}],
        }

    instance = [
        series(svc, {"doubleValue": "NaN"}),
        series(svc, {"doubleValue": -5}),
        series("not-ours", {"doubleValue": 99.0}),
        series(svc, {"doubleValue": 7.0}, end_time=None),
        series(svc, {"doubleValue": 3.0}, end_time="yesterday"),
        series(svc, {"doubleValue": 10.0}),
    ]
    starts = [series(svc, {"distributionValue": {"count": "0", "mean": 5.0}})]
    report = usage_report(window(10, 11), instance, [], starts)
    assert [(h.service, h.instance_seconds) for h in report.hours] == [(svc, 10.0)]
    assert report.cold_starts == ()


async def test_without_a_source_the_agent_says_usage_is_not_configured(cell: Cell) -> None:
    app = create_app(_NoDriver(), usage=None)  # pyright: ignore[reportArgumentType]
    usage = AgentCellUsage(
        AGENT, _id_token, client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app))
    )
    with pytest.raises(UsageNotConfiguredError):
        await usage.read(window(10, 11))
    await usage.aclose()
    assert cell.monitoring.calls == []


async def test_a_403_is_not_configured_and_other_failures_are_errors(
    cell: Cell, agent: AgentCellUsage
) -> None:
    cell.monitoring.refuse = 403
    with pytest.raises(UsageNotConfiguredError):
        await agent.read(window(10, 11))
    for status in (429, 500):
        cell.monitoring.refuse = status
        with pytest.raises(UsageError) as caught:
            await agent.read(window(10, 11))
        assert not isinstance(caught.value, UsageNotConfiguredError)


async def test_the_agent_refuses_a_malformed_window() -> None:
    app = create_app(_NoDriver(), usage=CellUsageReader(None))  # pyright: ignore[reportArgumentType]
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=AGENT) as client:
        bad = {"start": at(10, 30).isoformat(), "end": at(11).isoformat()}
        for body in ({"window": bad}, {"window": {"start": "x"}}, ["no"], {}):
            r = await client.post("/v1/usage/read", json=body)
            assert (r.status_code, r.json()["code"]) == (400, "INVALID_REQUEST")
        assert (await client.post("/v1/usage/write", json={})).status_code == 404


def test_windows_are_whole_hours_up_to_six() -> None:
    for start, end in ((at(10, 30), at(11)), (at(10), at(10)), (at(1), at(8))):
        with pytest.raises(ValueError, match="window"):
            UsageWindow(start=start, end=end)
    assert window_from_wire(window_to_wire(window(4, 10))) == window(4, 10)
    with pytest.raises(ValueError, match="time zone"):
        window_from_wire({"start": "2026-09-14T10:00:00", "end": "2026-09-14T11:00:00"})


async def test_a_report_survives_the_wire(cell: Cell) -> None:
    svc = _service()
    cell.monitoring.instance(svc, started=at(10, 1), stopped=at(10, 40), startup_ms=1234.5)
    cell.monitoring.busy(svc, start=at(10, 2), end=at(10, 3))
    report = await cell.reader().read(window(10, 11))
    assert report_from_wire(report_to_wire(report)) == report
    with pytest.raises(ValueError, match="malformed"):
        report_from_wire({"hours": [{"service": svc}], "cold_starts": []})
    with pytest.raises(ValueError, match="service"):
        report_from_wire(
            {
                "hours": [
                    {
                        "service": "x",
                        "hour": at(10).isoformat(),
                        "instance_seconds": 1.0,
                        "active_seconds": 0,
                    }
                ],
                "cold_starts": [],
            }
        )


class _NoDriver:
    """The usage routes never touch the runtime driver."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the usage route used the driver's {name}")
