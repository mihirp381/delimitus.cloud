"""Cloud Run's us-central1 (tier 1) list prices and the model's hourly figures.

Request-billed: $0.000024 a vCPU-second and $0.0000025 a GiB-second while serving.
Instance-billed: $0.000018 and $0.000002 for the instance's whole life. At 1 vCPU and 512 MiB
that is $0.0909 and $0.0684 an hour, the two figures T9 checks the bill against. Requests
($0.40 a million, request-billed only) are left out: a held WebSocket is one request.
"""

from typing import Final

RATES: Final = {
    "request": {"vcpu": 0.000024, "gib": 0.0000025},
    "instance": {"vcpu": 0.000018, "gib": 0.000002},
}
TOLERANCE: Final = 0.20


def hourly(mode: str, vcpu: float = 1.0, gib: float = 0.5) -> float:
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
