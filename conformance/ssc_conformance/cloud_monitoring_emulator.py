"""An in-memory Cloud Monitoring API v3 ``timeSeries.list`` of Cloud Run's metrics, for
``httpx2.MockTransport``.

A test says when each instance of a service started, how long it took to start and when it
stopped, and when requests (or open streams) were in flight. From that timeline the emulator
writes what Cloud Run writes, one raw point a minute per revision:

- ``container/billable_instance_time``: an instance-billed instance's whole life, or a
  request-billed one's start-up and busy seconds;
- ``container/instance_count``, label ``state`` ``active`` (serving a request) or ``idle``;
- ``container/startup_latencies``: a distribution of the start-up times of the minute's starts.

A point is readable only ``delay`` after its minute ends, by the ``now`` clock. Queries follow the
API: ``interval``, ``perSeriesAligner`` (``ALIGN_SUM``, ``ALIGN_MAX``, ``ALIGN_DELTA``),
``crossSeriesReducer`` ``REDUCE_SUM`` grouped by ``resource.labels.service_name``, newest point
first, INT64 values as strings, and pages of ``series_per_page`` series. It understands the part
of the filter language the cell agent writes and keeps every call, so a test can count them.
"""

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx2

from ssc_conformance.cloud_run_emulator import PROJECT

type Json = dict[str, Any]

INSTANCE_TIME: Final = "run.googleapis.com/container/billable_instance_time"
INSTANCE_COUNT: Final = "run.googleapis.com/container/instance_count"
STARTUP_LATENCIES: Final = "run.googleapis.com/container/startup_latencies"
MINUTE: Final = timedelta(minutes=1)
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_TERM = re.compile(r'([a-z_.]+)\s*=\s*(?:"([^"]*)"|starts_with\("([^"]*)"\))')


@dataclass(frozen=True, slots=True)
class _Instance:
    service: str
    revision: str
    started: datetime
    ready: datetime
    stopped: datetime
    startup_ms: float
    billing: str


@dataclass(frozen=True, slots=True)
class _Busy:
    service: str
    start: datetime
    end: datetime


class CloudMonitoringEmulator:
    def __init__(
        self,
        *,
        project: str = PROJECT,
        now: Callable[[], datetime] | None = None,
        delay: timedelta = timedelta(minutes=3),
        series_per_page: int = 1000,
    ) -> None:
        self.project = project
        self.delay = delay
        self.series_per_page = series_per_page
        self.calls: list[dict[str, str]] = []
        self.hosts: list[str] = []
        self.refuse: int | None = None
        self._now = now or (lambda: datetime.now(UTC))
        self._instances: list[_Instance] = []
        self._busy: list[_Busy] = []

    def instance(  # noqa: PLR0913  (keyword-only)
        self,
        service: str,
        *,
        started: datetime,
        stopped: datetime,
        startup_ms: float,
        billing: str = "instance",
        revision: str = "r1",
    ) -> None:
        ready = started + timedelta(milliseconds=startup_ms)
        self._instances.append(
            _Instance(
                service, f"{service}-{revision}", started, ready, stopped, startup_ms, billing
            )
        )

    def busy(self, service: str, *, start: datetime, end: datetime) -> None:
        """A request, or an open stream, in flight on ``service`` from ``start`` to ``end``."""
        self._busy.append(_Busy(service, start, end))

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.hosts.append(request.url.host)
        if request.url.host != "monitoring.googleapis.com":
            return _error(404, "NOT_FOUND", f"no host {request.url.host}")
        if request.method != "GET" or request.url.path != f"/v3/projects/{self.project}/timeSeries":
            return _error(404, "NOT_FOUND", f"no route {request.method} {request.url.path}")
        params = dict(request.url.params)
        self.calls.append(params)
        if self.refuse is not None:
            return _error(self.refuse, "PERMISSION_DENIED", "refused by the test")
        try:
            series = self._query(params)
        except (KeyError, ValueError) as exc:
            return _error(400, "INVALID_ARGUMENT", str(exc))
        start = int(params.get("pageToken") or 0)
        page = series[start : start + self.series_per_page]
        body: Json = {"timeSeries": page} if page else {}
        if start + self.series_per_page < len(series):
            body["nextPageToken"] = str(start + self.series_per_page)
        return httpx2.Response(200, json=body)

    def _query(self, params: dict[str, str]) -> list[Json]:
        terms = _terms(params["filter"])
        metric = terms.pop("metric.type")
        if terms.pop("resource.type", None) != "cloud_run_revision":
            raise ValueError("only cloud_run_revision")
        prefix = terms.pop("resource.labels.service_name")
        state = terms.pop("metric.labels.state", None)
        if terms:
            raise ValueError(f"unknown filter terms {sorted(terms)}")
        lo = _time(params["interval.startTime"])
        hi = _time(params["interval.endTime"])
        period = timedelta(seconds=int(params["aggregation.alignmentPeriod"].removesuffix("s")))
        aligner = params["aggregation.perSeriesAligner"]
        if params.get("aggregation.crossSeriesReducer") != "REDUCE_SUM":
            raise ValueError("only REDUCE_SUM")
        if params.get("aggregation.groupByFields") != "resource.labels.service_name":
            raise ValueError("only grouped by service")
        visible = self._now() - self.delay
        raw = self._raw(metric, state, prefix)
        per_service: dict[str, dict[datetime, Any]] = defaultdict(dict)
        for (service, _revision), minutes in raw.items():
            aligned: dict[datetime, list[Any]] = defaultdict(list)
            for minute, value in minutes.items():
                if minute + MINUTE > visible or not lo <= minute < hi:
                    continue
                end = hi - ((hi - minute - timedelta(microseconds=1)) // period) * period
                aligned[end].append(value)
            for end, values in aligned.items():
                one = _align(aligner, values)
                before = per_service[service].get(end)
                per_service[service][end] = one if before is None else _sum([before, one])
        return [
            _series(service, metric, period, points)
            for service, points in sorted(per_service.items())
        ]

    def _raw(
        self, metric: str, state: str | None, prefix: str
    ) -> dict[tuple[str, str], dict[datetime, Any]]:
        out: dict[tuple[str, str], dict[datetime, Any]] = defaultdict(dict)
        for inst in self._instances:
            if not inst.service.startswith(prefix):
                continue
            key = (inst.service, inst.revision)
            minute = _floor(inst.started)
            while minute < inst.stopped:
                window = (max(minute, inst.started), min(minute + MINUTE, inst.stopped))
                busy = self._busy_seconds(inst, window)
                if metric == INSTANCE_TIME:
                    starting = _overlap(window, (inst.started, inst.ready))
                    seconds = (
                        (window[1] - window[0]).total_seconds()
                        if inst.billing == "instance"
                        else min(60.0, busy + starting)
                    )
                    out[key][minute] = out[key].get(minute, 0.0) + seconds
                elif metric == INSTANCE_COUNT:
                    active = 1 if busy > 0 else 0
                    count = active if state == "active" else 1 - active
                    out[key][minute] = out[key].get(minute, 0) + count
                minute += MINUTE
            if metric == STARTUP_LATENCIES:
                started = _floor(inst.started)
                dist = out[key].get(started, (0, 0.0))
                out[key][started] = (dist[0] + 1, dist[1] + inst.startup_ms)
        return out

    def _busy_seconds(self, inst: _Instance, window: tuple[datetime, datetime]) -> float:
        lo = max(window[0], inst.ready)
        spans = [
            (max(b.start, lo), min(b.end, window[1]))
            for b in self._busy
            if b.service == inst.service
        ]
        spans = sorted(s for s in spans if s[0] < s[1])
        total, edge = 0.0, lo
        for begin, end in spans:
            start = max(begin, edge)
            if end > start:
                total += (end - start).total_seconds()
                edge = end
        return total


def _terms(filter_: str) -> dict[str, str]:
    parts = [p.strip() for p in filter_.split(" AND ")]
    terms: dict[str, str] = {}
    for part in parts:
        m = _TERM.fullmatch(part)
        if m is None:
            raise ValueError(f"unknown filter term {part!r}")
        terms[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return terms


def _align(aligner: str, values: list[Any]) -> Any:
    if aligner == "ALIGN_MAX":
        return max(values)
    if aligner in {"ALIGN_SUM", "ALIGN_DELTA"}:
        return _sum(values)
    raise ValueError(f"unknown aligner {aligner}")


def _sum(values: list[Any]) -> Any:
    if isinstance(values[0], tuple):
        return (sum(v[0] for v in values), sum(v[1] for v in values))
    return sum(values)


def _series(service: str, metric: str, period: timedelta, points: dict[datetime, Any]) -> Json:
    kind, value_type = {
        INSTANCE_TIME: ("DELTA", "DOUBLE"),
        INSTANCE_COUNT: ("GAUGE", "INT64"),
        STARTUP_LATENCIES: ("DELTA", "DISTRIBUTION"),
    }[metric]
    return {
        "metric": {"type": metric},
        "resource": {"type": "cloud_run_revision", "labels": {"service_name": service}},
        "metricKind": kind,
        "valueType": value_type,
        "points": [
            {
                "interval": {"startTime": _rfc3339(end - period), "endTime": _rfc3339(end)},
                "value": _value(value_type, value),
            }
            for end, value in sorted(points.items(), reverse=True)
        ],
    }


def _value(value_type: str, value: Any) -> Json:
    if value_type == "DOUBLE":
        return {"doubleValue": float(value)}
    if value_type == "INT64":
        return {"int64Value": str(int(value))}
    count, total = value
    return {"distributionValue": {"count": str(count), "mean": total / count if count else 0.0}}


def _overlap(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> float:
    return max(0.0, (min(a[1], b[1]) - max(a[0], b[0])).total_seconds())


def _floor(moment: datetime) -> datetime:
    return _EPOCH + ((moment - _EPOCH) // MINUTE) * MINUTE


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error(status: int, reason: str, message: str) -> httpx2.Response:
    return httpx2.Response(
        status, json={"error": {"code": status, "status": reason, "message": message}}
    )


__all__ = ["CloudMonitoringEmulator"]
