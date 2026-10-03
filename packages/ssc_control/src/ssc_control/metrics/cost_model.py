"""The cost model as data (architecture section 10, SSC-096): ``cost_model.toml`` beside this file.

That file is the one copy of the figures. Every figure in it carries where it comes from. The
monthly reconciliation (``metrics.reconcile``) reads it through :func:`load`, and the proof-run
kit (``spikes/proofrun/proofrun/cost.py``) reads the same file by its path in the repository, so
the kit never imports product code and product code never imports the kit.

Nothing here bends a stated figure to fit its parts. :func:`checks` recomputes each stated sum
from its parts and rates, and the reconciliation prints every one that does not add up.
"""

import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Final, Literal

FORMAT: Final = "ssc-cost-model/v1"
FILE_NAME: Final = "cost_model.toml"
SECONDS_PER_HOUR: Final = 3600

Mode = Literal["request", "instance"]
ServiceClass = Literal["apps", "fixed", "unattributed"]
UsageKind = Literal["rare", "daily", "session", "heavy"]
SERVICE_CLASSES: Final[frozenset[str]] = frozenset({"apps", "fixed", "unattributed"})
CELL_BASE: Final = "cell"
"""``created_with`` of the parts every cell has from onboarding."""


class CostModelError(ValueError):
    """The model file is not one this code can read; the message says what is wrong."""


@dataclass(frozen=True, slots=True)
class Rate:
    vcpu_second: float
    gib_second: float
    source: str

    def per_second(self, vcpu: float, gib: float) -> float:
        """Dollars a second for one instance of ``vcpu`` and ``gib``."""
        return vcpu * self.vcpu_second + gib * self.gib_second


@dataclass(frozen=True, slots=True)
class Part:
    name: str
    usd: float
    source: str
    label: str = ""
    created_with: str = CELL_BASE
    """For a cell part: ``cell`` (onboarding) or the ``fixed_resource`` that brings it."""
    service: str | None = None
    """For a platform part: the billing service its cost appears under."""


@dataclass(frozen=True, slots=True)
class Platform:
    project_id: str
    stated: float
    in_target: bool
    parts: tuple[Part, ...]
    source: str

    @property
    def total(self) -> float:
        return sum(p.usd for p in self.parts)

    def by_service(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for part in self.parts:
            key = part.service or ""
            out[key] = out.get(key, 0.0) + part.usd
        return out


@dataclass(frozen=True, slots=True)
class Scenario:
    """The ten-customer table of architecture section 10, as its inputs and its stated lines."""

    customers: int
    apps: int
    counts: Mapping[UsageKind, int]
    session_hours: tuple[float, float]
    heavy_busy: tuple[float, float]
    heavy_gib: float
    rare_seconds: tuple[float, float]
    daily_usd: float
    cells_without_proxy: tuple[int, int]
    stated: Mapping[str, tuple[float, float]]
    source: str


@dataclass(frozen=True, slots=True)
class Check:
    """A stated figure against the same figure recomputed from the model's parts and rates."""

    id: str
    label: str
    stated: float
    computed: float

    @property
    def difference(self) -> float:
        return self.computed - self.stated


@dataclass(frozen=True, slots=True)
class CostModel:
    format: str
    region: str
    month_hours: float
    tolerance: float
    min_usd: float
    adds_up_within: float
    adds_up_usd: float
    rates: Mapping[str, Rate]
    reference_vcpu: float
    reference_gib: float
    stated_hourly: Mapping[Mode, float]
    cell_parts: tuple[Part, ...]
    cell_stated: Mapping[str, float]
    platforms: Mapping[str, Platform]
    services: Mapping[str, ServiceClass]
    target: tuple[float, float]
    split: Mapping[str, float]
    scenario: Scenario
    sources: Mapping[str, str]

    def per_second(self, mode: Mode) -> float:
        """Dollars a second for one reference instance billed by ``mode``."""
        return self.rates[mode].per_second(self.reference_vcpu, self.reference_gib)

    def hourly(self, mode: Mode) -> float:
        return self.per_second(mode) * SECONDS_PER_HOUR

    def service_class(self, service: str) -> ServiceClass:
        """How a billing service is read; a service the model does not list is not attributed."""
        return self.services.get(service, "unattributed")

    def cell_month(self, resources: Iterable[str]) -> float:
        """A whole month of a cell with ``resources`` (``database``, ``egress``, ...)."""
        have = {CELL_BASE, *resources}
        return sum(p.usd for p in self.cell_parts if p.created_with in have)

    def part_for(self, resource: str) -> Part | None:
        return next((p for p in self.cell_parts if p.created_with == resource), None)

    def band(self, kind: UsageKind) -> tuple[float, float]:
        """The model's dollars per app of ``kind`` a month: the scenario's stated line over its
        count of apps, low and high."""
        low, high = self.scenario.stated[kind]
        n = self.scenario.counts[kind]
        return low / n, high / n

    def adds_up(self, check: Check) -> bool:
        return abs(check.difference) <= max(self.adds_up_usd, self.adds_up_within * check.stated)


def _rate(raw: Mapping[str, Any]) -> Rate:
    return Rate(float(raw["vcpu_second"]), float(raw["gib_second"]), str(raw["source"]))


def _pair(raw: Any) -> tuple[float, float]:
    low, high = raw
    return float(low), float(high)


def parse(raw: Mapping[str, Any]) -> CostModel:
    """A :class:`CostModel` from the file's TOML; :class:`CostModelError` when it is not one."""
    if raw.get("format") != FORMAT:
        raise CostModelError(f"not {FORMAT}: {raw.get('format')!r}")
    try:
        run = raw["cloud_run"]
        ref = run["reference"]
        cell = raw["cell"]
        scen = raw["scenario"]
        services: dict[str, ServiceClass] = {}
        for name, klass in raw["services"].items():
            if klass not in SERVICE_CLASSES:
                raise CostModelError(f"service {name!r} has class {klass!r}")
            services[str(name)] = klass
        platforms = {
            str(pid): Platform(
                project_id=str(pid),
                stated=float(p["stated"]),
                in_target=bool(p["in_target"]),
                parts=tuple(
                    Part(str(q["name"]), float(q["usd"]), str(q["source"]), service=q["service"])
                    for q in p["parts"]
                ),
                source=str(p["source"]),
            )
            for pid, p in raw["platform"].items()
        }
        return CostModel(
            format=FORMAT,
            region=str(raw["region"]),
            month_hours=float(raw["month_hours"]),
            tolerance=float(raw["tolerance"]),
            min_usd=float(raw["min_usd"]),
            adds_up_within=float(raw["adds_up_within"]),
            adds_up_usd=float(raw["adds_up_usd"]),
            rates={k: _rate(run[k]) for k in ("request", "instance", "worker_pool")},
            reference_vcpu=float(ref["vcpu"]),
            reference_gib=float(ref["gib"]),
            stated_hourly={
                "request": float(ref["request_hourly"]),
                "instance": float(ref["instance_hourly"]),
            },
            cell_parts=tuple(
                Part(
                    str(name),
                    float(p["usd"]),
                    str(p["source"]),
                    label=str(p["label"]),
                    created_with=str(p["created_with"]),
                )
                for name, p in cell["parts"].items()
            ),
            cell_stated={k: float(cell["stated"][k]) for k in ("empty", "database", "full")},
            platforms=platforms,
            services=services,
            target=(float(raw["target"]["per_app_low"]), float(raw["target"]["per_app_high"])),
            split={k: float(raw["split"][k]) for k in ("rare", "daily", "heavy")},
            scenario=Scenario(
                customers=int(scen["customers"]),
                apps=int(scen["apps"]),
                counts={
                    "rare": int(scen["rare_apps"]),
                    "daily": int(scen["daily_apps"]),
                    "session": int(scen["session_apps"]),
                    "heavy": int(scen["heavy_apps"]),
                },
                session_hours=_pair(scen["session_hours"]),
                heavy_busy=_pair(scen["heavy_busy"]),
                heavy_gib=float(scen["heavy_gib"]),
                rare_seconds=_pair(scen["rare_seconds"]),
                daily_usd=float(scen["daily_usd"]),
                cells_without_proxy=(
                    int(scen["cells_without_proxy"][0]),
                    int(scen["cells_without_proxy"][1]),
                ),
                stated={str(k): _pair(v) for k, v in scen["stated"].items()},
                source=str(scen["source"]),
            ),
            sources={
                "model": str(raw["source"]),
                "request": str(run["request"]["source"]),
                "instance": str(run["instance"]["source"]),
                "reference": str(ref["source"]),
                "cell_stated": str(cell["stated"]["source"]),
                "target": str(raw["target"]["source"]),
                "split": str(raw["split"]["source"]),
                "scenario": str(scen["source"]),
            },
        )
    except CostModelError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CostModelError(f"the cost model is missing or mistypes {exc}") from exc


def read(path: Path) -> CostModel:
    return parse(tomllib.loads(path.read_text(encoding="utf-8")))


@cache
def load() -> CostModel:
    """The packaged model, read once per process."""
    return parse(tomllib.loads(files(__package__).joinpath(FILE_NAME).read_text("utf-8")))


def _cells_fixed(model: CostModel, without_proxy: int) -> float:
    stated = model.cell_stated
    return (model.scenario.customers - without_proxy) * stated["full"] + without_proxy * stated[
        "database"
    ]


def checks(model: CostModel) -> tuple[Check, ...]:
    """Every stated sum of the model recomputed from its parts and rates, in page order."""
    s = model.scenario
    req = model.rates["request"]
    month_seconds = model.month_hours * SECONDS_PER_HOUR
    out = [
        Check("cell_empty", "empty cell", model.cell_stated["empty"], model.cell_month(())),
        Check(
            "cell_database",
            "cell with a database",
            model.cell_stated["database"],
            model.cell_month(("database",)),
        ),
        Check(
            "cell_full",
            "full cell",
            model.cell_stated["full"],
            model.cell_month(("database", "egress", "connections")),
        ),
        Check(
            "request_hourly",
            "request-billed hour",
            model.stated_hourly["request"],
            model.hourly("request"),
        ),
        Check(
            "instance_hourly",
            "instance-billed hour",
            model.stated_hourly["instance"],
            model.hourly("instance"),
        ),
    ]
    out += [Check(f"platform_{pid}", pid, p.stated, p.total) for pid, p in model.platforms.items()]
    prod = next((p.total for p in model.platforms.values() if p.in_target), 0.0)
    for i, end in enumerate(("low", "high")):
        computed = {
            "platform_prod": prod,
            "cells_fixed": _cells_fixed(model, s.cells_without_proxy[i]),
            "gateway": s.customers * s.session_hours[i] * model.hourly("request"),
            "rare": s.counts["rare"] * model.per_second("request") * s.rare_seconds[i],
            "session": s.counts["session"] * s.session_hours[i] * model.hourly("instance"),
            "daily": s.counts["daily"] * s.daily_usd,
            "heavy": s.counts["heavy"]
            * req.per_second(1.0, s.heavy_gib)
            * s.heavy_busy[i]
            * month_seconds,
        }
        out += [
            Check(f"scenario_{line}_{end}", f"ten customers, {line} ({end})", s.stated[line][i], v)
            for line, v in computed.items()
        ]
        lines = sum(s.stated[line][i] for line in computed)
        out.append(
            Check(
                f"scenario_total_{end}",
                f"ten customers, total of the lines ({end})",
                s.stated["total"][i],
                lines,
            )
        )
        out.append(
            Check(
                f"scenario_per_app_{end}",
                f"ten customers, per app ({end})",
                s.stated["per_app"][i],
                s.stated["total"][i] / s.apps,
            )
        )
    return tuple(out)
