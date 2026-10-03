"""How much each app in a cell ran, as the cell's Cloud Monitoring counts it (SSC-028).

The cell agent (``ssc_agent``) reads Cloud Run's own metrics for every SSC app service in its
project in one batch; the control plane (``ssc_control``) asks it for whole UTC hours and turns
the answer into usage events. Both speak these types, so they live below both. Only counts and
durations cross: never a request, a path, a user or an address. Nothing here calls an app.

- ``instance_seconds``: Cloud Run's billable instance time of the service in that hour.
- ``active_seconds``: 60 for each minute of the hour in which the service had an instance
  serving at least one request (an open WebSocket or stream is a request the whole time).
- ``ColdStart``: the instances that started in one minute and their mean startup time.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, cast

from ssc_shared.runtime import SERVICE_NAME

HOUR: Final = timedelta(hours=1)
MINUTE: Final = timedelta(minutes=1)
MAX_WINDOW_HOURS: Final = 6


def whole_hour(moment: datetime) -> bool:
    """True for a UTC-aware time on an hour boundary."""
    return moment.utcoffset() is not None and moment.timestamp() % 3600 == 0


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageWindow:
    """Whole UTC hours ``[start, end)``, at most ``MAX_WINDOW_HOURS`` of them."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not (whole_hour(self.start) and whole_hour(self.end)):
            raise ValueError("a usage window starts and ends on a whole hour")
        if not HOUR <= self.end - self.start <= MAX_WINDOW_HOURS * HOUR:
            raise ValueError(f"a usage window is 1 to {MAX_WINDOW_HOURS} hours")
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        object.__setattr__(self, "end", self.end.astimezone(UTC))


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceHour:
    service: str
    hour: datetime
    instance_seconds: float
    active_seconds: int

    def __post_init__(self) -> None:
        _check_service(self.service)
        if not whole_hour(self.hour):
            raise ValueError("an hour starts on a whole hour")
        if not (math.isfinite(self.instance_seconds) and self.instance_seconds >= 0):
            raise ValueError("instance_seconds is a number of seconds")
        if not 0 <= self.active_seconds <= HOUR.total_seconds():
            raise ValueError("active_seconds is within the hour")


@dataclass(frozen=True, slots=True, kw_only=True)
class ColdStart:
    service: str
    minute: datetime
    count: int
    duration_ms: float

    def __post_init__(self) -> None:
        _check_service(self.service)
        if self.minute.utcoffset() is None or self.minute.timestamp() % 60 != 0:
            raise ValueError("a cold start minute starts on a whole minute")
        if self.count < 1:
            raise ValueError("count is at least one")
        if not (math.isfinite(self.duration_ms) and self.duration_ms >= 0):
            raise ValueError("duration_ms is a number of milliseconds")


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageReport:
    hours: tuple[ServiceHour, ...]
    cold_starts: tuple[ColdStart, ...]


class UsageError(Exception):
    """Cloud Monitoring refused or failed a read."""


class UsageNotConfiguredError(UsageError):
    """The agent has no usage source, or may not read it."""


class CellUsage(Protocol):
    """``read`` returns the usage of every SSC app service in the cell over ``window``."""

    async def read(self, window: UsageWindow) -> UsageReport: ...


def window_to_wire(window: UsageWindow) -> dict[str, object]:
    return {"start": window.start.isoformat(), "end": window.end.isoformat()}


def window_from_wire(body: Mapping[str, Any]) -> UsageWindow:
    """Raises ``ValueError`` for anything malformed."""
    try:
        return UsageWindow(start=_time(body["start"]), end=_time(body["end"]))
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed usage window: {exc}") from None


def report_to_wire(report: UsageReport) -> dict[str, object]:
    return {
        "hours": [
            {
                "service": h.service,
                "hour": h.hour.isoformat(),
                "instance_seconds": h.instance_seconds,
                "active_seconds": h.active_seconds,
            }
            for h in report.hours
        ],
        "cold_starts": [
            {
                "service": c.service,
                "minute": c.minute.isoformat(),
                "count": c.count,
                "duration_ms": c.duration_ms,
            }
            for c in report.cold_starts
        ],
    }


def report_from_wire(body: Mapping[str, Any]) -> UsageReport:
    """Raises ``ValueError`` for anything malformed."""
    try:
        return UsageReport(
            hours=tuple(
                ServiceHour(
                    service=_str(h["service"]),
                    hour=_time(h["hour"]),
                    instance_seconds=_number(h["instance_seconds"]),
                    active_seconds=_whole(h["active_seconds"]),
                )
                for h in _list(body["hours"])
            ),
            cold_starts=tuple(
                ColdStart(
                    service=_str(c["service"]),
                    minute=_time(c["minute"]),
                    count=_whole(c["count"]),
                    duration_ms=_number(c["duration_ms"]),
                )
                for c in _list(body["cold_starts"])
            ),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed usage report: {exc}") from None


def _check_service(service: str) -> None:
    if SERVICE_NAME.fullmatch(service) is None:
        raise ValueError(f"not an SSC app service name: {service!r}")


def _list(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise TypeError("expected a list")
    return [cast("Mapping[str, Any]", x) for x in cast("list[object]", value)]


def _time(value: object) -> datetime:
    moment = datetime.fromisoformat(_str(value))
    if moment.utcoffset() is None:
        raise ValueError("a time must carry a time zone")
    return moment


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"expected a number, got {type(value).__name__}")
    return float(value)


def _whole(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected an integer, got {type(value).__name__}")
    return value


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {type(value).__name__}")
    return value
