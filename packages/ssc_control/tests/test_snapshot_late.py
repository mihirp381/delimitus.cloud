"""SSC-062: a late or failed compile is logged for the ``ssc-snapshot-late`` alert, and nothing
more."""

import logging

import pytest
from procrastinate.jobs import Job
from sqlalchemy.exc import OperationalError

from ssc_control.snapshot import jobs

ORG = "org_aaaaaaaaaaaaaaaaaaaa"


def late_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "snapshot compile late" in r.getMessage()]


def test_a_compile_over_sixty_seconds_is_logged_late(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=jobs.log.name):
        assert jobs.report_late(ORG, 100.0, 160.1)
    assert late_lines(caplog) == ["snapshot compile late: ran long after 60 s"]
    assert caplog.records[0].org_id == ORG  # pyright: ignore[reportAttributeAccessIssue]


def test_a_compile_of_exactly_sixty_seconds_or_less_is_not_late(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=jobs.log.name):
        assert not jobs.report_late(ORG, 100.0, 160.0)
        assert not jobs.report_late(ORG, 100.0, 101.0)
    assert caplog.records == []


def test_a_compile_that_fails_for_good_is_logged_late_however_fast(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=jobs.log.name):
        assert jobs.report_late(ORG, 100.0, 100.5, failed=True)
    assert late_lines(caplog) == ["snapshot compile late: failed for good after 0 s"]


def job(attempts: int) -> Job:
    return Job(
        id=1, queue="default", lock=None, queueing_lock=None, task_name="t", attempts=attempts
    )


def test_the_job_gives_up_when_procrastinate_will_not_retry_it() -> None:
    fault = OperationalError("select", {}, Exception("down"))
    assert jobs.RETRY.get_retry_decision(exception=fault, job=job(0)) is not None
    assert jobs.RETRY.get_retry_decision(exception=fault, job=job(jobs.RETRY.max_attempts)) is None
    assert jobs.RETRY.get_retry_decision(exception=ValueError("bad"), job=job(0)) is None
