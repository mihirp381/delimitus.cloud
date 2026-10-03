"""SSC-096 monthly cost reconciliation: each project's bill against the cost model
(``cost_model.toml``, architecture section 10) and the SSC-028 events that explain its usage.

``uv run python -m ssc_control.metrics.reconcile --month YYYY-MM --bill BILL.csv
[--cells CELLS.csv] [--org ORG ...] [--reasons REASONS.json] [--json] [--out DIR]``

Inputs:

- ``--bill``: the month's bill as CSV with the columns ``project_id``, ``service`` and ``usd``
  (cost before credits, in dollars), and optionally ``month`` (``YYYY-MM``, which must be the
  month asked for), or as a JSON array of objects with the same keys. Other columns are ignored
  and rows for one project and service are added up.
  ``docs/runbooks/ssc-096-cost-reconciliation.md`` says how to make it from the Cloud Billing
  console. Nothing here calls a billing API.
- The control database at ``SSC_DATABASE_DSN``, read only, one org at a time: each org with a
  cell label is the cell ``ssc-c-<label>``, created when the org was, with its ``usage_hour``,
  ``cold_start`` and ``fixed_resource`` events. ``--org`` limits the read to those orgs.
- ``--cells``: cells no control database knows (the proof-run cells), as CSV with the columns
  ``project_id``, ``resource`` (``cell``, ``database``, ``egress`` or ``connections``) and
  ``created_at`` (ISO 8601 with a zone), one ``cell`` row per project. Without
  ``SSC_DATABASE_DSN`` these are the only cells.
- ``--reasons``: a JSON object from a line's id to the written reason it is off.

What the page holds, per project: the bill, the model's figure and the difference. Per cell, the
fixed cost against the model's parts, each counted from when it was created as a share of the
month's seconds; the usage per app, typed as in ``metrics.usage``; the cost per app against the
target; the measured split of rare, daily and heavy apps against the founder's 70/20/10. Every
line more than the model's tolerance (20 %) off is flagged with a place for its reason. A money
line under ``min_usd`` on both sides is not flagged.

Attribution. A cell's ``Cloud Run`` bill is the usage part. Each app is priced from its
instance seconds in the month at the reference instance (1 vCPU, 512 MiB) and the rate of the
billing its events carry: request-billed seconds at the request rate, instance-billed seconds
(session apps) at the instance rate. When the apps together price at or under the Cloud Run bill,
each app is attributed its price and the rest is one line, "Cloud Run, not apps" (gateway, cell
agent, data gateway, jobs), against the model's gateway: the hours in which any session was open
(the busiest session environment of each hour) at the request rate. When they price over the bill,
every app's price is scaled by bill over price, so the apps share exactly the bill and nothing is
left. Every other service the model does not class as fixed is its own line, not attributed.

Nothing here bills a customer (A6). The numbers are ours, for the model and the price.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import math
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.cells import CellResource
from ssc_control.db.bind import bound_org, check_org_id
from ssc_control.db.engine import make_engine
from ssc_control.db.orgs import all_org_ids
from ssc_control.domain.stats import NO_RATE_BELOW_N, is_reportable
from ssc_control.metrics.cost_model import (
    CELL_BASE,
    Check,
    CostModel,
    Platform,
    ServiceClass,
    UsageKind,
    checks,
    load,
)
from ssc_control.metrics.usage import (
    FixedResource,
    UsageType,
    fixed_resources,
    month_span,
    parse_month,
    usage_type,
)
from ssc_shared.hosts import check_cell_label

FORMAT: Final = "ssc-cost-reconciliation/v1"
DSN_ENV: Final = "SSC_DATABASE_DSN"
CELL_PREFIX: Final = "ssc-c-"
"""A cell's project is ``ssc-c-<cell label>`` (``infra/ssc_infra/naming.py``)."""
BILL_COLUMNS: Final = ("project_id", "service", "usd")
CELLS_COLUMNS: Final = ("project_id", "resource", "created_at")
CLOUD_RUN_REST: Final = "Cloud Run, not apps (gateway, cell agent, data gateway, jobs)"
TYPES: Final[tuple[UsageKind, ...]] = ("rare", "daily", "session", "heavy")
FOOTER: Final = (
    "For us only. Nothing on this page bills a customer (A6): the numbers check the cost model "
    "and the price, and no charge is ever made from them."
)

Status = Literal["ok", "off", "no_bill", "no_model"]
Service = tuple[str, ServiceClass, float]
"""A bill's service, how the model classes it, and its dollars."""
FLAGGED: Final[frozenset[Status]] = frozenset({"off", "no_bill", "no_model"})

_ORG = text("select created_at, cell_label from ssc.org where id = :org")
_SECONDS = text(
    "select coalesce(app_id, environment_id), environment_id, "
    "coalesce(properties->>'billing', 'request'), "
    "coalesce(sum((properties->>'instance_seconds')::float8), 0), "
    "coalesce(sum((properties->>'session_seconds')::float8), 0) "
    "from ssc.metrics_event where org_id = :org and kind = 'usage_hour' "
    "and environment_id is not null and at >= :lo and at < :hi group by 1, 2, 3"
)
_COLD = text(
    "select coalesce(app_id, environment_id), coalesce(sum((properties->>'count')::int), 0) "
    "from ssc.metrics_event where org_id = :org and kind = 'cold_start' "
    "and environment_id is not null and at >= :lo and at < :hi group by 1"
)
_GATEWAY = text(
    "select coalesce(sum(busiest), 0) from (select max((properties->>'session_seconds')::float8) "
    "as busiest from ssc.metrics_event where org_id = :org and kind = 'usage_hour' "
    "and at >= :lo and at < :hi group by at) per_hour"
)


class InputError(Exception):
    """An input the reconciliation cannot read; the message says which and why."""


@dataclass(frozen=True, slots=True)
class BillLine:
    project_id: str
    service: str
    usd: float


@dataclass(frozen=True, slots=True)
class AppMonth:
    """One app's month in one cell, all its environments together."""

    app_id: str
    environments: int
    request_seconds: float = 0.0
    instance_billed_seconds: float = 0.0
    session_seconds: float = 0.0
    cold_starts: int = 0

    @property
    def instance_seconds(self) -> float:
        return self.request_seconds + self.instance_billed_seconds

    @property
    def usage_type(self) -> UsageType | None:
        return usage_type(self.session_seconds, self.instance_seconds)

    def price(self, model: CostModel) -> float:
        """The model's dollars for this month: its seconds at the reference instance."""
        return self.request_seconds * model.per_second(
            "request"
        ) + self.instance_billed_seconds * model.per_second("instance")


@dataclass(frozen=True, slots=True)
class CellMonth:
    project_id: str
    created_at: datetime
    resources: tuple[FixedResource, ...] = ()
    apps: tuple[AppMonth, ...] = ()
    gateway_session_seconds: float = 0.0
    """The busiest session environment's session seconds, summed over the month's hours."""
    org_id: str | None = None


@dataclass(frozen=True, slots=True)
class Line:
    """One compared line: what was measured against the model's figure or band."""

    id: str
    label: str
    measured: float | None
    low: float | None
    high: float | None = None
    tolerance: float = 0.20
    floor: float = 0.0
    billed: bool = True
    """A line read from the bill, so a missing measurement is a missing bill."""
    flags: bool = True
    """False for a line shown for completeness, which the model gives no figure."""
    reason: str | None = None
    note: str | None = None
    unit: Literal["usd", "share"] = "usd"

    @property
    def top(self) -> float | None:
        return self.low if self.high is None else self.high

    @property
    def off(self) -> float | None:
        """How far outside the model, as a share of the nearest edge; None without both."""
        if self.measured is None or self.low is None:
            return None
        top = self.top or 0.0
        if self.low <= self.measured <= top:
            return 0.0
        edge = self.low if self.measured < self.low else top
        return None if edge == 0 else (self.measured - edge) / edge

    @property
    def status(self) -> Status:
        top = self.top or 0.0
        if self.measured is None:
            return "no_bill" if self.billed and self.low is not None and top >= self.floor else "ok"
        if max(self.measured, top) < self.floor or self.measured == top == 0:
            return "ok"
        if self.low is None or self.off is None:
            return "no_model"
        return "off" if abs(self.off) > self.tolerance else "ok"

    @property
    def flagged(self) -> bool:
        return self.flags and self.status in FLAGGED


@dataclass(frozen=True, slots=True)
class PartShare:
    name: str
    label: str
    month_usd: float
    since: datetime
    fraction: float

    @property
    def usd(self) -> float:
        return self.month_usd * self.fraction


@dataclass(frozen=True, slots=True)
class FixedRow:
    project_id: str
    parts: tuple[PartShare, ...]
    line: Line
    shape: Literal["empty", "database", "full"]


@dataclass(frozen=True, slots=True)
class AppRow:
    project_id: str
    app: AppMonth
    model_usd: float
    attributed_usd: float | None


@dataclass(frozen=True, slots=True)
class ProjectRow:
    project_id: str
    kind: Literal["platform", "cell", "cell, no record", "not in the model"]
    line: Line
    services: tuple[Service, ...] = ()


@dataclass(frozen=True, slots=True)
class Reconciliation:
    month: date
    model: CostModel
    bill_name: str
    projects: tuple[ProjectRow, ...]
    fixed: tuple[FixedRow, ...]
    apps: tuple[AppRow, ...]
    services: tuple[Line, ...]
    """Platform lines per service and cell lines not attributed to apps."""
    types: tuple[tuple[Line, int], ...]
    per_app: Line
    split: tuple[tuple[Line, int], ...]
    checks: tuple[Check, ...]
    database: bool

    def lines(self) -> list[Line]:
        """Every compared line in page order."""
        return [
            *(p.line for p in self.projects),
            *(f.line for f in self.fixed),
            *self.services,
            *(t for t, _ in self.types),
            self.per_app,
            *(s for s, _ in self.split),
        ]

    @property
    def flags(self) -> list[Line]:
        return [line for line in self.lines() if line.flagged]


def _bill_rows(raw: str) -> list[dict[str, str]]:
    """The bill's rows: a JSON array of objects, or CSV with a header row."""
    if not raw.lstrip().startswith("["):
        reader = csv.DictReader(io.StringIO(raw))
        missing = [c for c in BILL_COLUMNS if c not in (reader.fieldnames or ())]
        if missing:
            raise InputError(f"the bill has no column {', '.join(missing)}")
        return [{k: v or "" for k, v in row.items() if k is not None} for row in reader]
    try:
        found: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(f"the bill is not JSON: {exc}") from None
    if not isinstance(found, list):
        raise InputError("a JSON bill is one array of objects")
    rows: list[dict[str, str]] = []
    for n, item in enumerate(cast("list[object]", found), start=1):
        if not isinstance(item, dict):
            raise InputError(f"bill entry {n} is not an object")
        entry = cast("dict[object, object]", item)
        missing = [c for c in BILL_COLUMNS if c not in entry]
        if missing:
            raise InputError(f"bill entry {n} has no {', '.join(missing)}")
        rows.append({str(k): "" if v is None else str(v) for k, v in entry.items()})
    return rows


def read_bill(raw: str, month: date) -> tuple[BillLine, ...]:
    """The bill, CSV or a JSON array with the same keys, one line per project and service;
    :class:`InputError` when it is not one."""
    wanted = month.strftime("%Y-%m")
    totals: dict[tuple[str, str], float] = {}
    for n, row in enumerate(_bill_rows(raw), start=2):
        project, service = (row["project_id"] or "").strip(), (row["service"] or "").strip()
        if not project or not service:
            raise InputError(f"bill line {n}: project_id and service are required")
        given = (row.get("month") or "").strip()
        if given and given != wanted:
            raise InputError(f"bill line {n} is for {given}, not {wanted}")
        try:
            usd = float((row["usd"] or "").strip())
        except ValueError:
            raise InputError(f"bill line {n}: usd {row['usd']!r} is not a number") from None
        if not math.isfinite(usd) or usd < 0:
            raise InputError(f"bill line {n}: usd must be the cost before credits, not {usd}")
        totals[project, service] = totals.get((project, service), 0.0) + usd
    return tuple(BillLine(p, s, usd) for (p, s), usd in sorted(totals.items()))


def check_cell_project(project_id: str) -> str:
    if not project_id.startswith(CELL_PREFIX):
        raise ValueError(f"{project_id!r} is not a cell project ({CELL_PREFIX}<label>)")
    check_cell_label(project_id.removeprefix(CELL_PREFIX))
    return project_id


def _instant(value: str, where: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise InputError(f"{where}: {value!r} is not an ISO 8601 time") from None
    if moment.utcoffset() is None:
        raise InputError(f"{where}: {value!r} has no time zone")
    return moment.astimezone(UTC)


def read_cells(raw: str) -> tuple[CellMonth, ...]:
    """The declared cells CSV, with no usage: these cells have no events."""
    reader = csv.DictReader(io.StringIO(raw))
    missing = [c for c in CELLS_COLUMNS if c not in (reader.fieldnames or ())]
    if missing:
        raise InputError(f"the cells file has no column {', '.join(missing)}")
    created: dict[str, datetime] = {}
    resources: dict[str, list[FixedResource]] = {}
    allowed = {CELL_BASE, *(r.value for r in CellResource)}
    for n, row in enumerate(reader, start=2):
        where = f"cells line {n}"
        try:
            project = check_cell_project((row["project_id"] or "").strip())
        except ValueError as exc:
            raise InputError(f"{where}: {exc}") from None
        resource = (row["resource"] or "").strip()
        if resource not in allowed:
            raise InputError(f"{where}: resource {resource!r} is not one of {sorted(allowed)}")
        at = _instant((row["created_at"] or "").strip(), where)
        if resource == CELL_BASE:
            if project in created:
                raise InputError(f"{where}: {project} has two cell rows")
            created[project] = at
        else:
            resources.setdefault(project, []).append(FixedResource(resource, at))
    orphans = sorted(set(resources) - set(created))
    if orphans:
        raise InputError(f"no cell row for {', '.join(orphans)}")
    return tuple(
        CellMonth(p, created[p], tuple(sorted(resources.get(p, []), key=lambda r: r.created_at)))
        for p in sorted(created)
    )


def read_reasons(raw: str) -> dict[str, str]:
    try:
        found: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(f"the reasons file is not JSON: {exc}") from None
    if not isinstance(found, dict):
        raise InputError("the reasons file is one object from line id to reason")
    items = cast("dict[object, object]", found)
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in items.items()):
        raise InputError("the reasons file is one object from line id to reason")
    return {str(k): str(v) for k, v in items.items()}


async def gather_cell(conn: AsyncConnection, org_id: str, month: date) -> CellMonth | None:
    """One org's cell and its month of events; None for an org with no cell that month."""
    lo, hi = month_span(month)
    row = (await conn.execute(_ORG, {"org": org_id})).first()
    if row is None or row[1] is None or row[0] >= hi:
        return None
    params = {"org": org_id, "lo": lo, "hi": hi}
    apps: dict[str, dict[str, Any]] = {}
    for app, env, billing, instance, session in (await conn.execute(_SECONDS, params)).all():
        entry = apps.setdefault(str(app), {"envs": set(), "req": 0.0, "inst": 0.0, "sess": 0.0})
        entry["envs"].add(str(env))
        entry["inst" if billing == "instance" else "req"] += float(instance)
        entry["sess"] += float(session)
    cold = {str(a): int(n) for a, n in (await conn.execute(_COLD, params)).all()}
    for app in cold:
        apps.setdefault(app, {"envs": set(), "req": 0.0, "inst": 0.0, "sess": 0.0})
    gateway = float((await conn.execute(_GATEWAY, params)).scalar_one())
    fixed = tuple(r for r in await fixed_resources(conn, org_id) if r.created_at < hi)
    return CellMonth(
        project_id=CELL_PREFIX + str(row[1]),
        created_at=row[0].astimezone(UTC),
        resources=fixed,
        apps=tuple(
            AppMonth(
                app_id=app,
                environments=max(len(e["envs"]), 1),
                request_seconds=e["req"],
                instance_billed_seconds=e["inst"],
                session_seconds=e["sess"],
                cold_starts=cold.get(app, 0),
            )
            for app, e in sorted(apps.items())
        ),
        gateway_session_seconds=gateway,
        org_id=org_id,
    )


def _fraction(since: datetime, month: date) -> float:
    lo, hi = month_span(month)
    start = max(lo, since.astimezone(UTC))
    return max(0.0, (hi - start).total_seconds()) / (hi - lo).total_seconds()


def fixed_parts(model: CostModel, cell: CellMonth, month: date) -> tuple[PartShare, ...]:
    """The cell's fixed parts this month, each from when it was created, never before the cell."""
    since = {CELL_BASE: cell.created_at}
    for r in cell.resources:
        since.setdefault(r.resource, max(r.created_at, cell.created_at))
    return tuple(
        PartShare(
            p.name, p.label, p.usd, since[p.created_with], _fraction(since[p.created_with], month)
        )
        for p in model.cell_parts
        if p.created_with in since
    )


def _shape(cell: CellMonth) -> Literal["empty", "database", "full"]:
    have = {r.resource for r in cell.resources}
    if CellResource.EGRESS.value in have:
        return "full"
    return "database" if CellResource.DATABASE.value in have else "empty"


def attribute(cloud_run: float, priced: Mapping[str, float]) -> tuple[dict[str, float], float]:
    """Each app's share of the Cloud Run bill and what is left (see the module docstring)."""
    total = sum(priced.values())
    scale = 1.0 if total <= cloud_run else cloud_run / total
    shares = {k: v * scale for k, v in priced.items()}
    return shares, max(0.0, cloud_run - sum(shares.values()))


def _sum(values: Iterable[float]) -> float:
    return sum(values, 0.0)


def _money(
    model: CostModel, id_: str, label: str, bill: float | None, figure: float | None
) -> Line:
    return Line(id_, label, bill, figure, tolerance=model.tolerance, floor=model.min_usd)


def _total(bill: Mapping[str, float] | None) -> float | None:
    return None if bill is None else _sum(bill.values())


def _classes(model: CostModel, bill: Mapping[str, float] | None) -> tuple[Service, ...]:
    return tuple((s, model.service_class(s), usd) for s, usd in sorted((bill or {}).items()))


def _platform(
    model: CostModel, platform: Platform, bill: Mapping[str, float] | None
) -> tuple[ProjectRow, list[Line]]:
    pid = platform.project_id
    row = ProjectRow(
        pid, "platform", _money(model, f"project:{pid}", pid, _total(bill), platform.total)
    )
    expected = platform.by_service()
    lines = [
        _money(
            model,
            f"service:{pid}:{service}",
            f"{pid}, {service}",
            None if bill is None else bill.get(service, 0.0),
            expected.get(service),
        )
        for service in sorted(set(expected) | set(bill or {}))
    ]
    return row, lines


@dataclass(frozen=True, slots=True)
class _CellRows:
    project: ProjectRow
    fixed: FixedRow
    apps: list[AppRow]
    services: list[Line]


def _cell(
    model: CostModel, month: date, cell: CellMonth, bill: Mapping[str, float] | None
) -> _CellRows:
    pid = cell.project_id
    parts = fixed_parts(model, cell, month)
    fixed_model = _sum(p.usd for p in parts)
    gateway_model = cell.gateway_session_seconds * model.per_second("request")
    priced = {a.app_id: a.price(model) for a in cell.apps}
    billed_fixed = shares = rest = None
    if bill is not None:
        billed_fixed = _sum(v for s, v in bill.items() if model.service_class(s) == "fixed")
        cloud_run = _sum(v for s, v in bill.items() if model.service_class(s) == "apps")
        shares, rest = attribute(cloud_run, priced)
    figure = fixed_model + _sum(priced.values()) + gateway_model
    services = [
        _money(model, f"cloud_run_rest:{pid}", f"{pid}, {CLOUD_RUN_REST}", rest, gateway_model)
    ]
    services += [
        Line(f"other:{pid}:{service}", f"{pid}, {service}", usd, None, flags=False)
        for service, klass, usd in _classes(model, bill)
        if klass == "unattributed"
    ]
    return _CellRows(
        project=ProjectRow(
            pid,
            "cell",
            _money(model, f"project:{pid}", pid, _total(bill), figure),
            _classes(model, bill),
        ),
        fixed=FixedRow(
            pid,
            parts,
            _money(model, f"fixed:{pid}", f"{pid}, fixed", billed_fixed, fixed_model),
            _shape(cell),
        ),
        apps=[
            AppRow(pid, a, priced[a.app_id], None if shares is None else shares[a.app_id])
            for a in cell.apps
        ],
        services=services,
    )


def _types(model: CostModel, apps: Sequence[AppRow]) -> list[tuple[Line, int]]:
    out: list[tuple[Line, int]] = []
    for kind in TYPES:
        rows = [r.attributed_usd for r in apps if r.app.usage_type == kind]
        paid = [usd for usd in rows if usd is not None]
        low, high = model.band(kind)
        mean = _sum(paid) / len(paid) if paid else None
        line = Line(
            f"type:{kind}", f"{kind} app, mean", mean, low, high, model.tolerance, billed=False
        )
        out.append((line, len(paid)))
    return out


def _split(model: CostModel, apps: Sequence[AppRow]) -> list[tuple[Line, int]]:
    typed = [r.app.usage_type for r in apps if r.app.usage_type is not None]
    n = len(typed)
    measured = {
        "rare": typed.count("rare"),
        "daily": typed.count("daily") + typed.count("session"),
        "heavy": typed.count("heavy"),
    }
    small = None if is_reportable(n) else f"small sample (n={n}, need {NO_RATE_BELOW_N})"
    return [
        (
            Line(
                f"split:{k}",
                f"{k} share" + (" (with session apps)" if k == "daily" else ""),
                v / n if n else None,
                model.split[k],
                tolerance=model.tolerance,
                billed=False,
                note=small,
                unit="share",
            ),
            v,
        )
        for k, v in measured.items()
    ]


def reconcile(  # noqa: PLR0913  (keyword-only)
    month: date,
    bills: Sequence[BillLine],
    cells: Sequence[CellMonth],
    model: CostModel,
    *,
    reasons: Mapping[str, str] | None = None,
    bill_name: str = "",
    database: bool = True,
) -> Reconciliation:
    """The month's reconciliation; :class:`InputError` for a reason given to no line."""
    by_project: dict[str, dict[str, float]] = {}
    for b in bills:
        by_project.setdefault(b.project_id, {})[b.service] = b.usd
    known = {c.project_id: c for c in cells}
    projects: list[ProjectRow] = []
    fixed: list[FixedRow] = []
    apps: list[AppRow] = []
    services: list[Line] = []
    billed_total, billed_apps = 0.0, 0
    for pid, platform in model.platforms.items():
        row, lines = _platform(model, platform, by_project.get(pid))
        projects.append(row)
        services += lines
        if platform.in_target:
            billed_total += row.line.measured or 0.0
    for pid in sorted(known):
        rows = _cell(model, month, known[pid], by_project.get(pid))
        projects.append(rows.project)
        fixed.append(rows.fixed)
        apps += rows.apps
        services += rows.services
        if rows.project.line.measured is not None:
            billed_total += rows.project.line.measured
            billed_apps += sum(1 for r in rows.apps if r.app.usage_type is not None)
    for pid in sorted(set(by_project) - set(known) - set(model.platforms)):
        kind: Literal["cell, no record", "not in the model"] = (
            "cell, no record" if pid.startswith(CELL_PREFIX) else "not in the model"
        )
        bill = by_project[pid]
        line = _money(model, f"project:{pid}", pid, _total(bill), None)
        projects.append(ProjectRow(pid, kind, line, _classes(model, bill)))
    s = model.scenario
    per_app = Line(
        "per_app",
        "cost per app, all in",
        billed_total / billed_apps if billed_apps else None,
        model.target[0],
        model.target[1],
        model.tolerance,
        billed=False,
        note=(
            f"{billed_apps} apps with usage in billed cells, against a target set for "
            f"{s.apps} apps in {s.customers} cells; fewer apps carry more fixed cost each"
        ),
    )
    built = Reconciliation(
        month=month,
        model=model,
        bill_name=bill_name,
        projects=tuple(projects),
        fixed=tuple(fixed),
        apps=tuple(apps),
        services=tuple(services),
        types=tuple(_types(model, apps)),
        per_app=per_app,
        split=tuple(_split(model, apps)),
        checks=checks(model),
        database=database,
    )
    return _with_reasons(built, reasons or {})


def _with_reasons(found: Reconciliation, reasons: Mapping[str, str]) -> Reconciliation:
    ids = {line.id for line in found.lines()}
    unknown = sorted(set(reasons) - ids)
    if unknown:
        raise InputError(f"reasons for lines that are not on the page: {', '.join(unknown)}")
    if not reasons:
        return found

    def give(line: Line) -> Line:
        return replace(line, reason=reasons[line.id]) if line.id in reasons else line

    return replace(
        found,
        projects=tuple(replace(p, line=give(p.line)) for p in found.projects),
        fixed=tuple(replace(f, line=give(f.line)) for f in found.fixed),
        services=tuple(give(x) for x in found.services),
        types=tuple((give(t), k) for t, k in found.types),
        per_app=give(found.per_app),
        split=tuple((give(t), k) for t, k in found.split),
    )


def _usd(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _amount(line: Line, value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f} %" if line.unit == "share" else _usd(value)


def _model(line: Line) -> str:
    if line.low is None:
        return "—"
    if line.high is None or line.high == line.low:
        return _amount(line, line.low)
    return f"{_amount(line, line.low)}–{_amount(line, line.high)}"


def _difference(line: Line) -> str:
    off = line.off
    if line.measured is None or line.low is None or off is None:
        return "—"
    pct = round(off * 100, 1) or 0.0
    if line.high is not None and line.high != line.low:
        return "within" if off == 0 else f"{pct:+.1f} % outside"
    delta = round(line.measured - line.low, 3 if line.unit == "share" else 2) or 0.0
    sign = "+" if delta >= 0 else "-"
    if line.unit == "share":
        return f"{sign}{abs(delta) * 100:.1f} points ({pct:+.1f} %)"
    return f"{sign}${abs(delta):,.2f} ({pct:+.1f} %)"


_STATUS: Final[Mapping[Status, str]] = {
    "ok": "",
    "off": "**off**",
    "no_bill": "**no bill**",
    "no_model": "**no model**",
}


def _mark(line: Line) -> str:
    return _STATUS[line.status] if line.flagged else ""


def _row(*cells: object) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _name(key: str) -> str:
    return " ".join("NAT" if word == "nat" else word for word in key.replace("_", " ").split(" "))


def _since(parts: Sequence[PartShare]) -> str:
    groups: dict[datetime, list[str]] = {}
    for p in parts:
        groups.setdefault(p.since, []).append(_name(p.name))
    return "; ".join(
        f"{', '.join(names)} from {since:%m-%d %H:%M} ({_fraction_of(parts, since) * 100:.0f} %)"
        for since, names in sorted(groups.items())
    )


def _fraction_of(parts: Sequence[PartShare], since: datetime) -> float:
    return next(p.fraction for p in parts if p.since == since)


def render(found: Reconciliation) -> str:
    """The month's page as Markdown."""
    model = found.model
    tol = f"{model.tolerance * 100:.0f} %"
    billed = _sum(p.line.measured or 0.0 for p in found.projects)
    sources = (
        f"{len(found.fixed)} cells from the control database"
        if found.database
        else f"{len(found.fixed)} declared cells, no control database"
    )
    out = [
        f"# SSC cost reconciliation, {found.month:%Y-%m}",
        "",
        f"Bill `{found.bill_name}`: {_usd(billed)} before credits across "
        f"{sum(1 for p in found.projects if p.line.measured is not None)} projects. Events: "
        f"{sources}. Model `{model.format}`, {model.region} list prices. A line more than "
        f"{tol} off is flagged; a project line under {_usd(model.min_usd)} on both sides is not.",
        "",
        "## 1. Projects",
        "",
        _row("Project", "Kind", "Bill", "Model", "Difference", ""),
        "|---|---|---:|---:|---:|---|",
    ]
    out += [
        _row(
            p.project_id,
            p.kind,
            _usd(p.line.measured),
            _model(p.line),
            _difference(p.line),
            _mark(p.line),
        )
        for p in found.projects
    ]
    stated = model.cell_stated
    whole = (
        ("empty", model.cell_month(())),
        ("with a database", model.cell_month(("database",))),
        ("full", model.cell_month(("database", "egress", "connections"))),
    )
    out += [
        "",
        "## 2. Fixed cost per cell",
        "",
        _row("Cell", "Parts, from (share of the month)", "Model", "Billed fixed", "Difference", ""),
        "|---|---|---:|---:|---:|---|",
    ]
    out += [
        _row(
            f.project_id,
            _since(f.parts),
            _usd(f.line.low),
            _usd(f.line.measured),
            _difference(f.line),
            _mark(f.line),
        )
        for f in found.fixed
    ] or [_row("none", "", "", "", "", "")]
    out += [
        "",
        "Parts a month: "
        + ", ".join(f"{_name(p.name)} {_usd(p.usd)}" for p in model.cell_parts)
        + ". A whole month: "
        + ", ".join(
            f"{name} {_usd(v)} (stated {_usd(stated[key])})"
            for (name, v), key in zip(whole, ("empty", "database", "full"), strict=True)
        )
        + ".",
        "",
        "## 3. Usage per app",
        "",
        _row(
            "Cell",
            "App",
            "Type",
            "Envs",
            "Instance h",
            "Session h",
            "Cold starts",
            "Model",
            "Attributed",
        ),
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    order = {k: i for i, k in enumerate(TYPES)}
    rows = sorted(
        found.apps,
        key=lambda r: (order.get(r.app.usage_type or "", len(TYPES)), r.project_id, r.app.app_id),
    )
    out += [
        _row(
            r.project_id,
            r.app.app_id,
            r.app.usage_type or "no usage",
            r.app.environments,
            f"{r.app.instance_seconds / 3600:.2f}",
            f"{r.app.session_seconds / 3600:.2f}",
            r.app.cold_starts,
            _usd(r.model_usd),
            _usd(r.attributed_usd),
        )
        for r in rows
    ] or [_row("none", "", "", "", "", "", "", "", "")]
    out += [
        "",
        "Model: instance seconds at 1 vCPU and 512 MiB, request-billed at "
        f"${model.hourly('request'):.4f} an hour, instance-billed at "
        f"${model.hourly('instance'):.4f}. Attributed: the cell's Cloud Run bill shared in "
        "proportion to the model, never more than the model; the rest is a line below.",
        "",
        "## 4. Not attributed to apps, and platform services",
        "",
        _row("Line", "Bill", "Model", "Difference", ""),
        "|---|---:|---:|---:|---|",
    ]
    out += [
        _row(s.label, _usd(s.measured), _model(s), _difference(s), _mark(s)) for s in found.services
    ] or [_row("none", "", "", "", "")]
    out += [
        "",
        "The model's figure for Cloud Run, not apps, is the gateway: hours with a session open at "
        "the request rate. The agent, the data gateway, builds and logs have none.",
        "",
        "## 5. Cost per app",
        "",
        _row("Line", "Apps", "Measured", "Model", "Difference", ""),
        "|---|---:|---:|---:|---:|---|",
    ]
    out += [
        _row(t.label, n, _usd(t.measured), _model(t), _difference(t), _mark(t))
        for t, n in found.types
    ]
    p = found.per_app
    out += [
        _row(p.label, "", _usd(p.measured), _model(p), _difference(p), _mark(p)),
        "",
        f"Target: {_usd(model.target[0])} to {_usd(model.target[1])} per app. {p.note}.",
        "",
        "## 6. Usage split against 70/20/10",
        "",
        _row("Share", "Apps", "Measured", "Assumed", "Difference", ""),
        "|---|---:|---:|---:|---:|---|",
    ]
    out += [
        _row(s.label, n, _amount(s, s.measured), _model(s), _difference(s), _mark(s))
        for s, n in found.split
    ]
    note = found.split[0][0].note if found.split else None
    out += [
        "",
        "Assumed: the founder's 70/20/10 of 2026-10-02. Replace it in the model with the measured "
        "split once there is one." + (f" {note[0].upper()}{note[1:]}." if note else ""),
        "",
        "## 7. Model arithmetic",
        "",
    ]
    short = [c for c in found.checks if not model.adds_up(c)]
    out += [
        f"- {_name(c.label)}: stated {_figure(c.stated)}, "
        f"parts and rates give {_figure(c.computed)} "
        f"({'+' if c.difference >= 0 else '-'}{_figure(abs(c.difference))})"
        for c in short
    ] or ["Every stated figure adds up from its parts."]
    flags = found.flags
    out += ["", f"## 8. Flagged lines ({len(flags)})", ""]
    if flags:
        out += [_row("Line", "Measured", "Model", "Off", "Reason"), "|---|---:|---:|---|---|"]
        out += [
            _row(
                f"`{f.id}`",
                _amount(f, f.measured),
                _model(f),
                _off(f),
                f.reason or "_reason to be written_",
            )
            for f in flags
        ]
    else:
        out.append("None.")
    out += ["", "---", "", FOOTER, ""]
    return "\n".join(out)


def _figure(value: float) -> str:
    return f"${value:,.4f}" if value < 1 else f"${value:,.2f}"


def _off(line: Line) -> str:
    if line.status == "no_bill":
        return "no bill"
    if line.status == "no_model":
        return "no model"
    return f"{(line.off or 0.0) * 100:+.1f} %"


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _line_json(line: Line) -> dict[str, Any]:
    return {
        "id": line.id,
        "label": line.label,
        "unit": line.unit,
        "measured": _r(line.measured),
        "model_low": _r(line.low),
        "model_high": _r(line.top),
        "off": _r(line.off),
        "status": line.status,
        "flagged": line.flagged,
        "reason": line.reason,
        "note": line.note,
    }


def as_json(found: Reconciliation) -> dict[str, Any]:
    """The same page as data."""
    model = found.model
    return {
        "format": FORMAT,
        "month": found.month.strftime("%Y-%m"),
        "model": {
            "format": model.format,
            "tolerance": model.tolerance,
            "min_usd": model.min_usd,
            "target": list(model.target),
            "split": dict(model.split),
        },
        "inputs": {"bill": found.bill_name, "database": found.database},
        "projects": [
            {
                "project_id": p.project_id,
                "kind": p.kind,
                **_line_json(p.line),
                "services": [
                    {"service": s, "class": c, "usd": _r(usd)} for s, c, usd in p.services
                ],
            }
            for p in found.projects
        ],
        "fixed": [
            {
                "project_id": f.project_id,
                "shape": f.shape,
                **_line_json(f.line),
                "parts": [
                    {
                        "name": part.name,
                        "month_usd": part.month_usd,
                        "since": part.since.isoformat(),
                        "fraction": _r(part.fraction),
                        "usd": _r(part.usd),
                    }
                    for part in f.parts
                ],
            }
            for f in found.fixed
        ],
        "apps": [
            {
                "project_id": r.project_id,
                "app_id": r.app.app_id,
                "usage_type": r.app.usage_type,
                "environments": r.app.environments,
                "instance_hours": round(r.app.instance_seconds / 3600, 2),
                "session_hours": round(r.app.session_seconds / 3600, 2),
                "cold_starts": r.app.cold_starts,
                "request_seconds": r.app.request_seconds,
                "instance_billed_seconds": r.app.instance_billed_seconds,
                "model_usd": _r(r.model_usd),
                "attributed_usd": _r(r.attributed_usd),
            }
            for r in found.apps
        ],
        "services": [_line_json(s) for s in found.services],
        "types": [{**_line_json(t), "apps": n} for t, n in found.types],
        "per_app": _line_json(found.per_app),
        "split": [{**_line_json(s), "apps": n} for s, n in found.split],
        "checks": [
            {
                "id": c.id,
                "label": c.label,
                "stated": c.stated,
                "computed": _r(c.computed),
                "difference": _r(c.difference),
                "adds_up": model.adds_up(c),
            }
            for c in found.checks
        ],
        "flags": [_line_json(f) for f in found.flags],
        "footer": FOOTER,
    }


async def gather(dsn: str, month: date, org_ids: Sequence[str]) -> tuple[CellMonth, ...]:
    """Every org's cell (or only ``org_ids``'), each read in its own read-only transaction."""
    engine = make_engine(dsn)
    try:
        snapshot = engine.execution_options(
            isolation_level="REPEATABLE READ", postgresql_readonly=True
        )
        ids = list(org_ids) or await all_org_ids(engine)
        out: list[CellMonth] = []
        for org_id in ids:
            async with bound_org(snapshot, org_id) as conn:
                cell = await gather_cell(conn, org_id, month)
            if cell is not None:
                out.append(cell)
            elif org_ids:
                raise InputError(f"org {org_id} has no cell in {month:%Y-%m}")
    finally:
        await engine.dispose()
    return tuple(out)


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read {path.name}: {exc.strerror}") from None


def _month(value: str) -> date:
    try:
        return parse_month(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ssc_control.metrics.reconcile")
    parser.add_argument("--month", required=True, type=_month, help="YYYY-MM (UTC)")
    parser.add_argument(
        "--bill", required=True, type=Path, help="project_id,service,usd CSV or JSON"
    )
    parser.add_argument("--cells", type=Path, help="cells with no control database, CSV")
    parser.add_argument("--org", action="append", default=[], type=check_org_id)
    parser.add_argument("--reasons", type=Path, help="JSON: line id to written reason")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", type=Path, help="write ssc-cost-YYYY-MM.md and .json here")
    args = parser.parse_args(argv)
    dsn = os.environ.get(DSN_ENV)
    if not dsn and args.cells is None:
        parser.error(f"{DSN_ENV} is not set and no --cells was given")
    if args.org and not dsn:
        parser.error(f"--org reads the control database; {DSN_ENV} is not set")
    month: date = args.month
    try:
        bills = read_bill(_text(args.bill), month)
        declared = read_cells(_text(args.cells)) if args.cells else ()
        reasons = read_reasons(_text(args.reasons)) if args.reasons else {}
        stored = asyncio.run(gather(dsn, month, args.org)) if dsn else ()
        twice = sorted({c.project_id for c in declared} & {c.project_id for c in stored})
        if twice:
            raise InputError(f"declared and in the control database: {', '.join(twice)}")
        found = reconcile(
            month,
            bills,
            (*stored, *declared),
            load(),
            reasons=reasons,
            bill_name=args.bill.name,
            database=bool(dsn),
        )
    except InputError as exc:
        parser.error(str(exc))
    page, data = render(found), json.dumps(as_json(found), indent=2, allow_nan=False) + "\n"
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        stem = args.out / f"ssc-cost-{month:%Y-%m}"
        stem.with_suffix(".md").write_text(page, encoding="utf-8")
        stem.with_suffix(".json").write_text(data, encoding="utf-8")
        sys.stdout.write(f"{stem.with_suffix('.md')}\n{stem.with_suffix('.json')}\n")
    else:
        sys.stdout.write(data if args.json else page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
