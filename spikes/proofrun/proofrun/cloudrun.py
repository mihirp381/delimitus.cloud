"""Read-only views of Cloud Run: a service's settings from ``gcloud run services describe``, and
its instance counts from Cloud Monitoring (``container/instance_count``, by ``state``)."""

import json
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from proofrun.common import REGION, Run, access_token, fence, gcloud_json, parse_time

MONITORING: Final = "https://monitoring.googleapis.com/v3"
INSTANCE_COUNT: Final = "run.googleapis.com/container/instance_count"
BILLABLE_TIME: Final = "run.googleapis.com/container/billable_instance_time"
STARTUP_LATENCIES: Final = "run.googleapis.com/container/startup_latencies"


def describe(run: Run, project: str, service: str, region: str = REGION) -> dict[str, Any]:
    return gcloud_json(
        run, "run", "services", "describe", service, f"--region={region}", f"--project={project}"
    )


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    """What a proof checks on a service. Unset values are None (Cloud Run's default applies)."""

    min_instances: int
    service_min_instances: int
    max_instances: int | None
    generation: str | None
    cpu: str | None
    memory: str | None
    concurrency: int | None
    cpu_throttled: bool | None
    timeout_seconds: int | None

    def describe(self) -> str:
        return (
            f"min {self.min_instances} (service {self.service_min_instances}), "
            f"max {self.max_instances}, generation {self.generation or 'unset'}, "
            f"cpu {self.cpu}, memory {self.memory}, concurrency {self.concurrency}, "
            f"cpu throttled {self.cpu_throttled}, timeout {self.timeout_seconds} s"
        )


def _int(value: object) -> int | None:
    return int(str(value)) if value not in {None, ""} else None


def settings(service: Mapping[str, Any]) -> ServiceSettings:
    """The settings of a ``gcloud run services describe --format=json`` document."""
    meta = service.get("metadata", {}).get("annotations", {})
    template = service.get("spec", {}).get("template", {})
    annotations = template.get("metadata", {}).get("annotations", {})
    spec = template.get("spec", {})
    container = (spec.get("containers") or [{}])[0]
    limits = container.get("resources", {}).get("limits", {})
    throttling = annotations.get("run.googleapis.com/cpu-throttling")
    return ServiceSettings(
        min_instances=_int(annotations.get("autoscaling.knative.dev/minScale")) or 0,
        service_min_instances=_int(meta.get("run.googleapis.com/minScale")) or 0,
        max_instances=_int(annotations.get("autoscaling.knative.dev/maxScale")),
        generation=annotations.get("run.googleapis.com/execution-environment"),
        cpu=limits.get("cpu"),
        memory=limits.get("memory"),
        concurrency=_int(spec.get("containerConcurrency")),
        cpu_throttled=None if throttling is None else str(throttling).lower() == "true",
        timeout_seconds=_int(spec.get("timeoutSeconds")),
    )


def cpu_value(cpu: str | None) -> float | None:
    """``"1"``, ``"1000m"`` or ``"0.5"`` as a number of vCPUs."""
    if cpu is None:
        return None
    return int(cpu[:-1]) / 1000 if cpu.endswith("m") else float(cpu)


type Get = Callable[[str, Mapping[str, str]], dict[str, Any]]


def _get(url: str, headers: Mapping[str, str]) -> dict[str, Any]:
    fence(url)
    request = urllib.request.Request(url, headers=dict(headers))  # noqa: S310
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return json.loads(response.read())


def series_query(  # noqa: PLR0913  (keyword-only)
    *,
    project: str,
    service: str,
    metric: str,
    start: datetime,
    end: datetime,
    aligner: str = "ALIGN_MAX",
) -> str:
    """The ``timeSeries.list`` URL for one service's metric, one point a minute."""
    query = urllib.parse.urlencode(
        {
            "filter": f'metric.type="{metric}" AND resource.labels.service_name="{service}"',
            "interval.startTime": start.isoformat().replace("+00:00", "Z"),
            "interval.endTime": end.isoformat().replace("+00:00", "Z"),
            "aggregation.alignmentPeriod": "60s",
            "aggregation.perSeriesAligner": aligner,
        }
    )
    return f"{MONITORING}/projects/{project}/timeSeries?{query}"


def minutes_by_state(document: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    """``{state: {minute end time: value}}`` from a ``timeSeries.list`` answer. Series without a
    ``state`` label (other metrics) fall under ``all``; values of one minute are summed."""
    out: dict[str, dict[str, float]] = {}
    for series in document.get("timeSeries", []):
        state = series.get("metric", {}).get("labels", {}).get("state", "all")
        for point in series.get("points", []):
            value = point.get("value", {})
            if "distributionValue" in value:
                number = float(value["distributionValue"].get("mean", 0))
            else:
                number = float(value.get("int64Value", value.get("doubleValue", 0)))
            minute = parse_time(point["interval"]["endTime"]).isoformat()
            bucket = out.setdefault(state, {})
            bucket[minute] = bucket.get(minute, 0.0) + number
    return out


def read_series(  # noqa: PLR0913  (keyword-only)
    run: Run,
    *,
    project: str,
    service: str,
    metric: str,
    end: datetime,
    minutes: int,
    aligner: str = "ALIGN_MAX",
    get: Get = _get,
) -> dict[str, dict[str, float]]:
    """A service's metric over the last ``minutes`` before ``end``, read as the operator."""
    url = series_query(
        project=project,
        service=service,
        metric=metric,
        start=end - timedelta(minutes=minutes),
        end=end,
        aligner=aligner,
    )
    headers = {"Authorization": f"Bearer {access_token(run)}"}
    pages: dict[str, dict[str, float]] = {}
    while True:
        document = get(url, headers)
        for state, points in minutes_by_state(document).items():
            pages.setdefault(state, {}).update(points)
        token = document.get("nextPageToken")
        if not token:
            return pages
        url = url.split("&pageToken=")[0] + "&" + urllib.parse.urlencode({"pageToken": token})
