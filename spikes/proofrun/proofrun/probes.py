"""The runtime probes, reused from ``conformance/runtime/probe_app`` rather than copied.

- :func:`load_runner` imports the probe app's ``runner`` (and its ``checks``) from the repository.
- :func:`public_probe` calls the probe app the way a signed-in person's client does: through the
  cell's public host and gateway, with the session cookie and the app's own ``Authorization``.
- :func:`nightly_rows` reads the table ``python -m ssc_conformance.nightly`` prints.
- :func:`peer_legs` sorts each attempt of ``cannot_reach_peer_cell`` into a network refusal,
  Cloud Run's ingress refusal, an IAM refusal, or an answer.
"""

import importlib
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from proofrun.common import REPO, Cookie, session_headers

PROBE_APP: Final = REPO / "conformance" / "runtime" / "probe_app"
PEER_CELL_PROBE: Final = "cannot_reach_peer_cell"
LEG_NAMES: Final = tuple(
    sorted(
        (
            f"{target} {how}"
            for target in ("app", "gateway")
            for how in ("by name", "by name with ID token", "by Google VIP with ID token")
        ),
        key=len,
        reverse=True,
    )
)
_LEG: Final = re.compile("(" + "|".join(re.escape(n) for n in LEG_NAMES) + r"|tcp [0-9.]+:\d+): ")
_HTTP: Final = re.compile(r"HTTP (\d{3})")
IAM_STATUSES: Final = frozenset({401, 403})
INGRESS_STATUS: Final = 404
REFUSALS: Final = frozenset({"network", "ingress"})


def load_runner(probe_app: Path = PROBE_APP) -> ModuleType:
    """The probe app's ``runner`` module; its ``import checks`` resolves beside it."""
    if not (probe_app / "runner.py").exists():
        raise FileNotFoundError(f"no probe runner at {probe_app}")
    if str(probe_app) not in sys.path:
        sys.path.insert(0, str(probe_app))
    return importlib.import_module("runner")


def public_probe(runner: ModuleType, base_url: str, cookie: Cookie) -> Any:
    """A ``runner.Probe`` that goes through the public host: the session cookie gets it past the
    gateway, which adds the ID token for the app's ``run.app`` URL itself."""
    probe = runner.Probe(base_url, "")
    probe.headers = {**session_headers(cookie), "Authorization": runner.checks.APP_CREDENTIAL}
    return probe


def probe_counts(results: Sequence[Mapping[str, str]]) -> tuple[int, int, list[str]]:
    """Passed and total among the probes that must pass without a peer cell (the ticket's 14),
    and what failed or was skipped among them."""
    counted = [r for r in results if r["probe"] != PEER_CELL_PROBE]
    bad = [
        f"{r['probe']}: {r['status']}: {r['reason']}" for r in counted if r["status"] != "passed"
    ]
    return len(counted) - len(bad), len(counted), bad


def nightly_rows(markdown: str) -> list[dict[str, str]]:
    """The probe rows of the nightly's printed table."""
    rows: list[dict[str, str]] = []
    for line in markdown.splitlines():
        if not line.startswith("| ") or line.startswith(("| probe ", "| --- ")):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ", 2)]
        if len(cells) == 3:
            rows.append({"probe": cells[0], "status": cells[1], "reason": cells[2]})
    return rows


def nightly_drift(markdown: str) -> str | None:
    m = re.search(r"Drift repaired in: (.+)", markdown)
    return m.group(1).strip() if m else None


@dataclass(frozen=True, slots=True)
class Leg:
    """One attempt of ``cannot_reach_peer_cell``: ``kind`` is ``network`` (no connection),
    ``ingress`` (Cloud Run refused before IAM), ``iam`` (401 or 403: the network let it through),
    ``peer`` (the peer app or gateway answered) or ``answered`` (any other status)."""

    name: str
    kind: str
    detail: str

    @property
    def target(self) -> str:
        return "range" if self.name.startswith("tcp ") else self.name.split(" ", 1)[0]


def _kind(detail: str) -> str:
    if "from the peer itself" in detail:
        return "peer"
    m = _HTTP.match(detail)
    if m is None:
        return "answered" if "'blocked': False" in detail else "network"
    status = int(m.group(1))
    if status == INGRESS_STATUS:
        return "ingress"
    return "iam" if status in IAM_STATUSES else "answered"


def peer_legs(reason: str) -> list[Leg]:
    """Each attempt named in the probe's reason, passed or failed, with its kind."""
    reason = reason.removeprefix("the peer cell let calls through: ")
    matches = list(_LEG.finditer(reason))
    legs: list[Leg] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(reason)
        detail = reason[m.end() : end].strip().rstrip(";,").strip()
        detail = detail.split("; range leg not applicable")[0].strip()
        legs.append(Leg(m.group(1), _kind(detail), detail))
    return legs


def range_not_applicable(reason: str) -> bool:
    return "range leg not applicable" in reason
