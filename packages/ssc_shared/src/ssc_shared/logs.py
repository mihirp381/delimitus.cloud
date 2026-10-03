"""App logs and app health, as the cell keeps them (SSC-024).

The cell agent (``ssc_agent``) reads its project's Cloud Logging through one log view; the
control plane (``ssc_control``) asks it for one app environment's lines or health. Both speak
these types, so they live below both. A query names the service, never a filter: the agent
builds the filter itself, so a caller cannot widen it to another app.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol, cast, get_args

from ssc_shared.runtime import SERVICE_NAME

CellLogSource = Literal["app", "build"]
"""What the cell reads: the app's own output and its Cloud Run request and system logs, or its
builds. Timers join here when they run (SSC-028)."""
CELL_LOG_SOURCES: Final[tuple[CellLogSource, ...]] = get_args(CellLogSource)
LOG_SOURCES: Final = (*CELL_LOG_SOURCES, "deploy")
"""``deploy`` lines come from the control plane's own deployment records, not the cell."""

HealthState = Literal["running", "asleep", "failing"]
HEALTH_STATES: Final[tuple[HealthState, ...]] = get_args(HealthState)

CLOUD_BUILD_REF: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_BUILDS: Final = 5
MAX_LINES: Final = 1000
MAX_SINCE_SECONDS: Final = 7 * 24 * 3600
MAX_WAIT_SECONDS: Final = 20
MAX_CALLER_CHARS: Final = 200
_CURSOR = re.compile(r"[0-9]{1,19}\.[0-9]{1,19}\.[0-9]{1,19}")
_EPOCH0: Final = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class LogQuery:
    """One app environment's lines from one source. ``builds`` are the Cloud Build ids of its
    recent builds, for ``source="build"`` only."""

    service: str
    source: CellLogSource
    builds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if SERVICE_NAME.fullmatch(self.service) is None:
            raise ValueError(f"not an SSC app service name: {self.service!r}")
        if self.source not in CELL_LOG_SOURCES:
            raise ValueError(f"source is one of {', '.join(CELL_LOG_SOURCES)}: {self.source!r}")
        if self.source != "build" and self.builds:
            raise ValueError("builds go with source build only")
        if len(self.builds) > MAX_BUILDS:
            raise ValueError(f"at most {MAX_BUILDS} builds")
        bad = [b for b in self.builds if CLOUD_BUILD_REF.fullmatch(b) is None]
        if bad:
            raise ValueError(f"not Cloud Build ids: {bad}")
        object.__setattr__(self, "builds", tuple(sorted(set(self.builds))))


@dataclass(frozen=True, slots=True, kw_only=True)
class LogLine:
    """One redacted line. ``severity`` is Cloud Logging's (``DEFAULT``, ``INFO``, ``ERROR``...)."""

    timestamp: datetime
    severity: str
    source: str
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class LogPage:
    """Lines oldest first. ``cursor`` continues a follow after the last of them."""

    lines: tuple[LogLine, ...]
    cursor: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Health:
    """``state`` is None when nothing runs (``reason`` says why) or the cell cannot tell."""

    state: HealthState | None
    reason: str
    last_request_at: datetime | None
    checked_at: datetime


class LogsError(Exception):
    """Cloud Logging refused or failed a read."""


class LogsRateLimitedError(LogsError):
    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, retry_after)


class LogsNotConfiguredError(LogsError):
    """The agent has no log view to read."""


class CellLogs(Protocol):
    """``read`` returns the newest ``limit`` lines of the last ``since_seconds``; ``follow``
    waits up to ``wait_seconds`` for lines after ``cursor``. ``caller`` names who asks, for the
    fair share; both raise ``LogsRateLimitedError`` past it. ``health`` sends nothing to the app."""

    async def read(
        self, query: LogQuery, *, since_seconds: int, limit: int, caller: str
    ) -> LogPage: ...

    async def follow(
        self, query: LogQuery, *, cursor: str | None, wait_seconds: float, caller: str
    ) -> LogPage: ...

    async def health(self, service: str, *, caller: str) -> Health: ...


def make_cursor(epoch: int, seq: int, moment: datetime) -> str:
    """``<epoch>.<seq>.<microseconds>``: the agent's position when ``epoch`` is its own, else
    only the time of the last line seen."""
    return f"{epoch}.{seq}.{max(0, (moment - _EPOCH0) // timedelta(microseconds=1))}"


def parse_cursor(cursor: str) -> tuple[int, int, datetime]:
    """Raises ``ValueError`` for anything but a cursor ``make_cursor`` could have made."""
    epoch, seq, micros = (int(part) for part in check_cursor(cursor).split("."))
    try:
        return epoch, seq, _EPOCH0 + timedelta(microseconds=micros)
    except OverflowError:
        raise ValueError("not a log cursor") from None


def check_cursor(cursor: str) -> str:
    if _CURSOR.fullmatch(cursor) is None:
        raise ValueError("not a log cursor")
    return cursor


def check_caller(caller: str) -> str:
    if not 1 <= len(caller) <= MAX_CALLER_CHARS or not caller.isprintable():
        raise ValueError("not a caller")
    return caller


def query_to_wire(query: LogQuery) -> dict[str, object]:
    return {"service": query.service, "source": query.source, "builds": list(query.builds)}


def query_from_wire(body: Mapping[str, Any]) -> LogQuery:
    """Raises ``ValueError`` for anything malformed."""
    try:
        builds: object = body["builds"]
        if not isinstance(builds, list):
            raise TypeError("builds must be a list")
        return LogQuery(
            service=_str(body["service"]),
            source=cast(CellLogSource, _str(body["source"])),
            builds=tuple(_str(b) for b in cast("list[object]", builds)),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed log query: {exc}") from None


def page_to_wire(page: LogPage) -> dict[str, object]:
    return {
        "lines": [
            {
                "timestamp": line.timestamp.isoformat(),
                "severity": line.severity,
                "source": line.source,
                "text": line.text,
            }
            for line in page.lines
        ],
        "cursor": page.cursor,
    }


def page_from_wire(body: Mapping[str, Any]) -> LogPage:
    """Raises ``ValueError`` for anything malformed."""
    try:
        lines: object = body["lines"]
        if not isinstance(lines, list):
            raise TypeError("lines must be a list")
        cursor = body["cursor"]
        return LogPage(
            lines=tuple(_line(cast("Mapping[str, Any]", x)) for x in cast("list[object]", lines)),
            cursor=None if cursor is None else check_cursor(_str(cursor)),
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"malformed log page: {exc}") from None


def health_to_wire(health: Health) -> dict[str, object]:
    seen = health.last_request_at
    return {
        "state": health.state,
        "reason": health.reason,
        "last_request_at": None if seen is None else seen.isoformat(),
        "checked_at": health.checked_at.isoformat(),
    }


def health_from_wire(body: Mapping[str, Any]) -> Health:
    """Raises ``ValueError`` for anything malformed."""
    try:
        state = body["state"]
        if state is not None and state not in HEALTH_STATES:
            raise ValueError(f"unknown health state {state!r}")
        seen = body["last_request_at"]
        return Health(
            state=state,
            reason=_str(body["reason"]),
            last_request_at=None if seen is None else datetime.fromisoformat(_str(seen)),
            checked_at=datetime.fromisoformat(_str(body["checked_at"])),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed health: {exc}") from None


def _line(body: Mapping[str, Any]) -> LogLine:
    return LogLine(
        timestamp=datetime.fromisoformat(_str(body["timestamp"])),
        severity=_str(body["severity"]),
        source=_str(body["source"]),
        text=_str(body["text"]),
    )


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {type(value).__name__}")
    return value
