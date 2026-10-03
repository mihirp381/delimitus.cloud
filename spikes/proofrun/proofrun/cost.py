"""Cloud Run's us-central1 (tier 1) list prices and the model's figures, read from the cost model
the product keeps (``packages/ssc_control/src/ssc_control/metrics/cost_model.toml``, SSC-096).

The kit reads that file as data, by its path in the repository, and never imports product code;
product code never imports the kit. T1, T9 and T10 therefore check against the numbers the
monthly reconciliation uses, and a change to the model changes both.

Request-billed: $0.000024 a vCPU-second and $0.0000025 a GiB-second while serving.
Instance-billed: $0.000018 and $0.000002 for the instance's whole life. At 1 vCPU and 512 MiB
that is $0.0909 and $0.0684 an hour, the two figures T9 checks the bill against. Requests
($0.40 a million, request-billed only) are left out: a held WebSocket is one request.
"""

import tomllib
from pathlib import Path
from typing import Any, Final

from proofrun.common import REPO

MODEL_FILE: Final = REPO / "packages/ssc_control/src/ssc_control/metrics/cost_model.toml"


def read_model(path: Path = MODEL_FILE) -> dict[str, Any]:
    """The cost model's TOML as plain data."""
    return tomllib.loads(path.read_text(encoding="utf-8"))


MODEL: Final = read_model()
RATES: Final = {
    mode: {
        "vcpu": float(MODEL["cloud_run"][mode]["vcpu_second"]),
        "gib": float(MODEL["cloud_run"][mode]["gib_second"]),
    }
    for mode in ("request", "instance")
}
TOLERANCE: Final = float(MODEL["tolerance"])
REFERENCE_VCPU: Final = float(MODEL["cloud_run"]["reference"]["vcpu"])
REFERENCE_GIB: Final = float(MODEL["cloud_run"]["reference"]["gib"])
EMPTY_CELL_MONTH_USD: Final = float(MODEL["cell"]["stated"]["empty"])


def hourly(mode: str, vcpu: float = REFERENCE_VCPU, gib: float = REFERENCE_GIB) -> float:
    """Dollars an hour for one instance of ``vcpu`` and ``gib`` billed by ``mode``."""
    rate = RATES[mode]
    return (vcpu * rate["vcpu"] + gib * rate["gib"]) * 3600


def usage_cost(mode: str, vcpu_seconds: float, gib_seconds: float) -> float:
    """Dollars for the usage amounts on a bill, before the free tier takes its share."""
    rate = RATES[mode]
    return vcpu_seconds * rate["vcpu"] + gib_seconds * rate["gib"]


def within(measured: float, model: float, tolerance: float = TOLERANCE) -> bool:
    """Whether ``measured`` is within ``tolerance`` of ``model``."""
    return model > 0 and abs(measured - model) <= tolerance * model
