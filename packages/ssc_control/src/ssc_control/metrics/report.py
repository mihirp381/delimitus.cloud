"""The SSC-028 metrics report and the pilot kill criteria (Build Path §3.6, SSC-060).

``uv run python -m ssc_control.metrics.report --org <org_id> [--since DATE] [--as-of DATE]
[--invited-builders N] [--approved-path yes|no] [--paid yes|no] [--json]``

Read-only: one ``REPEATABLE READ`` snapshot as the app role, bound to the org, using
``SSC_DATABASE_DSN``. Days are UTC. The window runs from ``--since`` (default: the day the org was
created) through ``--as-of`` (default: today); pilot day 1 is the first day of the window.

A builder is a pseudonym that deployed or shared; a user is a pseudonym that opened an app.
Counts are exact and always shown. A rate, a mean or a percentile over fewer than 20 is replaced
by ``insufficient data (n=…, need 20)``; Wilson intervals are for proportions only. The kill
criteria always show their observed value, flagged ``small sample`` under 20, because each is a
threshold agreed in writing on a known population.

Kill criteria: pilot week 6 is days 36 to 42 and every builder active in it counts as invited;
the day-60 criteria count days 1 to 60; "side by side" reads each active app's latest deploy up
to ``--as-of``, with the app's status as it is now. Two misses by day 60 means stop (SSC-060).

Usage (2026-10-03) is per environment per UTC month, for every month the window touches, and the
cell's fixed resources with the time each was created (``metrics.usage``). It is for metrics and
the cost view only; nothing bills from it. The month's bill against the cost model is
``metrics.reconcile`` (SSC-096).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from statistics import fmean
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_control.db.bind import bound_org, check_org_id
from ssc_control.db.engine import make_engine
from ssc_control.domain.stats import (
    NO_RATE_BELOW_N,
    Interval,
    format_rate,
    insufficient,
    is_reportable,
    percentile,
    versus,
    wilson,
)
from ssc_control.metrics.source_tool import OTHER
from ssc_control.metrics.usage import (
    EnvironmentUsage,
    FixedResource,
    environment_usage,
    fixed_resources,
    month_of,
    month_span,
)
from ssc_control.ports import MetricKind

FORMAT: Final = "ssc-metrics-report/v1"
DSN_ENV: Final = "SSC_DATABASE_DSN"
NOT_DECLARED: Final = "not declared"
WEEK6: Final = (35, 42)
"""Pilot week 6 as day offsets from the first day, end exclusive."""
DAY60: Final = 60
BUILDER_KINDS: Final = (MetricKind.DEPLOY.value, MetricKind.SHARE.value)
USER_KINDS: Final = (MetricKind.APP_OPENED.value,)
DATA_KINDS: Final = (MetricKind.DATA_QUERY.value, MetricKind.DATABASE_USE.value)

Verdict = Literal["met", "missed", "not_due", "needs_input", "not_recorded"]
Decision = Literal["stop", "undecided", "continue"]
_OPEN: Final[frozenset[Verdict]] = frozenset({"not_due", "needs_input", "not_recorded"})

# ── SQL ──────────────────────────────────────────────────────────────────────

_ORG_CREATED = text("select created_at from ssc.org where id = :org")
_KIND_COUNTS = text(
    "select kind, count(*) from ssc.metrics_event "
    "where org_id = :org and at >= :lo and at < :hi group by kind"
)
_FIRST_URL_SECONDS = text(
    "select greatest(0, extract(epoch from first_at - created_at))::float8 from ("
    " select a.created_at, min(m.at) as first_at from ssc.metrics_event m"
    " join ssc.app a on a.org_id = m.org_id and a.id = m.app_id"
    " where m.org_id = :org and m.kind = 'first_url' and m.at < :hi"
    " group by a.id, a.created_at"
    ") per_app where first_at >= :lo order by 1"
)
_APPS_PER_BUILDER = text(
    "select count(distinct app_id) from ssc.metrics_event "
    "where org_id = :org and kind = 'deploy' and pseudonym is not null and app_id is not null "
    "and at >= :lo and at < :hi group by pseudonym order by 1"
)
_WEEKLY_PSEUDONYMS = text(
    "select to_char(date_trunc('week', at at time zone 'UTC'), 'IYYY-\"W\"IW') as week, "
    "count(distinct pseudonym) from ssc.metrics_event "
    "where org_id = :org and kind = any(cast(:kinds as text[])) and pseudonym is not null "
    "and at >= :lo and at < :hi group by week order by week"
)
_ACTIVE_BUILDERS = text(
    "select count(distinct pseudonym) from ssc.metrics_event "
    "where org_id = :org and kind = any(cast(:kinds as text[])) and pseudonym is not null "
    "and at >= :lo and at < :hi"
)
_APP_USAGE = text(
    "with deployed as ("
    " select distinct app_id from ssc.metrics_event"
    " where org_id = :org and kind = 'deploy' and app_id is not null and at >= :lo and at < :hi"
    ") select count(*) filter (where exists ("
    " select 1 from ssc.metrics_event u where u.org_id = :org and u.app_id = deployed.app_id"
    " and u.kind = any(cast(:kinds as text[])) and u.at >= :lo and u.at < :hi)),"
    " count(*) from deployed"
)
_TOOL_DEPLOYS = text(
    "select source_tool, count(*) from ssc.metrics_event "
    "where org_id = :org and kind = 'deploy' and at >= :lo and at < :hi "
    "group by source_tool order by count(*) desc, source_tool nulls last"
)
_RUNNING_TOOLS = text(
    "select source_tool, count(*) from ("
    " select distinct on (m.app_id) m.source_tool from ssc.metrics_event m"
    " join ssc.app a on a.org_id = m.org_id and a.id = m.app_id"
    " where m.org_id = :org and m.kind = 'deploy' and m.at < :hi and a.status = 'active'"
    " order by m.app_id, m.at desc, m.id desc"
    ") latest group by source_tool order by count(*) desc, source_tool nulls last"
)

# ── facts ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Window:
    day0: date
    as_of: date

    @property
    def elapsed_days(self) -> int:
        return (self.as_of - self.day0).days + 1

    def span(self, first: int = 0, end: int | None = None) -> tuple[datetime, datetime]:
        """Pilot days ``[first, end)`` as UTC instants, never past the end of ``as_of``."""
        start = datetime.combine(self.day0, time(), UTC)
        until = datetime.combine(self.as_of + timedelta(days=1), time(), UTC)
        hi = until if end is None else min(until, start + timedelta(days=end))
        return start + timedelta(days=first), hi


Counts = tuple[tuple[str | None, int], ...]


@dataclass(frozen=True, slots=True)
class Facts:
    """Everything the report reads, fetched in one snapshot."""

    events: dict[str, int]
    first_url_seconds: tuple[float, ...]
    apps_per_builder: tuple[int, ...]
    weekly_users: Counts
    weekly_builders: Counts
    app_usage: tuple[int, int]
    """(apps that used data or state, apps deployed)."""
    tool_deploys: Counts
    week6_builders: int
    day60_apps_per_builder: tuple[int, ...]
    day60_app_usage: tuple[int, int]
    running_tools: Counts
    usage: tuple[tuple[date, tuple[EnvironmentUsage, ...]], ...] = ()
    fixed_resources: tuple[FixedResource, ...] = ()


class ReportError(Exception):
    """A report that cannot be made as asked; the message says why."""


async def org_created(conn: AsyncConnection, org_id: str) -> date:
    created = (await conn.execute(_ORG_CREATED, {"org": org_id})).scalar_one_or_none()
    if created is None:
        raise ReportError(f"org {org_id} not found")
    return created.astimezone(UTC).date()


async def gather(conn: AsyncConnection, org_id: str, window: Window) -> Facts:
    async def rows(sql: Any, lo: datetime, hi: datetime, **extra: object) -> list[Any]:
        return list((await conn.execute(sql, {"org": org_id, "lo": lo, "hi": hi, **extra})).all())

    async def usage(lo: datetime, hi: datetime) -> tuple[int, int]:
        ((used, deployed),) = await rows(_APP_USAGE, lo, hi, kinds=list(DATA_KINDS))
        return int(used), int(deployed)

    lo, hi = window.span()
    d60 = window.span(0, DAY60)
    w6 = window.span(*WEEK6)
    return Facts(
        events={str(k): int(n) for k, n in await rows(_KIND_COUNTS, lo, hi)},
        first_url_seconds=tuple(float(s) for (s,) in await rows(_FIRST_URL_SECONDS, lo, hi)),
        apps_per_builder=tuple(int(n) for (n,) in await rows(_APPS_PER_BUILDER, lo, hi)),
        weekly_users=_counts(await rows(_WEEKLY_PSEUDONYMS, lo, hi, kinds=list(USER_KINDS))),
        weekly_builders=_counts(await rows(_WEEKLY_PSEUDONYMS, lo, hi, kinds=list(BUILDER_KINDS))),
        app_usage=await usage(lo, hi),
        tool_deploys=_counts(await rows(_TOOL_DEPLOYS, lo, hi)),
        week6_builders=int((await rows(_ACTIVE_BUILDERS, *w6, kinds=list(BUILDER_KINDS)))[0][0]),
        day60_apps_per_builder=tuple(int(n) for (n,) in await rows(_APPS_PER_BUILDER, *d60)),
        day60_app_usage=await usage(*d60),
        running_tools=_counts(await rows(_RUNNING_TOOLS, lo, hi)),
        usage=tuple(
            [(m, tuple(await environment_usage(conn, org_id, m))) for m in window_months(window)]
        ),
        fixed_resources=tuple(await fixed_resources(conn, org_id)),
    )


def window_months(window: Window) -> tuple[date, ...]:
    """The first day of every UTC month from ``day0`` through ``as_of``."""
    months: list[date] = []
    month, last = window.day0.replace(day=1), window.as_of.replace(day=1)
    while month <= last:
        months.append(month)
        month = month_of(month_span(month)[1])
    return tuple(months)


def _counts(rows: Sequence[Any]) -> Counts:
    return tuple((None if k is None else str(k), int(n)) for k, n in rows)


# ── kill criteria ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PilotInputs:
    """What the database cannot know: the invited builders and the two written facts."""

    invited_builders: int | None = None
    approved_path: bool | None = None
    paid: bool | None = None


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    label: str
    target: float | None
    due_day: int
    verdict: Verdict
    observed: float | None = None
    n: int | None = None
    interval: Interval | None = None
    note: str | None = None
    estimate: bool = True
    """False for a plain count (the tools criterion), which no sample size qualifies."""

    @property
    def small_sample(self) -> bool:
        return self.estimate and self.n is not None and not is_reportable(self.n)


def _at_least(observed: float, target: float) -> Verdict:
    return "met" if observed >= target else "missed"


def _active_builders(facts: Facts, window: Window, invited: int | None) -> Criterion:
    base = Criterion(
        "active_builders_week6",
        "≥40% of invited builders active weekly by week 6",
        0.40,
        WEEK6[1],
        "not_due",
    )
    if invited is None:
        return replace(base, verdict="needs_input", note="pass --invited-builders")
    if window.elapsed_days < WEEK6[1]:
        return base
    active = facts.week6_builders
    if active > invited:
        note = f"{active} builders active in week 6, more than {invited} invited"
        return replace(base, verdict="needs_input", note=note)
    iv = wilson(active, invited)
    return replace(
        base, verdict=_at_least(iv.point, 0.40), observed=iv.point, n=invited, interval=iv
    )


def _apps_per_active_builder(facts: Facts, window: Window) -> Criterion:
    base = Criterion(
        "apps_per_builder_day60", "≥2 apps per active builder by day 60", 2.0, DAY60, "not_due"
    )
    if window.elapsed_days < DAY60:
        return base
    per = facts.day60_apps_per_builder
    if not per:
        return replace(base, verdict="not_recorded", n=0, note="no deploy events by day 60")
    mean = fmean(per)
    return replace(base, verdict=_at_least(mean, 2.0), observed=mean, n=len(per))


def _apps_using_data(facts: Facts, window: Window) -> Criterion:
    base = Criterion(
        "apps_using_data_day60",
        "≥30% of apps using company data or app state by day 60",
        0.30,
        DAY60,
        "not_due",
    )
    if window.elapsed_days < DAY60:
        return base
    used, deployed = facts.day60_app_usage
    if deployed == 0:
        return replace(base, verdict="not_recorded", n=0, note="no deploy events by day 60")
    iv = wilson(used, deployed)
    return replace(
        base, verdict=_at_least(iv.point, 0.30), observed=iv.point, n=deployed, interval=iv
    )


def _tools_side_by_side(facts: Facts, window: Window) -> Criterion:
    base = Criterion(
        "tools_side_by_side",
        "apps from ≥2 builder tools running side by side",
        2.0,
        DAY60,
        "not_due",
        estimate=False,
    )
    running = sum(n for _, n in facts.running_tools)
    tools = sorted(t for t, _ in facts.running_tools if t is not None and t != OTHER)
    note = ", ".join(tools) or None
    if running == 0:
        verdict: Verdict = "not_recorded" if window.elapsed_days >= DAY60 else "not_due"
        return replace(base, verdict=verdict, n=0, note="no running app has a recorded deploy")
    verdict = _at_least(len(tools), 2.0)
    if verdict == "missed" and window.elapsed_days < DAY60:
        verdict = "not_due"
    return replace(base, verdict=verdict, observed=float(len(tools)), n=running, note=note)


def _written(id_: str, label: str, value: bool | None, flag: str, window: Window) -> Criterion:
    base = Criterion(id_, label, None, DAY60, "not_due")
    if value is None:
        return replace(base, verdict="needs_input", note=f"pass {flag} yes|no")
    if value:
        return replace(base, verdict="met", observed=1.0)
    return replace(
        base, verdict="missed" if window.elapsed_days >= DAY60 else "not_due", observed=0.0
    )


def criteria(facts: Facts, window: Window, pilot: PilotInputs) -> tuple[Criterion, ...]:
    return (
        _active_builders(facts, window, pilot.invited_builders),
        _apps_per_active_builder(facts, window),
        _apps_using_data(facts, window),
        _tools_side_by_side(facts, window),
        _written(
            "approved_path",
            'a written "approved path" designation',
            pilot.approved_path,
            "--approved-path",
            window,
        ),
        _written("paid_conversion", "paid conversion", pilot.paid, "--paid", window),
    )


@dataclass(frozen=True, slots=True)
class StopRule:
    """SSC-060: two misses by day 60 means stop and rethink."""

    misses: int
    open: int

    @property
    def decision(self) -> Decision:
        if self.misses >= 2:
            return "stop"
        return "undecided" if self.misses + self.open >= 2 else "continue"


def stop_rule(found: Sequence[Criterion]) -> StopRule:
    return StopRule(
        misses=sum(c.verdict == "missed" for c in found),
        open=sum(c.verdict in _OPEN for c in found),
    )


# ── the report ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Report:
    org_id: str
    window: Window
    facts: Facts
    criteria: tuple[Criterion, ...]

    @property
    def stop(self) -> StopRule:
        return stop_rule(self.criteria)


def build(org_id: str, window: Window, facts: Facts, pilot: PilotInputs) -> Report:
    return Report(org_id, window, facts, criteria(facts, window, pilot))


def _num(value: float | None) -> float | None:
    return None if value is None or math.isnan(value) else value


def _rate_json(iv: Interval) -> dict[str, Any]:
    shown = is_reportable(iv.n)
    return {
        "successes": iv.successes,
        "n": iv.n,
        "small_sample": not shown,
        "rate": _num(iv.point) if shown else None,
        "low": _num(iv.low) if shown else None,
        "high": _num(iv.high) if shown else None,
    }


def _distribution(per: Sequence[int]) -> dict[str, int]:
    return {str(k): v for k, v in sorted(Counter(per).items())}


def as_json(report: Report) -> dict[str, Any]:
    f = report.facts
    ttfu, per = f.first_url_seconds, f.apps_per_builder
    ttfu_shown, per_shown = is_reportable(len(ttfu)), is_reportable(len(per))
    deploys = sum(n for _, n in f.tool_deploys)
    used, deployed = f.app_usage
    stop = report.stop
    return {
        "format": FORMAT,
        "org_id": report.org_id,
        "window": {
            "since": report.window.day0.isoformat(),
            "as_of": report.window.as_of.isoformat(),
            "elapsed_days": report.window.elapsed_days,
        },
        "min_n": NO_RATE_BELOW_N,
        "events": {k.value: f.events.get(k.value, 0) for k in MetricKind},
        "metrics": {
            "time_to_first_url": {
                "n": len(ttfu),
                "small_sample": not ttfu_shown,
                "median_seconds": percentile(ttfu, 0.5) if ttfu_shown else None,
                "p75_seconds": percentile(ttfu, 0.75) if ttfu_shown else None,
            },
            "apps_per_builder": {
                "n": len(per),
                "small_sample": not per_shown,
                "mean": fmean(per) if per_shown else None,
                "distribution": _distribution(per),
            },
            "weekly_unique_users": [{"week": w, "users": n} for w, n in f.weekly_users],
            "apps_using_data_or_state": _rate_json(wilson(used, deployed)),
            "source_tool_mix": {
                "n": deploys,
                "tools": [
                    {"tool": t, "deploys": n, **_rate_json(wilson(n, deploys))}
                    for t, n in f.tool_deploys
                ],
            },
            "weekly_active_builders": [{"week": w, "builders": n} for w, n in f.weekly_builders],
            "usage": [
                {"month": m.strftime("%Y-%m"), "environments": [_usage_json(u) for u in found]}
                for m, found in f.usage
            ],
            "fixed_resources": [
                {"resource": r.resource, "created_at": r.created_at.isoformat()}
                for r in f.fixed_resources
            ],
        },
        "kill_criteria": [
            {
                "id": c.id,
                "label": c.label,
                "target": c.target,
                "due_day": c.due_day,
                "verdict": c.verdict,
                "observed": _num(c.observed),
                "n": c.n,
                "small_sample": c.small_sample,
                "low": _num(c.interval.low) if c.interval else None,
                "high": _num(c.interval.high) if c.interval else None,
                "versus_target": (
                    versus(c.interval, c.target) if c.interval and c.target is not None else None
                ),
                "note": c.note,
            }
            for c in report.criteria
        ],
        "stop_rule": {"misses": stop.misses, "open": stop.open, "decision": stop.decision},
    }


def _usage_json(u: EnvironmentUsage) -> dict[str, Any]:
    return {
        "environment_id": u.environment_id,
        "app_id": u.app_id,
        "usage_type": u.usage_type,
        "session_hours": u.session_hours,
        "instance_hours": u.instance_hours,
        "cold_starts": u.cold_starts,
        "small_sample": u.small_sample,
        "cold_start_p50_seconds": u.cold_start_p50_seconds,
        "cold_start_p95_seconds": u.cold_start_p95_seconds,
        "active_days": u.active_days,
    }


def _usage_lines(f: Facts) -> list[str]:
    out: list[str] = []
    for month, found in f.usage:
        out.append(f"   {month:%Y-%m}")
        if not found:
            out.append("      none: no usage_hour or cold_start events")
        for u in found:
            starts = (
                f"cold starts {u.cold_starts}, insufficient data (n={u.cold_starts}, "
                f"need {NO_RATE_BELOW_N})"
                if u.small_sample
                else f"cold starts {u.cold_starts}, p50 {u.cold_start_p50_seconds:.1f}s, "
                f"p95 {u.cold_start_p95_seconds:.1f}s"
            )
            out.append(
                f"      {u.environment_id} {u.usage_type or 'no usage'}: "
                f"session {u.session_hours:.2f} h, instance {u.instance_hours:.2f} h, "
                f"{starts}, {u.active_days} active days"
            )
    return out


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes:02d}m"
    return f"{hours / 24:.1f}d"


def _weeks(counts: Counts, what: str, kind: str) -> str:
    if not counts:
        return f"   none: no {kind} events"
    return "\n".join(f"   {w}  {n} {what}" for w, n in counts)


_VERSUS: Final = {
    "above": "interval above target",
    "below": "interval below target",
    "indistinguishable": "interval spans target",
}


def _observed(c: Criterion) -> str:
    if c.verdict == "not_due":
        return f"due after day {c.due_day}" + (f" ({c.note})" if c.note else "")
    if c.observed is None:
        return c.note or "no value"
    if c.target is None:
        return "yes" if c.observed else "no"
    small = ", small sample" if c.small_sample else ""
    if c.interval is not None:
        iv = c.interval
        return (
            f"{iv.point:.1%} ({iv.successes} of {iv.n}) [{iv.low:.1%}–{iv.high:.1%}]"
            f", {_VERSUS[versus(iv, c.target)]}{small}"
        )
    if not c.estimate:
        return f"{c.observed:.0f} ({c.note or 'none known'}) across {c.n} running apps"
    return f"{c.observed:.2f} (n={c.n}{small})"


def render(report: Report) -> str:
    f, w = report.facts, report.window
    ttfu, per = f.first_url_seconds, f.apps_per_builder
    used, deployed = f.app_usage
    deploys = sum(n for _, n in f.tool_deploys)
    out = [
        f"SSC metrics report · {report.org_id}",
        f"Window {w.day0} to {w.as_of} UTC, {w.elapsed_days} days",
        "",
        "Events recorded",
        "   " + " · ".join(f"{k.value} {f.events.get(k.value, 0)}" for k in MetricKind),
        "",
        "1. Time to first URL (first_url minus app created, per app)",
        (
            f"   median {_duration(percentile(ttfu, 0.5))}, "
            f"p75 {_duration(percentile(ttfu, 0.75))} (n={len(ttfu)})"
            if is_reportable(len(ttfu))
            else f"   {insufficient(len(ttfu))}"
        ),
        "2. Apps per builder (distinct apps each deploying builder deployed)",
        (
            f"   mean {fmean(per):.2f} (n={len(per)})"
            if is_reportable(len(per))
            else f"   {insufficient(len(per))}"
        ),
    ]
    if per:
        spread = ", ".join(f"{k} app(s): {v}" for k, v in _distribution(per).items())
        out.append(f"   builders by apps: {spread}")
    out += [
        "3. Weekly unique users (distinct pseudonyms opening an app, ISO weeks)",
        _weeks(f.weekly_users, "users", MetricKind.APP_OPENED.value),
        "4. Share of apps using data or state (of apps deployed in the window)",
        f"   {format_rate(wilson(used, deployed))}",
        "5. Source tool mix (share of deploys)",
    ]
    if deploys == 0:
        out.append(f"   {insufficient(0)}")
    for tool, n in f.tool_deploys:
        rate = format_rate(wilson(n, deploys))
        out.append(f"   {tool or NOT_DECLARED}: {n} of {deploys} deploys, {rate}")
    out += [
        "6. Weekly active builders (distinct pseudonyms deploying or sharing, ISO weeks)",
        _weeks(f.weekly_builders, "builders", "deploy or share"),
        "7. Usage per environment (UTC months; for the cost view, never for billing)",
        *_usage_lines(f),
        "8. Fixed resources of the cell (when each was created)",
        *(
            [
                f"   {r.resource} {r.created_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
                for r in f.fixed_resources
            ]
            or ["   none: no fixed_resource events"]
        ),
        "",
        "Pilot kill criteria (Build Path §3.6, SSC-060)",
    ]
    out += [f"   [{c.verdict}] {c.label}: {_observed(c)}" for c in report.criteria]
    stop = report.stop
    out += [
        f"Stop rule, two misses by day 60: {stop.decision} "
        f"({stop.misses} missed, {stop.open} open)",
        "",
    ]
    return "\n".join(out)


# ── command line ─────────────────────────────────────────────────────────────


async def run(
    dsn: str, org_id: str, *, since: date | None, as_of: date, pilot: PilotInputs
) -> Report:
    engine = make_engine(dsn)
    try:
        snapshot = engine.execution_options(
            isolation_level="REPEATABLE READ", postgresql_readonly=True
        )
        async with bound_org(snapshot, org_id) as conn:
            window = Window(since or await org_created(conn, org_id), as_of)
            if window.day0 > as_of:
                raise ReportError(f"the window starts on {window.day0}, after --as-of")
            facts = await gather(conn, org_id, window)
    finally:
        await engine.dispose()
    return build(org_id, window, facts, pilot)


def _yes_no(value: str) -> bool:
    if value not in {"yes", "no"}:
        raise argparse.ArgumentTypeError("yes or no")
    return value == "yes"


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ssc_control.metrics.report")
    parser.add_argument("--org", required=True, type=check_org_id)
    parser.add_argument("--since", type=date.fromisoformat, help="pilot day 1 (UTC)")
    parser.add_argument("--as-of", type=date.fromisoformat, help="last day counted (UTC)")
    parser.add_argument("--invited-builders", type=_positive)
    parser.add_argument("--approved-path", type=_yes_no, help="yes or no")
    parser.add_argument("--paid", type=_yes_no, help="yes or no")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        parser.error(f"{DSN_ENV} is not set")
    as_of: date = args.as_of or datetime.now(UTC).date()
    if args.since is not None and args.since > as_of:
        parser.error("--since is after --as-of")
    pilot = PilotInputs(args.invited_builders, args.approved_path, args.paid)
    try:
        report = asyncio.run(run(dsn, args.org, since=args.since, as_of=as_of, pilot=pilot))
    except ReportError as e:
        parser.error(str(e))
    if args.json:
        sys.stdout.write(json.dumps(as_json(report), indent=2, allow_nan=False) + "\n")
    else:
        sys.stdout.write(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
