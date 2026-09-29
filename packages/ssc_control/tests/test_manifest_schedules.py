"""B1's manifest schedules satisfy the timers port's ``DeclaredSchedule`` (A5 reads them)."""

from typing import get_protocol_members, get_type_hints

from ssc_contracts.manifest import load_manifest
from ssc_control.ports import DeclaredSchedule


def test_manifest_schedules_satisfy_declared_schedule() -> None:
    m = load_manifest(
        'schema = "ssc/v1"\n[[schedules]]\nname = "nightly"\ncron = "0 3 * * *"\n'
        'path = "/tasks/nightly"\n'
    )
    (schedule,) = m.schedules
    members = get_protocol_members(DeclaredSchedule)
    assert members == {"name", "cron", "timezone", "path", "method", "timeout_seconds"}
    for member in members:
        expected = get_type_hints(getattr(DeclaredSchedule, member).fget)["return"]
        assert isinstance(getattr(schedule, member), expected), member
    assert (schedule.method, schedule.timeout_seconds) == ("POST", 60)
