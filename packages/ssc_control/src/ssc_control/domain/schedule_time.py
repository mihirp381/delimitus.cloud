"""When a schedule runs next (SSC-041, decision 020). Pure: no I/O but the pinned zone data.

A cron expression is read by cronsim in the schedule's IANA zone. Zones come from the pinned
``tzdata`` package, never the host's zone files, so every worker and every test computes the same
instants.

``next_after`` returns the first instant strictly later, in UTC, than ``after``. Callers pass the
later of the last scheduled instant and now, so a run delayed by an outage fires once, late, and
the schedule then skips to the next future instant: missed runs coalesce. At clock changes:

* fall back: a fixed wall time that happens twice (``30 1 * * *``) fires once, at its first
  occurrence; an interval (``0 * * * *``, ``*/15 1 * * *``) fires in both hours;
* spring forward: a wall time the clock skips (``30 2 * * *``) fires once, when the gap ends.
"""

from datetime import UTC, datetime
from functools import cache
from importlib import resources
from typing import Final
from zoneinfo import ZoneInfo

from cronsim import CronSim

MAX_CANDIDATES: Final = 1000
"""Candidates examined before giving up. Only fall-back folds yield candidates that are not
later than ``after``, a few per call; a manifest cron always has an instant within eight years."""


class UnknownZoneError(ValueError):
    pass


class NoNextRunError(ValueError):
    pass


@cache
def _zone_names() -> frozenset[str]:
    return frozenset(resources.files("tzdata").joinpath("zones").read_text().split())


@cache
def zone(name: str) -> ZoneInfo:
    """The IANA zone ``name`` from the pinned ``tzdata`` package; one object per name."""
    if name == "UTC":
        name = "Etc/UTC"
    if name not in _zone_names():
        raise UnknownZoneError(f"{name!r} is not in the pinned time zone data")
    data = resources.files("tzdata").joinpath("zoneinfo", *name.split("/"))
    with data.open("rb") as f:
        return ZoneInfo.from_file(f, key=name)


def next_after(cron: str, tz: str, after: datetime) -> datetime:
    """The first instant of ``cron`` in zone ``tz`` strictly later than ``after``, in UTC."""
    if after.utcoffset() is None:
        raise ValueError("after must carry a UTC offset")
    floor = after.astimezone(UTC)
    candidates = CronSim(cron, floor.astimezone(zone(tz)))
    for _ in range(MAX_CANDIDATES):
        instant = next(candidates).astimezone(UTC)
        if instant > floor:
            return instant
    raise NoNextRunError(f"{cron!r} in {tz} has no instant after {floor.isoformat()}")
