"""The on-call alerts (SSC-062): what pages a person, and the runbook entry each one links to.

Every alert is a threshold on a count of log lines or on a load balancer ratio, and a quiet cell
sends nothing: a Cloud Run service that scales to zero is healthy, so no alert keys on missing
data, an instance count, an uptime check or a heartbeat. With no ``oncall_email`` no alert
resource exists. ``docs/runbooks/ssc-062-support-and-on-call.md`` has one ``##`` heading per
alert name here.
"""

from dataclasses import dataclass
from typing import Final, Literal

import pulumi
import pulumi_gcp as gcp

RUNBOOK: Final = "docs/runbooks/ssc-062-support-and-on-call.md"
USER_METRIC: Final = "logging.googleapis.com/user"
LB_REQUESTS: Final = "loadbalancing.googleapis.com/https/request_count"
MISSING_DATA: Final = "EVALUATION_MISSING_DATA_INACTIVE"
SNAPSHOT_STALE_MS: Final = 60_000
GATEWAY_ERROR_RATE: Final = 0.05
BUILD_FAILURES: Final = 3
BUILD_FAILURE_WINDOW_S: Final = 900
DATAGW_REFUSALS: Final = 20
AUTHORISER_ERRORS: Final = 5
SHORT_WINDOW_S: Final = 300
PAGE_WINDOW_S: Final = 60
CLOUD_RUN: Final = 'resource.type="cloud_run_revision"'
Kind = Literal["count", "ratio", "budget", "nightly"]


@dataclass(frozen=True, slots=True)
class LogMetric:
    """A counter of the log lines ``filter`` matches, in one project."""

    name: str
    description: str
    filter: str


@dataclass(frozen=True, slots=True)
class Alert:
    """One alert. A ``count`` alert fires when its log metric's sum over ``window_s`` is above
    ``threshold``; a ``ratio`` alert when the gateway's 5xx share of load balancer requests is
    above it for ``duration_s``. A ``budget`` or ``nightly`` entry is raised by the cell's budget
    or by the nightly run, and builds no policy. The runbook heading is the ``name``."""

    name: str
    display_name: str
    kind: Kind
    summary: str
    threshold: float = 0
    window_s: int = PAGE_WINDOW_S
    duration_s: int = 0
    severity: str = "CRITICAL"
    metric: LogMetric | None = None

    @property
    def runbook(self) -> str:
        return f"{RUNBOOK}#{self.name}"


def _service(name: str) -> str:
    return f'{CLOUD_RUN} AND resource.labels.service_name="{name}"'


AUTHORISER_METRIC: Final = LogMetric(
    "ssc_gateway_authoriser_errors",
    "Gateway authoriser checks that raised an error",
    f'{_service("ssc-gateway")} AND textPayload:"authz check failed"',
)
SNAPSHOT_STALE_METRIC: Final = LogMetric(
    "ssc_gateway_snapshot_stale",
    "Served requests that found the gateway's snapshot over 60 s old",
    f'{_service("ssc-gateway")} AND textPayload:"gateway snapshot stale"',
)
DATAGW_METRIC: Final = LogMetric(
    "ssc_datagw_refusals",
    "Data gateway refusals other than APP_NOT_ACTIVE",
    f'{_service("ssc-datagw")} AND textPayload:"datagw "'
    ' AND textPayload:"\\"outcome\\": \\""'
    ' AND NOT textPayload:"\\"outcome\\": \\"served\\""'
    ' AND NOT textPayload:"\\"outcome\\": \\"APP_NOT_ACTIVE\\""',
)
PROXY_METRIC: Final = LogMetric(
    "ssc_proxy_unhealthy",
    "The egress proxy's health check went from healthy to unhealthy",
    'log_id("compute.googleapis.com/healthchecks")'
    ' AND jsonPayload.healthCheckProbeResult.healthState="UNHEALTHY"'
    ' AND jsonPayload.healthCheckProbeResult.previousHealthState="HEALTHY"',
)
LATE_METRIC: Final = LogMetric(
    "ssc_snapshot_late",
    "A snapshot compile ran over 60 s or failed for good, or the sweep found one stale",
    f'{CLOUD_RUN} AND (textPayload:"snapshot compile late"'
    ' OR textPayload:"stale snapshot marked dirty")',
)
BUILD_METRIC: Final = LogMetric(
    "ssc_build_failures",
    "Builds that failed",
    f'{CLOUD_RUN} AND textPayload:"build failed"',
)

CELL_ALERTS: Final = (
    Alert(
        "ssc-gateway-authoriser-errors",
        "Gateway authoriser errors",
        "count",
        "The gateway authoriser raised more than 5 errors in 5 minutes.",
        threshold=AUTHORISER_ERRORS,
        window_s=SHORT_WINDOW_S,
        metric=AUTHORISER_METRIC,
    ),
    Alert(
        "ssc-gateway-snapshot-stale",
        "Gateway snapshot over 60 s old",
        "count",
        "A request was served from a snapshot more than 60 seconds old.",
        metric=SNAPSHOT_STALE_METRIC,
    ),
    Alert(
        "ssc-gateway-lb-error-rate",
        "Gateway error rate at the load balancer",
        "ratio",
        "More than 5% of the gateway's requests at the load balancer failed with a 5xx for 5 "
        "minutes.",
        threshold=GATEWAY_ERROR_RATE,
        duration_s=SHORT_WINDOW_S,
        severity="ERROR",
    ),
    Alert(
        "ssc-datagw-refusals",
        "Data gateway refusals",
        "count",
        "The data gateway refused more than 20 calls in 5 minutes (APP_NOT_ACTIVE excluded).",
        threshold=DATAGW_REFUSALS,
        window_s=SHORT_WINDOW_S,
        severity="ERROR",
        metric=DATAGW_METRIC,
    ),
    Alert(
        "ssc-proxy-unhealthy",
        "Egress proxy unhealthy",
        "count",
        "The egress proxy machine failed its health check. The group recreates it.",
        metric=PROXY_METRIC,
    ),
    Alert(
        "ssc-cell-budget",
        "Cell budget",
        "budget",
        "The cell's spend passed 50, 90 or 100 percent of its budget, or is forecast to pass 100.",
        severity="WARNING",
    ),
    Alert(
        "ssc-cert-expiry",
        "Certificate near expiry",
        "nightly",
        "The nightly TLS check found the cell's wildcard certificate under 21 days from expiry.",
        severity="ERROR",
    ),
)
PLATFORM_ALERTS: Final = (
    Alert(
        "ssc-snapshot-late",
        "Snapshot compile or write late",
        "count",
        "A snapshot compile ran over 60 seconds or failed for good, or the sweep found a stale "
        "snapshot.",
        metric=LATE_METRIC,
    ),
    Alert(
        "ssc-build-failures",
        "Build failures",
        "count",
        "At least 3 builds failed in 15 minutes.",
        threshold=BUILD_FAILURES - 1,
        window_s=BUILD_FAILURE_WINDOW_S,
        severity="ERROR",
        metric=BUILD_METRIC,
    ),
)
ALL_ALERTS: Final = CELL_ALERTS + PLATFORM_ALERTS


def notification_channel(
    project: pulumi.Input[str], email: str, opts: pulumi.ResourceOptions
) -> gcp.monitoring.NotificationChannel:
    """The one email channel every alert in ``project`` uses."""
    return gcp.monitoring.NotificationChannel(
        "oncall",
        project=project,
        display_name="SSC on call",
        type="email",
        labels={"email_address": email},
        opts=opts,
    )


def _condition(alert: Alert) -> gcp.monitoring.AlertPolicyConditionArgs:
    if alert.kind == "ratio":
        aggregation = gcp.monitoring.AlertPolicyConditionConditionThresholdAggregationArgs(
            alignment_period=f"{SHORT_WINDOW_S}s",
            per_series_aligner="ALIGN_RATE",
            cross_series_reducer="REDUCE_SUM",
        )
        total = (
            f'metric.type="{LB_REQUESTS}" AND resource.type="https_lb_rule"'
            ' AND resource.labels.backend_target_name="ssc-gateway"'
        )
        threshold = gcp.monitoring.AlertPolicyConditionConditionThresholdArgs(
            comparison="COMPARISON_GT",
            threshold_value=alert.threshold,
            duration=f"{alert.duration_s}s",
            filter=f"{total} AND metric.labels.response_code_class=500",
            denominator_filter=total,
            aggregations=[aggregation],
            denominator_aggregations=[
                gcp.monitoring.AlertPolicyConditionConditionThresholdDenominatorAggregationArgs(
                    alignment_period=f"{SHORT_WINDOW_S}s",
                    per_series_aligner="ALIGN_RATE",
                    cross_series_reducer="REDUCE_SUM",
                )
            ],
            evaluation_missing_data=MISSING_DATA,
        )
    else:
        assert alert.metric is not None
        threshold = gcp.monitoring.AlertPolicyConditionConditionThresholdArgs(
            comparison="COMPARISON_GT",
            threshold_value=alert.threshold,
            duration=f"{alert.duration_s}s",
            filter=f'metric.type="{USER_METRIC}/{alert.metric.name}"',
            aggregations=[
                gcp.monitoring.AlertPolicyConditionConditionThresholdAggregationArgs(
                    alignment_period=f"{alert.window_s}s",
                    per_series_aligner="ALIGN_SUM",
                    cross_series_reducer="REDUCE_SUM",
                )
            ],
            evaluation_missing_data=MISSING_DATA,
        )
    return gcp.monitoring.AlertPolicyConditionArgs(
        display_name=alert.display_name, condition_threshold=threshold
    )


def _alerts(
    project: pulumi.Input[str],
    alerts: tuple[Alert, ...],
    channel: gcp.monitoring.NotificationChannel,
    opts: pulumi.ResourceOptions,
) -> None:
    metrics = {
        alert.metric.name: gcp.logging.Metric(
            f"metric-{alert.metric.name}",
            project=project,
            name=alert.metric.name,
            description=alert.metric.description,
            filter=alert.metric.filter,
            metric_descriptor=gcp.logging.MetricMetricDescriptorArgs(
                metric_kind="DELTA", value_type="INT64"
            ),
            opts=opts,
        )
        for alert in alerts
        if alert.metric is not None
    }
    for alert in alerts:
        if alert.kind not in ("count", "ratio"):
            continue
        after = [metrics[alert.metric.name]] if alert.metric is not None else []
        gcp.monitoring.AlertPolicy(
            f"alert-{alert.name}",
            project=project,
            display_name=alert.display_name,
            combiner="OR",
            severity=alert.severity,
            conditions=[_condition(alert)],
            notification_channels=[channel.name],
            documentation=gcp.monitoring.AlertPolicyDocumentationArgs(
                content=f"{alert.summary}\n\nRunbook: {alert.runbook}",
                mime_type="text/markdown",
            ),
            user_labels={"runbook": alert.name},
            opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(depends_on=after)),
        )


def cell_alerts(
    project: pulumi.Input[str],
    channel: gcp.monitoring.NotificationChannel,
    opts: pulumi.ResourceOptions,
) -> None:
    """A cell's own alerts, in the cell's project."""
    _alerts(project, CELL_ALERTS, channel, opts)


def platform_alerts(
    project: pulumi.Input[str],
    channel: gcp.monitoring.NotificationChannel,
    opts: pulumi.ResourceOptions,
) -> None:
    """The control plane's alerts, in one control project."""
    _alerts(project, PLATFORM_ALERTS, channel, opts)
