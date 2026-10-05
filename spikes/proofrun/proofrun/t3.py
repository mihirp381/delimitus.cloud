"""T3: the runtime probes through the new path, the gateway at minimum 0.

- ``t3 public --project <p> --project-number <n> --app <probe a> --peer-app <probe b>`` runs the
  conformance runner's probes against probe app ``a`` through its public host, as a signed-in
  person's client would: load balancer, gateway, then the app's ``run.app`` URL with the
  gateway's ID token. ``cannot_reach_peer_app`` targets probe app ``b``'s ``run.app`` URL.
  ``cannot_reach_peer_cell`` is T4's and is skipped here. Every answer from the app proves Cloud
  Run accepted the gateway's ID token for the app's ``run.app`` URL (the check SSC-018 left open).
- ``t3 nightly --project <p> --agent-url <url> --digest <sha256:...>`` runs the existing nightly
  (``python -m ssc_conformance.nightly``) against the cell and reads its table: the same probes
  from where the gateway stands, plus the drift repair.

Pass: 14 of 14 passed, and the gateway's minimum (template and service) is 0.
"""

import argparse
import os
from collections.abc import Mapping, Sequence
from typing import Any

from proofrun import cloudrun
from proofrun.common import (
    GATEWAY,
    REPO,
    CookieJar,
    Outcome,
    Run,
    app_environment,
    app_service,
    host_of,
    last_line,
    run_command,
    run_url,
)
from proofrun.probes import (
    load_runner,
    nightly_drift,
    nightly_rows,
    probe_counts,
    public_probe,
)

NIGHTLY_ENV = {
    "project": "SSC_PROBE_PROJECT",
    "agent_url": "SSC_PROBE_AGENT_URL",
    "digest": "SSC_PROBE_DIGEST",
}
PEER_ENV = ("SSC_PROBE_PEER_APP_URL", "SSC_PROBE_PEER_GATEWAY_URL", "SSC_PROBE_PEER_RANGE")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="step", required=True)
    public = sub.add_parser("public", help="the probes through the public host")
    add_public_arguments(public)
    nightly = sub.add_parser("nightly", help="the existing nightly against the cell")
    add_nightly_arguments(nightly)


def add_public_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True, help="the cell's project id")
    parser.add_argument("--project-number", required=True, help="the cell's project number")
    parser.add_argument("--app", required=True, help="probe app a's slug")
    parser.add_argument("--peer-app", required=True, help="probe app b's slug")
    parser.add_argument("--env", default="preview")
    parser.add_argument("--egress-host", action="append", default=[], help="also dial this host")


def add_nightly_arguments(parser: argparse.ArgumentParser) -> None:
    for field, env in NIGHTLY_ENV.items():
        flag = "--" + field.replace("_", "-")
        parser.add_argument(flag, default=os.environ.get(env), help=f"default: ${env}")


def gateway_min(run: Run, project: str) -> tuple[bool, str]:
    s = cloudrun.settings(cloudrun.describe(run, project, GATEWAY))
    return s.min_instances == 0 and s.service_min_instances == 0, s.describe()


def public_results(
    args: argparse.Namespace, run: Run, runner: Any = None
) -> list[Mapping[str, str]]:
    """The probe run through the public host."""
    runner = runner or load_runner()
    app = app_environment(run, args.app, args.env)
    peer = app_environment(run, args.peer_app, args.env)
    cookie = CookieJar().get(host_of(app["url"]))
    peer_url = run_url(app_service(peer["id"]), args.project_number)
    probe = public_probe(runner, app["url"], cookie)
    return runner.run(probe, peer_url, "/health", None, tuple(args.egress_host))


def verdict(
    results: Sequence[Mapping[str, str]], min_zero: bool, gateway: str, path: str
) -> Outcome:
    passed, total, bad = probe_counts(results)
    lines = [f"{r['probe']}: {r['status']}: {r['reason']}" for r in results]
    lines.append(f"gateway: {gateway}")
    audience = {r["probe"]: r["status"] for r in results}.get("health_path") == "passed"
    lines.append(
        "run.app audience accepted: "
        + ("yes, the app answered the gateway's ID token" if audience else "not shown")
    )
    return Outcome(
        "T3",
        f"{passed}/{total} probes passed {path}, gateway min {'0' if min_zero else 'not 0'}",
        passed == total == 14 and min_zero and not bad,
        lines,
        {"passed": passed, "total": total, "failed": bad, "path": path},
    )


def nightly_env(args: argparse.Namespace, environ: Mapping[str, str]) -> dict[str, str]:
    """The nightly's settings, with no peer cell (that is T4)."""
    env = {k: v for k, v in environ.items() if k not in PEER_ENV}
    for field, name in NIGHTLY_ENV.items():
        value = getattr(args, field)
        if not value:
            raise SystemExit(f"--{field.replace('_', '-')} or ${name} is required")
        env[name] = value
    return env


def run_nightly(run: Run, env: Mapping[str, str]) -> tuple[list[dict[str, str]], str | None, str]:
    done = run(["uv", "run", "python", "-m", "ssc_conformance.nightly"], cwd=REPO, env=env)
    rows = nightly_rows(done.stdout)
    return rows, nightly_drift(done.stdout), nightly_error(done.stderr) if not rows else ""


def nightly_error(stderr: str) -> str:
    """The nightly's own error line, which may be followed by a JSON body, else the last line."""
    said = [line.strip() for line in stderr.splitlines() if line.startswith("nightly:")]
    return said[-1] if said else last_line(stderr)


def run(args: argparse.Namespace, run: Run = run_command) -> Outcome:
    if args.step == "nightly":
        rows, drift, error = run_nightly(run, nightly_env(args, os.environ))
        if not rows:
            return Outcome("T3", "the nightly printed no results", None, [error])
        min_zero, gateway = gateway_min(run, args.project)
        outcome = verdict(rows, min_zero, gateway, "from the gateway's position")
        outcome.lines.append(f"drift repaired in: {drift}")
        outcome.lines.append(
            "then restore the schedule: uncomment `schedule` in .github/workflows/nightly.yml "
            "and set the SSC_PROBE_* repository variables to this cell (README, T3)"
        )
        return outcome
    min_zero, gateway = gateway_min(run, args.project)
    return verdict(public_results(args, run), min_zero, gateway, "through the public host")
