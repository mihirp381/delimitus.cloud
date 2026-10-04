"""The on-call alerts (SSC-062): who they notify, that a quiet cell raises none, and that every
one has a runbook entry."""

import re
from pathlib import Path
from typing import Any

import pytest

from mockcloud import Declared, as_export, one, run
from ssc_infra import alerts, cell_diff, naming

A, B = "testcell01", "testcell02"
EMAIL = "oncall@example.com"
ALERT_TYPE = "gcp:monitoring/alertPolicy:AlertPolicy"
CHANNEL_TYPE = "gcp:monitoring/notificationChannel:NotificationChannel"
METRIC_TYPE = "gcp:logging/metric:Metric"
BUDGET_TYPE = "gcp:billing/budget:Budget"
HEALTH_TYPE = "gcp:compute/healthCheck:HealthCheck"
PLATFORM_FOLDER = "333333333333"
RUNBOOK = Path(__file__).resolve().parents[2] / alerts.RUNBOOK
FORBIDDEN = ("uptime", "instance_count", "heartbeat", "conditionAbsent", "no instances")


@pytest.fixture(scope="module")
def cell_a() -> list[Declared]:
    return run(naming.cell_stack(A), {"oncall_email": EMAIL})


@pytest.fixture(scope="module")
def cell_b() -> list[Declared]:
    return run(naming.cell_stack(B), {"oncall_email": EMAIL})


@pytest.fixture(scope="module")
def platform_alerts() -> list[Declared]:
    return run(
        naming.PLATFORM_STACK,
        {"platform_folder_id": PLATFORM_FOLDER, "oncall_email": EMAIL},
    )


def _policies(declared: list[Declared]) -> dict[str, dict[str, Any]]:
    return {d.name: d.inputs for d in declared if d.type == ALERT_TYPE}


def _expected(kinds: tuple[alerts.Alert, ...]) -> set[str]:
    return {f"alert-{a.name}" for a in kinds if a.kind in ("count", "ratio")}


def test_a_cell_has_every_cell_alert_and_a_channel(cell_a: list[Declared]) -> None:
    assert set(_policies(cell_a)) == _expected(alerts.CELL_ALERTS)
    channel = one(cell_a, CHANNEL_TYPE).inputs
    assert channel["type"] == "email"
    assert channel["labels"] == {"email_address": EMAIL}


def test_the_control_plane_has_its_alerts_in_each_control_project(
    platform_alerts: list[Declared],
) -> None:
    assert set(_policies(platform_alerts)) >= _expected(alerts.PLATFORM_ALERTS)
    projects = {d.inputs["project"] for d in platform_alerts if d.type == ALERT_TYPE}
    assert projects == {naming.control_project("staging")}
    assert one(platform_alerts, CHANNEL_TYPE).inputs["labels"] == {"email_address": EMAIL}


def test_no_alert_resources_without_an_oncall_email() -> None:
    for declared in (
        run(naming.cell_stack(A)),
        run(naming.PLATFORM_STACK, {"platform_folder_id": PLATFORM_FOLDER}),
    ):
        kinds = {d.type for d in declared}
        assert not kinds & {ALERT_TYPE, CHANNEL_TYPE, METRIC_TYPE}


def test_every_policy_notifies_the_channel_and_links_its_runbook(
    cell_a: list[Declared], platform_alerts: list[Declared]
) -> None:
    for declared in (cell_a, platform_alerts):
        channel = one(declared, CHANNEL_TYPE).outputs["name"]
        assert channel.startswith("projects/")
        for name, inputs in _policies(declared).items():
            assert inputs["notificationChannels"] == [channel], name
            assert (
                f"{alerts.RUNBOOK}#{name.removeprefix('alert-')}"
                in (inputs["documentation"]["content"])
            )
            assert inputs["userLabels"] == {"runbook": name.removeprefix("alert-")}
            for condition in inputs["conditions"]:
                assert re.fullmatch(r"\d+s", condition["conditionThreshold"]["duration"])


def test_nothing_alerts_on_a_sleeping_service(
    cell_a: list[Declared], platform_alerts: list[Declared]
) -> None:
    for declared in (cell_a, platform_alerts):
        for name, inputs in _policies(declared).items():
            assert not any(word in repr(inputs) for word in FORBIDDEN), name
            for condition in inputs["conditions"]:
                threshold = condition["conditionThreshold"]
                assert threshold["evaluationMissingData"] == "EVALUATION_MISSING_DATA_INACTIVE"
    for alert in alerts.ALL_ALERTS:
        assert not any(word in alert.summary.lower() for word in ("no instances", "heartbeat"))


def test_a_stalled_snapshot_pages_within_five_minutes(
    cell_a: list[Declared], platform_alerts: list[Declared]
) -> None:
    for name, declared in (
        ("ssc-gateway-snapshot-stale", cell_a),
        ("ssc-snapshot-late", platform_alerts),
    ):
        condition = _policies(declared)[f"alert-{name}"]["conditions"][0]["conditionThreshold"]
        window = int(condition["aggregations"][0]["alignmentPeriod"].removesuffix("s"))
        assert window + int(condition["duration"].removesuffix("s")) <= 300
        assert condition["thresholdValue"] == 0


def test_the_thresholds_are_the_ones_the_ticket_sets() -> None:
    by_name = {a.name: a for a in alerts.ALL_ALERTS}
    assert (by_name["ssc-build-failures"].threshold, by_name["ssc-build-failures"].window_s) == (
        2,
        900,
    )
    assert by_name["ssc-datagw-refusals"].threshold == 20
    assert by_name["ssc-datagw-refusals"].window_s == 300
    assert by_name["ssc-gateway-authoriser-errors"].threshold == 5
    assert by_name["ssc-gateway-authoriser-errors"].window_s == 300
    assert alerts.SNAPSHOT_STALE_MS == 60_000


def test_the_proxy_alert_keys_on_the_health_check_logs(cell_a: list[Declared]) -> None:
    metric = one(cell_a, METRIC_TYPE, "metric-ssc_proxy_unhealthy").inputs
    assert 'log_id("compute.googleapis.com/healthchecks")' in metric["filter"]
    assert "resource.type" not in metric["filter"]
    for state in ('healthState="UNHEALTHY"', 'previousHealthState="HEALTHY"'):
        assert f"jsonPayload.healthCheckProbeResult.{state}" in metric["filter"]
    policy = _policies(cell_a)["alert-ssc-proxy-unhealthy"]
    filter_ = policy["conditions"][0]["conditionThreshold"]["filter"]
    assert filter_ == 'metric.type="logging.googleapis.com/user/ssc_proxy_unhealthy"'
    assert one(cell_a, HEALTH_TYPE).inputs["logConfig"] == {"enable": True}


def test_the_proxy_health_check_logs_whether_or_not_alerts_are_on() -> None:
    health = one(run(naming.cell_stack(A)), HEALTH_TYPE).inputs
    assert health["logConfig"] == {"enable": True}


def test_the_data_gateway_alert_leaves_out_apps_that_are_not_active(
    cell_a: list[Declared],
) -> None:
    metric = one(cell_a, METRIC_TYPE, "metric-ssc_datagw_refusals").inputs["filter"]
    assert 'NOT textPayload:"\\"outcome\\": \\"APP_NOT_ACTIVE\\""' in metric
    assert 'NOT textPayload:"\\"outcome\\": \\"served\\""' in metric


def test_the_stale_gateway_alert_counts_only_the_warning_line(cell_a: list[Declared]) -> None:
    metric = one(cell_a, METRIC_TYPE, "metric-ssc_gateway_snapshot_stale").inputs["filter"]
    assert 'textPayload:"gateway snapshot stale"' in metric
    assert "snapshot age" not in metric


def test_the_load_balancer_alert_is_the_gateways_5xx_share(cell_a: list[Declared]) -> None:
    condition = _policies(cell_a)["alert-ssc-gateway-lb-error-rate"]["conditions"][0]
    threshold = condition["conditionThreshold"]
    assert 'backend_target_name="ssc-gateway"' in threshold["denominatorFilter"]
    assert "response_code_class=500" in threshold["filter"]
    assert "response_code_class" not in threshold["denominatorFilter"]
    assert threshold["thresholdValue"] == alerts.GATEWAY_ERROR_RATE


def test_the_cell_budget_notifies_the_channel(cell_a: list[Declared]) -> None:
    budget = one(cell_a, BUDGET_TYPE, "cell-monthly").inputs
    channel = one(cell_a, CHANNEL_TYPE).outputs["name"]
    assert budget["allUpdatesRule"] == {"monitoringNotificationChannels": [channel]}
    assert [r["thresholdPercent"] for r in budget["thresholdRules"]] == [0.5, 0.9, 1.0, 1.0]
    assert sum(d.type == BUDGET_TYPE for d in cell_a) == 1


def test_the_cell_budget_has_no_channel_without_an_oncall_email() -> None:
    budget = one(run(naming.cell_stack(A)), BUDGET_TYPE, "cell-monthly").inputs
    assert not budget.get("allUpdatesRule")


def test_two_cells_with_alerts_differ_only_in_their_label(
    cell_a: list[Declared], cell_b: list[Declared]
) -> None:
    config = {"oncall_email": EMAIL}
    first = cell_diff.normalise(as_export(cell_a, A, config=config), A)
    second = cell_diff.normalise(as_export(cell_b, B, config=config), B)
    assert cell_diff.compare(first, second) == []


def test_the_alerts_add_nothing_to_a_cell_with_no_oncall_email() -> None:
    first = cell_diff.normalise(as_export(run(naming.cell_stack(A)), A), A)
    second = cell_diff.normalise(as_export(run(naming.cell_stack(B)), B), B)
    assert cell_diff.compare(first, second) == []


def test_the_runbook_has_a_heading_for_every_alert_and_no_other() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    headings = set(re.findall(r"^## (ssc-[a-z0-9-]+)\s*$", text, re.MULTILINE))
    assert headings == {alert.name for alert in alerts.ALL_ALERTS}
    for alert in alerts.ALL_ALERTS:
        assert f"## {alert.runbook.rsplit('#', 1)[1]}" in text


def test_every_alert_name_is_unique_and_a_valid_anchor() -> None:
    names = [alert.name for alert in alerts.ALL_ALERTS]
    assert len(names) == len(set(names))
    for alert in alerts.ALL_ALERTS:
        assert re.fullmatch(r"ssc-[a-z0-9-]+", alert.name)
        assert alert.runbook == f"{alerts.RUNBOOK}#{alert.name}"
