"""``timeout_for``: how long Cloud Run lets one request to an app run."""

from typing import Any

from ssc_contracts.manifest import Manifest
from ssc_shared.runtime import REQUEST_TIMEOUT_SECONDS, SESSION_TIMEOUT_SECONDS, timeout_for


def manifest(runtime: dict[str, Any] | None = None, *timeouts: int) -> Manifest:
    schedules = [
        {"name": f"s{i}", "cron": "0 3 * * *", "path": "/t", "timeout_seconds": t}
        for i, t in enumerate(timeouts)
    ]
    return Manifest.model_validate(
        {"schema": "ssc/v1", "runtime": runtime or {}, "schedules": schedules}
    )


def test_a_request_app_without_schedules_gets_the_request_timeout() -> None:
    m = manifest()
    assert timeout_for(m.runtime, None, m.schedules) == REQUEST_TIMEOUT_SECONDS
    assert timeout_for(m.runtime) == REQUEST_TIMEOUT_SECONDS


def test_a_short_schedule_keeps_the_request_timeout() -> None:
    m = manifest(None, 60, 120)
    assert timeout_for(m.runtime, None, m.schedules) == REQUEST_TIMEOUT_SECONDS


def test_the_longest_schedule_raises_a_request_apps_timeout() -> None:
    m = manifest(None, 60, 900, 600)
    assert timeout_for(m.runtime, None, m.schedules) == 900


def test_a_session_app_keeps_the_session_timeout() -> None:
    m = manifest({"sessions": True}, 900)
    assert timeout_for(m.runtime, None, m.schedules) == SESSION_TIMEOUT_SECONDS
