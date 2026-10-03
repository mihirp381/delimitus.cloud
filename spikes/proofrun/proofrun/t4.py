"""T4: ``cannot_reach_peer_cell`` from cell 1 against cell 2.

``t4 --project <cell 1> --agent-url <cell 1 agent> --digest <sha256:...>
--peer-project-number <cell 2 number> [--peer-range 10.20.0.0/24]``

Runs the existing nightly against cell 1 with the peer settings pointing at cell 2's probe app
``a`` (its ``run.app`` host), cell 2's gateway (its ``run.app`` host) and cell 2's apps range.
Cell 2's probe apps exist once the nightly has run against cell 2 (``t3 nightly`` there first).
Each attempt is sorted from the probe's reason:

- ``network``: no connection (refused, timed out, nothing resolved);
- ``ingress``: Cloud Run's ingress refused with 404 before any IAM check, with none of the peer's
  own markers;
- ``iam``: 401 or 403, which means the network let the call through to IAM;
- ``peer``: the peer app or gateway answered itself; ``answered``: any other answer.

Pass: the probe passed and every app and gateway attempt is ``network`` or ``ingress``. Every
cell uses the same address plan, so the range leg dials cell 1's own range; the probe says so and
leaves it out (separate networks with no peering stop it, shown by T11's refused peering).
"""

import argparse
import os
from collections import Counter
from collections.abc import Mapping, Sequence

from proofrun.common import GATEWAY, Outcome, Run, app_service, run_command, run_url
from proofrun.probes import PEER_CELL_PROBE, REFUSALS, peer_legs, range_not_applicable
from proofrun.t3 import add_nightly_arguments, nightly_env, run_nightly

PROBE_ENV_A = "env_probe00000000000000a"
APPS_RANGE = "10.20.0.0/24"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    add_nightly_arguments(parser)
    parser.add_argument("--peer-project-number", required=True, help="cell 2's project number")
    parser.add_argument("--peer-range", default=APPS_RANGE, help="cell 2's apps subnet")


def peer_env(number: str, cidr: str) -> dict[str, str]:
    """The nightly's three peer settings for cell 2."""
    return {
        "SSC_PROBE_PEER_APP_URL": run_url(app_service(PROBE_ENV_A), number),
        "SSC_PROBE_PEER_GATEWAY_URL": run_url(GATEWAY, number),
        "SSC_PROBE_PEER_RANGE": cidr,
    }


def verdict(rows: Sequence[Mapping[str, str]]) -> Outcome:
    row = next((r for r in rows if r["probe"] == PEER_CELL_PROBE), None)
    if row is None:
        return Outcome("T4", f"{PEER_CELL_PROBE} did not report", None, [])
    legs = peer_legs(row["reason"])
    counted = [leg for leg in legs if leg.target != "range"]
    by_target = {
        t: Counter(leg.kind for leg in counted if leg.target == t) for t in ("app", "gateway")
    }
    lines = [f"{leg.name}: {leg.kind}: {leg.detail}" for leg in legs]
    if range_not_applicable(row["reason"]):
        lines.append("range leg: not applicable, the peer range holds cell 1's own address")
    lines.append(f"probe status: {row['status']}")
    number = "; ".join(
        f"{t}: " + (", ".join(f"{n} {k}" for k, n in sorted(c.items())) or "no attempts")
        for t, c in by_target.items()
    )
    passed = (
        row["status"] == "passed"
        and bool(counted)
        and all(by_target[t] for t in by_target)
        and all(leg.kind in REFUSALS for leg in counted)
    )
    data = {
        "legs": [{"name": leg.name, "kind": leg.kind} for leg in legs],
        "status": row["status"],
    }
    return Outcome("T4", number, passed, lines, data)


def run(args: argparse.Namespace, run: Run = run_command) -> Outcome:
    env = {**nightly_env(args, os.environ), **peer_env(args.peer_project_number, args.peer_range)}
    rows, _drift, error = run_nightly(run, env)
    if not rows:
        return Outcome("T4", "the nightly printed no results", None, [error])
    return verdict(rows)
