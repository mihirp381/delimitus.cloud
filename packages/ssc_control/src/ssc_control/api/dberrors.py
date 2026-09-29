"""One place that turns a database error into a catalogue code.

The SQLSTATE and constraint name are evidence: they go to the log under the request id. The
client sees the fixed text for the code. Anything not listed is ``INTERNAL``: ``SC001`` (no org
bound) and ``42501`` (a row-level security refusal) mean a bug in this service, not a bad request.
"""

from collections.abc import Mapping
from typing import Final

import psycopg
from sqlalchemy.exc import DBAPIError

from ssc_contracts.errors import ErrorCode
from ssc_control.db.errors import (
    CHECK_VIOLATION,
    FOREIGN_KEY_VIOLATION,
    NOT_NULL_VIOLATION,
    UNIQUE_VIOLATION,
    SqlState,
)

ONE_IN_FLIGHT_INDEX: Final = "deployment_one_in_flight"

# Constraints whose refusal has its own code, whatever the SQLSTATE class says.
_BY_CONSTRAINT: Final[Mapping[str, ErrorCode]] = {
    "approval_request_not_self": ErrorCode.SELF_APPROVAL_REFUSED,
    "approval_request_decided_via_agent_check": ErrorCode.AGENT_SESSION_REFUSED,
    "approval_request_one_pending": ErrorCode.ALREADY_EXISTS,
}

_BY_SQLSTATE: Final[Mapping[str, ErrorCode]] = {
    UNIQUE_VIOLATION: ErrorCode.ALREADY_EXISTS,
    FOREIGN_KEY_VIOLATION: ErrorCode.REFERENCE_NOT_FOUND,
    NOT_NULL_VIOLATION: ErrorCode.VALIDATION_FAILED,
    CHECK_VIOLATION: ErrorCode.VALIDATION_FAILED,
    SqlState.LAST_ORG_ADMIN.value: ErrorCode.LAST_ORG_ADMIN,
    SqlState.OWNER_NOT_ACTIVE.value: ErrorCode.OWNER_NOT_ACTIVE,
    SqlState.RELEASE_IMMUTABLE.value: ErrorCode.RECORD_IMMUTABLE,
    SqlState.AUDIT_IMMUTABLE.value: ErrorCode.RECORD_IMMUTABLE,
    SqlState.TRUNCATE_REFUSED.value: ErrorCode.RECORD_IMMUTABLE,
    SqlState.SCHEDULE_DELETED.value: ErrorCode.SCHEDULE_DELETED,
}


def classify(exc: DBAPIError) -> tuple[ErrorCode, dict[str, object]]:
    """The catalogue code for a database error, and the evidence to log."""
    orig = exc.orig
    sqlstate: str | None = None
    constraint: str | None = None
    table: str | None = None
    if isinstance(orig, psycopg.Error):
        sqlstate = orig.sqlstate
        constraint = orig.diag.constraint_name
        table = orig.diag.table_name
    evidence: dict[str, object] = {
        "sqlstate": sqlstate,
        "constraint": constraint,
        "table": table,
        "error": type(orig).__name__ if orig is not None else type(exc).__name__,
    }
    if sqlstate is None:
        return ErrorCode.INTERNAL, evidence
    if sqlstate == UNIQUE_VIOLATION and constraint == ONE_IN_FLIGHT_INDEX:
        return ErrorCode.DEPLOYMENT_IN_FLIGHT, evidence
    if constraint is not None and constraint in _BY_CONSTRAINT:
        return _BY_CONSTRAINT[constraint], evidence
    if sqlstate.startswith("22"):  # data exception: bad literal, out of range, ...
        return ErrorCode.VALIDATION_FAILED, evidence
    return _BY_SQLSTATE.get(sqlstate, ErrorCode.INTERNAL), evidence
