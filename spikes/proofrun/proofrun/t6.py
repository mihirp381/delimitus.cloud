"""T6, with the stand-ins the ticket allows (``ssc_datagw`` and ``ssc_egress`` are empty):

- ``t6 nat --project <cell 1> --nat-ip <stack output nat_ip>`` runs the job ``proofrun-egress-nat``
  (``standins/egress``, made once by the README's T6 steps) in the data gateway's place: the
  ``ssc-data`` account and tag, the gateway subnet, Direct VPC egress for all traffic. It fetches
  ``https://1.1.1.1/cdn-cgi/trace`` (an address, so the cell's DNS sinkhole is not in the way)
  and reports the source address. Pass: it is the reserved NAT address.
- ``t6 proxy --project <cell 1> --nat-ip <ip>`` runs ``proofrun-egress-app`` from an app's place
  (the apps subnet, no tag, the zero-role ``ssc-deny-probe`` account), which sends ``CONNECT``
  through the stand-in Envoy at the reserved proxy address 10.20.4.10:3128 for each allowed host
  and each unlisted one. Pass: allowed hosts tunnel and leave from the NAT address; every
  unlisted host is refused.
- ``t6 envoy-config --allow <host> --allow <host> --envoy-image <image@digest> --out <file>``
  writes the cloud-config that starts stock Envoy with that fixed two-host list on the proxy
  machine.

The jobs print one JSON line, ``{"proofrun_egress": {...}}``, read back from Cloud Logging.
Neither result is evidence for SSC-050 or SSC-053: both are stand-ins, named in RESULTS.md.
"""

import argparse
import json
import time
from collections.abc import Callable, Sequence
from importlib import resources
from pathlib import Path
from typing import Any, Final

from proofrun.common import REGION, CommandError, Outcome, Run, gcloud_json, last_line, run_command

NAT_JOB: Final = "proofrun-egress-nat"
APP_JOB: Final = "proofrun-egress-app"
PROXY: Final = "10.20.4.10:3128"
DEFAULT_ALLOWED: Final = ("ifconfig.me", "api.ipify.org")
DEFAULT_UNLISTED: Final = ("example.org", "1.1.1.1")
LOG_WAIT_S: Final = 180
LOG_POLL_S: Final = 15


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="step", required=True)
    for name in ("nat", "proxy"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True)
        p.add_argument("--nat-ip", required=True, help="the cell stack's nat_ip output")
        p.add_argument("--job", default=NAT_JOB if name == "nat" else APP_JOB)
        if name == "proxy":
            p.add_argument("--allow", action="append", help="an allowed host (Envoy's list)")
            p.add_argument("--unlisted", action="append", help="a host Envoy must refuse")
    cfg = sub.add_parser("envoy-config", help="write the proxy machine's cloud-config")
    cfg.add_argument("--allow", action="append", help="an allowed host, two in all")
    cfg.add_argument("--envoy-image", required=True, help="envoyproxy/envoy:<tag>@sha256:...")
    cfg.add_argument("--resolver", default="8.8.8.8", help="public resolver, through the NAT")
    cfg.add_argument("--out", type=Path, required=True, help="where to write it")


def envoy_config(allowed: Sequence[str], image: str, resolver: str) -> str:
    """The cloud-config for the proxy machine, with the allowed hosts and image filled in."""
    template = resources.files("proofrun").joinpath("envoy-cloud-config.yaml").read_text()
    domains = ", ".join(f'"{h}:443"' for h in allowed)
    return (
        template.replace("@DOMAINS@", domains)
        .replace("@IMAGE@", image)
        .replace("@RESOLVER@", resolver)
    )


def execute(run: Run, project: str, job: str, job_args: Sequence[str] = ()) -> str:
    """Run the job to completion and return its execution's name."""
    override = [f"--args={','.join(job_args)}"] if job_args else []
    done = gcloud_json(
        run,
        "run",
        "jobs",
        "execute",
        job,
        f"--region={REGION}",
        f"--project={project}",
        "--wait",
        *override,
    )
    name = (done or {}).get("metadata", {}).get("name")
    if not name:
        raise CommandError(f"{job}: the run named no execution")
    return str(name)


def job_report(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The job's one JSON report among its log entries."""
    for entry in entries:
        payload = entry.get("jsonPayload") or {}
        if "proofrun_egress" in payload:
            return payload["proofrun_egress"]
        text = entry.get("textPayload") or ""
        if text.startswith("{") and "proofrun_egress" in text:
            return json.loads(text)["proofrun_egress"]
    return None


def read_report(
    run: Run,
    project: str,
    job: str,
    execution: str,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    query = (
        f'resource.type="cloud_run_job" AND resource.labels.job_name="{job}" '
        f'AND labels."run.googleapis.com/execution_name"="{execution}"'
    )
    waited = 0.0
    while True:
        entries = gcloud_json(
            run, "logging", "read", query, f"--project={project}", "--freshness=1h", "--limit=50"
        )
        report = job_report(entries or [])
        if report is not None:
            return report
        if waited >= LOG_WAIT_S:
            raise CommandError(f"{execution} wrote no report to Cloud Logging")
        sleep(LOG_POLL_S)
        waited += LOG_POLL_S


def nat_verdict(report: dict[str, Any], nat_ip: str) -> Outcome:
    ip = report.get("ip")
    lines = [f"source address seen by 1.1.1.1: {ip or report.get('error')}"]
    return Outcome(
        "T6",
        f"data-gateway stand-in left from {ip or 'nowhere'}",
        ip == nat_ip,
        [*lines, f"reserved NAT address: {nat_ip}"],
        {"nat": report},
    )


def proxy_verdict(report: dict[str, Any], nat_ip: str) -> Outcome:
    allowed: dict[str, Any] = report.get("allowed", {})
    unlisted: dict[str, Any] = report.get("unlisted", {})
    lines = [
        f"allowed {h}: CONNECT {a.get('status')}, left from {a.get('ip') or a.get('error')}"
        for h, a in allowed.items()
    ]
    lines += [
        f"unlisted {h}: CONNECT {a.get('status') or a.get('error')}" for h, a in unlisted.items()
    ]
    tunnelled = [h for h, a in allowed.items() if a.get("status") == 200 and a.get("ip") == nat_ip]
    refused = [h for h, a in unlisted.items() if a.get("status") != 200]
    passed = (
        bool(allowed)
        and bool(unlisted)
        and len(tunnelled) == len(allowed)
        and len(refused) == len(unlisted)
    )
    return Outcome(
        "T6",
        f"proxy stand-in: {len(tunnelled)}/{len(allowed)} allowed via {nat_ip}, "
        f"{len(refused)}/{len(unlisted)} unlisted refused",
        passed,
        lines,
        {"proxy": report},
    )


def run(args: argparse.Namespace, run: Run = run_command) -> Outcome:
    if args.step == "envoy-config":
        allowed = tuple(args.allow or DEFAULT_ALLOWED)
        args.out.write_text(envoy_config(allowed, args.envoy_image, args.resolver))
        return Outcome(
            "T6 envoy-config", f"{len(allowed)} allowed host(s)", True, [f"wrote {args.out}"]
        )
    if args.step == "nat":
        execution = execute(run, args.project, args.job, ["nat"])
        return nat_verdict(read_report(run, args.project, args.job, execution), args.nat_ip)
    allowed = args.allow or list(DEFAULT_ALLOWED)
    unlisted = args.unlisted or list(DEFAULT_UNLISTED)
    job_args = ["proxy", PROXY, *(f"allow={h}" for h in allowed), *(f"deny={h}" for h in unlisted)]
    try:
        execution = execute(run, args.project, args.job, job_args)
    except CommandError as exc:
        return Outcome("T6", "proxy job did not run", None, [last_line(str(exc))])
    return proxy_verdict(read_report(run, args.project, args.job, execution), args.nat_ip)
