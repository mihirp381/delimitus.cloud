"""App usage from the cell's Cloud Monitoring (SSC-028): instance time, busy minutes, cold starts.

Cloud Run writes these metrics itself, for every revision, whether or not anyone asks; reading
them sends nothing to an app, so it never wakes one or bills it. One read is three
``timeSeries.list`` calls for every SSC app service in the project at once, summed per service
by Monitoring:

- ``container/billable_instance_time``, summed per hour: instance seconds;
- ``container/instance_count`` with ``state = active``, the most per minute: busy minutes;
- ``container/startup_latencies``, per minute: how many instances started and their mean time.

A cell of 200 apps fits each call in one page, so collecting every hour costs 3 calls an hour.
"""

import math
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, cast

import httpx2

from ssc_shared.runtime import SERVICE_NAME
from ssc_shared.usage import (
    HOUR,
    MINUTE,
    CellUsage,
    ColdStart,
    ServiceHour,
    UsageError,
    UsageNotConfiguredError,
    UsageReport,
    UsageWindow,
)

type AccessTokens = Callable[[], Awaitable[str]]
type Json = dict[str, Any]

MONITORING_API: Final = "https://monitoring.googleapis.com/v3"
INSTANCE_TIME: Final = "run.googleapis.com/container/billable_instance_time"
INSTANCE_COUNT: Final = "run.googleapis.com/container/instance_count"
STARTUP_LATENCIES: Final = "run.googleapis.com/container/startup_latencies"
SERVICES: Final = (
    'resource.type = "cloud_run_revision" AND resource.labels.service_name = starts_with("ssc-a-")'
)
CALL_TIMEOUT_SECONDS: Final = 30.0
PAGE_SIZE: Final = 100_000
MAX_PAGES: Final = 10
_HTTP_FORBIDDEN: Final = 403
_HTTP_TOO_MANY: Final = 429
_HTTP_BAD_REQUEST: Final = 400


class TimeSeries(Protocol):
    """``timeSeries.list`` over the cell project: every page's series."""

    async def list(self, params: Mapping[str, str]) -> list[Json]: ...


class CloudMonitoringSeries(TimeSeries):
    """The Monitoring API v3, reading the cell's own project."""

    def __init__(
        self, project: str, tokens: AccessTokens, *, client: httpx2.AsyncClient | None = None
    ) -> None:
        self._url = f"{MONITORING_API}/projects/{project}/timeSeries"
        self._tokens = tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list(self, params: Mapping[str, str]) -> list[Json]:
        series: list[Json] = []
        token = ""
        for _ in range(MAX_PAGES):
            query = {**params, "pageSize": str(PAGE_SIZE)} | ({"pageToken": token} if token else {})
            body = await self._page(query)
            found = body.get("timeSeries")
            if isinstance(found, list):
                series.extend(_obj(s) for s in cast("list[object]", found))
            next_token = body.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                return series
            token = next_token
        raise UsageError(f"timeSeries.list: more than {MAX_PAGES} pages")

    async def _page(self, query: Mapping[str, str]) -> Json:
        headers = {"Authorization": f"Bearer {await self._tokens()}"}
        try:
            response = await self._client.get(self._url, params=dict(query), headers=headers)
        except httpx2.HTTPError as exc:
            raise UsageError(f"timeSeries.list: {type(exc).__name__}") from None
        if response.status_code == _HTTP_FORBIDDEN:
            raise UsageNotConfiguredError("the agent may not read Cloud Monitoring in this project")
        if response.status_code == _HTTP_TOO_MANY:
            raise UsageError("Cloud Monitoring's read quota is spent")
        if response.status_code >= _HTTP_BAD_REQUEST:
            raise UsageError(f"timeSeries.list: HTTP {response.status_code}")
        return _obj(response.json())


class CellUsageReader(CellUsage):
    """Usage of every SSC app service in the cell; refuses with no ``series`` to read."""

    def __init__(self, series: TimeSeries | None) -> None:
        self._series = series

    async def read(self, window: UsageWindow) -> UsageReport:
        if self._series is None:
            raise UsageNotConfiguredError("this agent has no usage source")
        instance = await self._series.list(
            params(window, INSTANCE_TIME, "ALIGN_SUM", HOUR, reducer="REDUCE_SUM")
        )
        active = await self._series.list(
            params(
                window,
                INSTANCE_COUNT,
                "ALIGN_MAX",
                MINUTE,
                reducer="REDUCE_SUM",
                extra='metric.labels.state = "active"',
            )
        )
        starts = await self._series.list(
            params(window, STARTUP_LATENCIES, "ALIGN_DELTA", MINUTE, reducer="REDUCE_SUM")
        )
        return usage_report(window, instance, active, starts)


def params(  # noqa: PLR0913  (keyword-only)
    window: UsageWindow,
    metric: str,
    aligner: str,
    period: timedelta,
    *,
    reducer: str,
    extra: str = "",
) -> dict[str, str]:
    """One query over every SSC app service, one value per service and ``period``."""
    filter_ = f'metric.type = "{metric}" AND {SERVICES}' + (f" AND {extra}" if extra else "")
    return {
        "filter": filter_,
        "interval.startTime": _rfc3339(window.start),
        "interval.endTime": _rfc3339(window.end),
        "aggregation.alignmentPeriod": f"{int(period.total_seconds())}s",
        "aggregation.perSeriesAligner": aligner,
        "aggregation.crossSeriesReducer": reducer,
        "aggregation.groupByFields": "resource.labels.service_name",
        "view": "FULL",
    }


def usage_report(
    window: UsageWindow, instance: list[Json], active: list[Json], starts: list[Json]
) -> UsageReport:
    """The three answers as hours per service and cold starts per minute, inside ``window``."""
    seconds: dict[tuple[str, datetime], float] = defaultdict(float)
    for service, end, value in _points(instance):
        start = end - HOUR
        if window.start <= start < window.end:
            seconds[service, _floor(start, HOUR)] += _double(value)
    busy: dict[tuple[str, datetime], set[datetime]] = defaultdict(set)
    for service, end, value in _points(active):
        minute = _floor(end - MINUTE, MINUTE)
        if window.start <= minute < window.end and _int(value.get("int64Value")) > 0:
            busy[service, _floor(minute, HOUR)].add(minute)
    started: dict[tuple[str, datetime], tuple[int, float]] = {}
    for service, end, value in _points(starts):
        minute = _floor(end - MINUTE, MINUTE)
        dist = _obj(value.get("distributionValue"))
        count = _int(dist.get("count"))
        if count < 1 or not window.start <= minute < window.end:
            continue
        ms = _double({"doubleValue": dist.get("mean")})
        before, total = started.get((service, minute), (0, 0.0))
        started[service, minute] = (before + count, total + ms * count)
    hours = tuple(
        ServiceHour(
            service=service,
            hour=hour,
            instance_seconds=round(seconds.get((service, hour), 0.0), 3),
            active_seconds=min(60 * len(busy.get((service, hour), ())), 3600),
        )
        for service, hour in sorted(set(seconds) | set(busy))
    )
    cold = tuple(
        ColdStart(service=service, minute=minute, count=count, duration_ms=round(total / count, 1))
        for (service, minute), (count, total) in sorted(started.items())
    )
    return UsageReport(hours=hours, cold_starts=cold)


def _points(series: list[Json]) -> list[tuple[str, datetime, Json]]:
    out: list[tuple[str, datetime, Json]] = []
    for one in series:
        service = _obj(_obj(one.get("resource")).get("labels")).get("service_name")
        if not isinstance(service, str) or SERVICE_NAME.fullmatch(service) is None:
            continue
        points = one.get("points")
        for point in cast("list[object]", points) if isinstance(points, list) else []:
            p = _obj(point)
            end = _obj(p.get("interval")).get("endTime")
            if not isinstance(end, str):
                continue
            try:
                moment = datetime.fromisoformat(end)
            except ValueError:
                continue
            moment = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
            out.append((service, moment.astimezone(UTC), _obj(p.get("value"))))
    return out


def _floor(moment: datetime, step: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return epoch + ((moment - epoch) // step) * step


def _double(value: Json) -> float:
    for key in ("doubleValue", "int64Value"):
        raw = value.get(key)
        if raw is None:
            continue
        try:
            number = float(raw)
        except TypeError, ValueError:
            return 0.0
        return number if math.isfinite(number) and number >= 0 else 0.0
    return 0.0


def _int(raw: object) -> int:
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return 0


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _obj(value: object) -> Json:
    return cast(Json, value) if isinstance(value, dict) else {}


__all__ = ["CellUsageReader", "CloudMonitoringSeries", "TimeSeries", "params", "usage_report"]
