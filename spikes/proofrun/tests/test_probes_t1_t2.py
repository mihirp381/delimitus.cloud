from datetime import UTC, datetime

import pytest
from conftest import FakeRun, answer, ok

from proofrun import cloudrun, instances, probes, t1, t2
from proofrun.common import Done

NIGHTLY = """# Nightly conformance

| probe | status | reason |
| --- | --- | --- |
| no_internet | passed | blocked: a | b |
| cannot_reach_peer_cell | failed | the peer cell let calls through: app by name: HTTP 403 |
| ready | skipped | not deployed |

Drift repaired in: ssc-c-cellone01
"""


def test_nightly_rows_keep_pipes_inside_the_reason() -> None:
    rows = probes.nightly_rows(NIGHTLY)
    assert [r["probe"] for r in rows] == ["no_internet", "cannot_reach_peer_cell", "ready"]
    assert rows[0]["reason"] == "blocked: a | b"
    assert probes.nightly_drift(NIGHTLY) == "ssc-c-cellone01"
    assert probes.nightly_drift("nothing") is None


def test_probe_counts_leave_out_the_peer_cell_probe() -> None:
    passed, total, bad = probes.probe_counts(probes.nightly_rows(NIGHTLY))
    assert (passed, total) == (1, 2)
    assert bad == ["ready: skipped: not deployed"]


def test_peer_legs_sort_network_ingress_and_iam() -> None:
    reason = (
        "app by name: {'blocked': True, 'error': 'timed out'}; "
        "gateway by Google VIP with ID token: HTTP 404; "
        "tcp 10.20.0.5:8080: {'blocked': True}; "
        "range leg not applicable: same range, separate networks"
    )
    legs = probes.peer_legs(reason)
    assert [(leg.name, leg.kind, leg.target) for leg in legs] == [
        ("app by name", "network", "app"),
        ("gateway by Google VIP with ID token", "ingress", "gateway"),
        ("tcp 10.20.0.5:8080", "network", "range"),
    ]
    assert probes.range_not_applicable(reason)


def test_peer_legs_on_a_failure_name_what_got_through() -> None:
    reason = (
        "the peer cell let calls through: app by name with ID token: HTTP 403, "
        "gateway by name: HTTP 404 from the peer itself (Envoy server header), "
        "app by Google VIP with ID token: {'blocked': False}"
    )
    kinds = [(leg.name, leg.kind) for leg in probes.peer_legs(reason)]
    assert kinds == [
        ("app by name with ID token", "iam"),
        ("gateway by name", "peer"),
        ("app by Google VIP with ID token", "answered"),
    ]
    assert not probes.range_not_applicable(reason)


def test_the_runner_still_has_fourteen_probes_without_a_peer() -> None:
    if not (probes.PROBE_APP / "runner.py").exists():
        pytest.skip("conformance probe app not in this checkout")
    runner = probes.load_runner()
    names = list(runner.checks.PROBES)
    assert probes.PEER_CELL_PROBE in names
    assert len(names) - 1 == 14


DIFF_OUT = "policies in force on ssc-c-cellone01:\n  a\n  override: run.allowedIngress\n"


def test_read_diff_passes_only_with_no_difference_and_no_override() -> None:
    clean = t1.read_diff(
        0,
        "",
        "flags differ, their resources left out: sql\n41 resources compared, 0 difference(s)\n",
    )
    assert clean.passed is True
    assert clean.data["compared"] == 41
    assert clean.lines[0] == "left out for differing flags: sql"
    override = t1.read_diff(0, DIFF_OUT, "41 resources compared, 0 difference(s)")
    assert override.passed is False
    assert override.data["overrides"] == ["override: run.allowedIngress"]
    assert t1.read_diff(1, "~ x", "41 resources compared, 1 difference(s)").passed is False
    assert t1.read_diff(0, "", "0 resources compared, 0 difference(s)").passed is False
    unfinished = t1.read_diff(2, "", "Traceback\nKeyError: x")
    assert unfinished.passed is None
    assert unfinished.lines == ["KeyError: x"]


def test_t1_diff_runs_cell_diff_from_infra() -> None:
    run = FakeRun([(["ssc_infra.cell_diff"], Done(0, "", "3 resources compared, 0 difference(s)"))])
    args = t1.argparse.Namespace(step="diff", full="cellone01", empty="celltwo02")
    assert t1.run(args, run).passed is True
    assert run.calls[0][-2:] == ["cellone01", "celltwo02"]


def test_daily_cost() -> None:
    cheap = t1.daily_cost(5.6, 7)
    assert cheap.passed is True
    assert cheap.data == {"per_day_usd": 0.8, "month_usd": 24.32}
    assert t1.daily_cost(7.7, 7).passed is False
    with pytest.raises(ValueError):
        t1.daily_cost(1, 0)


def test_certificate_issue_time_from_the_record() -> None:
    changes = [
        {"startTime": "2026-10-03T10:05:00Z", "additions": [{"type": "A", "name": "x."}]},
        {
            "startTime": "2026-10-03T10:00:00Z",
            "additions": [{"type": "CNAME", "name": "_acme-challenge.cellone01.apps.example."}],
        },
    ]
    added = t2.record_added(changes, "cellone01")
    assert added == datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
    assert t2.record_added(changes, "celltwo02") is None
    cert = {"managed": {"state": "ACTIVE"}, "updateTime": "2026-10-03T10:12:30Z"}
    assert t2.issue_minutes(cert, added) == ("ACTIVE", 12.5)
    assert t2.issue_minutes({"managed": {"state": "PROVISIONING"}}, added) == ("PROVISIONING", None)


def test_t2_verdict_needs_all_three_and_names_a_sealed_cookie() -> None:
    good = answer(200, b"ok")
    out = t2.verdict(
        state="ACTIVE",
        minutes=12.0,
        entry_passed=True,
        entry_line=t2.direct_line("x\ndirect: 404 from ingress\n"),
        answer=good,
        cookie_source="sealed",
    )
    assert out.passed is True
    assert "direct: 404 from ingress" in out.lines[1]
    assert "not exercised" in out.lines[-1]
    slow = t2.verdict(
        state="ACTIVE",
        minutes=31.0,
        entry_passed=True,
        entry_line="",
        answer=good,
        cookie_source="browser",
    )
    assert slow.passed is False
    assert t2.direct_line("only\nlast") == "last"


def test_instance_window_counts_active_minutes() -> None:
    by_state = {
        "active": {"10:01": 1.0, "10:02": 1.0, "10:03": 0.0},
        "idle": {"10:03": 1.0, "10:04": 1.0},
    }
    out = instances.read_window(by_state)
    assert out.number == "2/4 minutes active"
    assert out.passed is False
    trimmed = instances.read_window(
        {"active": {"10:01": 1.0, "10:02": 1.0}, "idle": {"10:03": 1.0}}, 1
    )
    assert trimmed.passed is True
    assert instances.read_window({}).passed is None
    values = instances.read_values("startup_latencies", {"all": {"10:01": 1200.0, "10:02": 800.0}})
    assert values.data == {"points": 2, "total": 2000.0}


def test_monitoring_pages_are_merged_by_state() -> None:
    def point(minute: str, value: dict[str, object]) -> dict[str, object]:
        return {"interval": {"endTime": f"2026-10-03T10:{minute}:00Z"}, "value": value}

    pages = [
        {
            "timeSeries": [
                {
                    "metric": {"labels": {"state": "active"}},
                    "points": [point("01", {"int64Value": "1"})],
                }
            ],
            "nextPageToken": "p2",
        },
        {
            "timeSeries": [
                {
                    "metric": {"labels": {"state": "idle"}},
                    "points": [point("02", {"doubleValue": 2.0})],
                }
            ]
        },
    ]
    seen: list[str] = []

    def get(url: str, headers: object) -> dict[str, object]:
        seen.append(url)
        return pages[len(seen) - 1]

    run = FakeRun([(["print-access-token"], Done(0, "tok\n", ""))])
    out = cloudrun.read_series(
        run,
        project="ssc-c-cellone01",
        service="ssc-a-x",
        metric=cloudrun.INSTANCE_COUNT,
        end=datetime(2026, 10, 3, 10, 5, tzinfo=UTC),
        minutes=5,
        get=get,
    )
    assert set(out) == {"active", "idle"}
    assert "pageToken=p2" in seen[1]
    assert "tok" not in "".join(seen)


def test_service_settings_read_the_describe_document() -> None:
    doc = {
        "metadata": {"annotations": {"run.googleapis.com/minScale": "1"}},
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "autoscaling.knative.dev/maxScale": "3",
                        "run.googleapis.com/execution-environment": "gen1",
                        "run.googleapis.com/cpu-throttling": "false",
                    }
                },
                "spec": {
                    "containerConcurrency": 1,
                    "timeoutSeconds": 3600,
                    "containers": [{"resources": {"limits": {"cpu": "500m", "memory": "512Mi"}}}],
                },
            }
        },
    }
    s = cloudrun.settings(doc)
    assert (s.service_min_instances, s.max_instances, s.generation) == (1, 3, "gen1")
    assert s.cpu_throttled is False
    assert cloudrun.cpu_value(s.cpu) == 0.5
    assert cloudrun.settings({}).cpu_throttled is None
    run = FakeRun([(["describe", "ssc-gateway"], ok(doc))])
    assert cloudrun.describe(run, "ssc-c-cellone01", "ssc-gateway")["spec"]
