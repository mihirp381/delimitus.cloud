"""Record one metrics event in the caller's transaction (``ports.MetricsPort``).

A user id is stored only as its org-scoped pseudonym. ``properties`` are flat scalars under
short snake_case keys, and never carry a user id, an email or a key that names one; a call that
tries is a bug and raises :class:`MetricsPropertyError` before anything is written.

:func:`record_once` writes the usage events (SSC-028, 2026-10-03), which carry no person at all:
each has a ``dedup_key`` that makes it one of a kind in its org, so writing it twice is a no-op.
"""

import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_control.metrics.pseudonym import DerivedKeys, PseudonymKeys, pseudonym
from ssc_control.metrics.source_tool import normalise
from ssc_control.ports import MetricKind, MetricsPort, MetricValue, NullMetricsPort

MAX_PROPERTIES: Final = 20
MAX_VALUE_CHARS: Final = 200
_KEY: Final = re.compile(r"[a-z][a-z0-9_]{0,39}")
_EMAIL: Final = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_USER_ID: Final = re.compile(r"usr_[a-z0-9]{20}")
_APP_ID: Final = re.compile(r"app_[a-z0-9]{20}")
_ENV_ID: Final = re.compile(r"env_[a-z0-9]{20}")
_DEDUP_KEY: Final = re.compile(r"[a-z0-9_:]{1,64}")
_NAMES_A_PERSON: Final = ("email", "user", "pseudonym", "name")

_INSERT: Final = text(
    "insert into ssc.metrics_event (org_id, at, kind, pseudonym, app_id, source_tool, "
    "properties) values (:org, coalesce(:at, now()), :kind, :pseudonym, :app, :tool, "
    "cast(:properties as jsonb))"
)

_INSERT_ONCE: Final = text(
    "insert into ssc.metrics_event (org_id, at, kind, app_id, environment_id, dedup_key, "
    "properties) values (:org, coalesce(:at, now()), :kind, :app, :env, :dedup, "
    "cast(:properties as jsonb)) "
    "on conflict (org_id, kind, dedup_key) where dedup_key is not null do nothing "
    "returning id"
)


class MetricsPropertyError(ValueError):
    """An event that would store something the metrics table must never hold."""


def _checked_properties(properties: Mapping[str, MetricValue] | None) -> dict[str, MetricValue]:
    out: dict[str, MetricValue] = {}
    items = dict(properties or {})
    if len(items) > MAX_PROPERTIES:
        raise MetricsPropertyError(f"at most {MAX_PROPERTIES} properties")
    for key, value in items.items():
        if not _KEY.fullmatch(key):
            raise MetricsPropertyError(f"property key {key!r} is not snake_case")
        if any(word in key for word in _NAMES_A_PERSON):
            raise MetricsPropertyError(f"property key {key!r} names a person")
        if isinstance(value, float) and not math.isfinite(value):
            raise MetricsPropertyError(f"property {key!r} is not a finite number")
        if isinstance(value, str):
            if len(value) > MAX_VALUE_CHARS:
                raise MetricsPropertyError(f"property {key!r} is too long")
            if _USER_ID.search(value) or _EMAIL.search(value):
                raise MetricsPropertyError(f"property {key!r} holds a user id or an email")
        elif not isinstance(value, bool | int | float) and value is not None:
            raise MetricsPropertyError(f"property {key!r} is not a flat scalar")
        out[key] = value
    return out


class Metrics(MetricsPort):
    """The real :class:`ssc_control.ports.MetricsPort`."""

    def __init__(self, keys: PseudonymKeys) -> None:
        self._keys = keys

    async def record_event(  # noqa: PLR0913  (keyword-only)
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        kind: MetricKind,
        app_id: str | None = None,
        user_id: str | None = None,
        source_tool: str | None = None,
        properties: Mapping[str, MetricValue] | None = None,
        at: datetime | None = None,
    ) -> None:
        checked = _checked_properties(properties)
        if app_id is not None and not _APP_ID.fullmatch(app_id):
            raise MetricsPropertyError("app_id is not an app id")
        if at is not None and at.utcoffset() is None:
            raise MetricsPropertyError("at must carry a time zone")
        await conn.execute(
            _INSERT,
            {
                "org": org_id,
                "at": at,
                "kind": MetricKind(kind).value,
                "pseudonym": None if user_id is None else pseudonym(self._keys, org_id, user_id),
                "app": app_id,
                "tool": normalise(source_tool),
                "properties": json.dumps(checked, sort_keys=True, allow_nan=False),
            },
        )


async def record_once(  # noqa: PLR0913  (keyword-only)
    conn: AsyncConnection,
    *,
    org_id: str,
    kind: MetricKind,
    dedup_key: str,
    app_id: str | None = None,
    environment_id: str | None = None,
    properties: Mapping[str, MetricValue] | None = None,
    at: datetime | None = None,
) -> bool:
    """Write one usage event unless its org already has one of ``kind`` with ``dedup_key``.

    True when this call wrote it. No user id and no pseudonym: usage events count, never name.
    """
    checked = _checked_properties(properties)
    if not _DEDUP_KEY.fullmatch(dedup_key):
        raise MetricsPropertyError("dedup_key is not a key")
    if app_id is not None and not _APP_ID.fullmatch(app_id):
        raise MetricsPropertyError("app_id is not an app id")
    if environment_id is not None and not _ENV_ID.fullmatch(environment_id):
        raise MetricsPropertyError("environment_id is not an environment id")
    if at is not None and at.utcoffset() is None:
        raise MetricsPropertyError("at must carry a time zone")
    written = await conn.execute(
        _INSERT_ONCE,
        {
            "org": org_id,
            "at": at,
            "kind": MetricKind(kind).value,
            "app": app_id,
            "env": environment_id,
            "dedup": dedup_key,
            "properties": json.dumps(checked, sort_keys=True, allow_nan=False),
        },
    )
    return written.first() is not None


def metrics_port(master_key: bytes | None) -> MetricsPort:
    """A process's recorder: :class:`Metrics` keyed from ``master_key`` (``Settings.metrics_key``
    or :func:`~ssc_control.metrics.pseudonym.parse_master_key`), or the no-op port without one."""
    return NullMetricsPort() if master_key is None else Metrics(DerivedKeys(master_key))
