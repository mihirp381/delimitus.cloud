"""T6: the NAT leg with a stand-in job, and the proxy leg against the real ``ssc-egress`` proxy.

- ``t6 nat --project <cell 1> --nat-ip <stack output nat_ip>`` runs the job ``proofrun-egress-nat``
  (``standins/egress``, made once by the README's T6 steps) in the data gateway's place: the
  ``ssc-data`` account and tag, the gateway subnet, Direct VPC egress for all traffic. It fetches
  ``https://1.1.1.1/cdn-cgi/trace``, or each ``--target <host>@<ip>[/<path>]`` (an address, so
  the cell's DNS sinkhole is not in the way), and reports the source address each saw or the
  stage that failed. Pass: at least one answered, and every answer is the reserved NAT address.
  It is a stand-in, named in RESULTS.md, and no evidence for SSC-050.
- ``t6 egress --base <https://pegress--preview.<label>.delimitusapps.com> --nat-ip <ip>`` is the
  proxy leg on a cell whose stack sets ``proxy_image``. It calls the ``egress`` probe app
  (``apps/egress``, deployed in preview) through its public host with the jar's cookie, three
  times: an allowed host with the app's credential (pass: the proxy answers 200, the host 200,
  and the address it saw is the NAT address), an unlisted host with the credential (pass: the
  proxy answers anything but 200; the number is recorded) and the allowed host without the
  credential (pass: 407). The org's allowlist must hold the allowed host and not the unlisted one.
- ``t6 proxy --project <cell 1> --nat-ip <ip>`` runs ``proofrun-egress-app`` from an app's place
  (the apps subnet, no tag, the zero-role ``ssc-deny-probe`` account), which sends ``CONNECT``
  through a stand-in Envoy at the reserved proxy address 10.20.4.10:3128 for each allowed host
  and each unlisted one. It is for a cell without ``proxy_image`` only; the real proxy answers
  407 to a ``CONNECT`` without credentials.
- ``t6 envoy-config --allow <host> --allow <host> --envoy-image <image@digest> --out <file>``
  writes the cloud-config that starts stock Envoy with that fixed two-host list on the proxy
  machine of a cell without ``proxy_image``.

The jobs print one JSON line, ``{"proofrun_egress": {...}}``, read back from Cloud Logging.
"""

import argparse
import json
import time
import urllib.parse
from collections.abc import Callable, Sequence
from importlib import resources
from pathlib import Path
from typing import Any, Final

from proofrun.common import (
    REGION,
    CommandError,
    CookieJar,
    Http,
    Outcome,
    Run,
    fetch,
    gcloud_json,
    host_of,
    last_line,
    run_command,
    session_headers,
)

NAT_JOB: Final = "proofrun-egress-nat"
APP_JOB: Final = "proofrun-egress-app"
PROXY: Final = "10.20.4.10:3128"
DEFAULT_ALLOWED: Final = ("ifconfig.me", "api.ipify.org")
DEFAULT_UNLISTED: Final = ("example.org", "1.1.1.1")
EGRESS_ALLOWED: Final = "www.cloudflare.com"
EGRESS_UNLISTED: Final = "example.com"
EGRESS_TIMEOUT_S: Final = 60.0
NO_CREDENTIAL: Final = 407
LOG_WAIT_S: Final = 180
LOG_POLL_S: Final = 15


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="step", required=True)
    for name in ("nat", "proxy"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True)
        p.add_argument("--nat-ip", required=True, help="the cell stack's nat_ip output")
        p.add_argument("--job", default=NAT_JOB if name == "nat" else APP_JOB)
        if name == "nat":
            p.add_argument(
                "--target",
                action="append",
                help="<host>@<ip>[/<path>] that answers with the caller's address (repeatable)",
            )
        if name == "proxy":
            p.add_argument("--allow", action="append", help="an allowed host (Envoy's list)")
            p.add_argument("--unlisted", action="append", help="a host Envoy must refuse")
    egress = sub.add_parser("egress", help="the real proxy, through the egress probe app")
    egress.add_argument("--base", required=True, help="the probe app's preview URL")
    egress.add_argument("--nat-ip", required=True, help="the cell stack's nat_ip output")
    egress.add_argument("--allow", default=EGRESS_ALLOWED, help="a host on the org's allowlist")
    egress.add_argument("--unlisted", default=EGRESS_UNLISTED, help="a host that is not on it")
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
    """Pass: at least one target answered, and every one that did saw the reserved address.
    A report from the stand-in's first version holds a single ``ip`` (1.1.1.1's answer)."""
    targets: dict[str, dict[str, Any]] = report.get("targets") or {
        "1.1.1.1": {"ip": report.get("ip"), "error": report.get("error")}
    }
    seen = {t: str(a["ip"]) for t, a in targets.items() if a.get("ip")}
    lines = [
        f"{t}: saw {a['ip']}"
        if a.get("ip")
        else f"{t}: no answer, stopped at {a.get('stage', '?')}: "
        f"{a.get('error') or a.get('status')}"
        for t, a in targets.items()
    ]
    passed = bool(seen) and all(ip == nat_ip for ip in seen.values())
    return Outcome(
        "T6",
        f"data-gateway stand-in: {len(seen)}/{len(targets)} target(s) answered"
        + (f", from {', '.join(sorted(set(seen.values())))}" if seen else ""),
        passed,
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


def egress_verdict(
    allowed: dict[str, Any], unlisted: dict[str, Any], anonymous: dict[str, Any], nat_ip: str
) -> Outcome:
    """Pass when the allowed host tunnels and leaves from the NAT address, the unlisted host is
    refused with the credential and the allowed host is refused without it (407)."""
    tunnelled = (
        allowed.get("proxy_status") == 200
        and allowed.get("status") == 200
        and allowed.get("ip") == nat_ip
    )
    refused = unlisted.get("proxy_status") not in (None, 200)
    challenged = anonymous.get("proxy_status") == NO_CREDENTIAL
    lines = [
        f"allowed {allowed.get('host')}: CONNECT {allowed.get('proxy_status')}, "
        f"host {allowed.get('status')}, left from {allowed.get('ip') or allowed.get('error')}",
        f"unlisted {unlisted.get('host')}: CONNECT "
        f"{unlisted.get('proxy_status') or unlisted.get('error')}",
        f"no credential {anonymous.get('host')}: CONNECT "
        f"{anonymous.get('proxy_status') or anonymous.get('error')}",
        f"reserved NAT address: {nat_ip}",
    ]
    if allowed.get("proxy_status") == 403:
        lines.append(
            "403 for the allowed host: the org allowlist may lack it; see the README's T6 steps"
        )
    number = ", ".join(
        (
            "real proxy: allowed via NAT" if tunnelled else "real proxy: allowed host failed",
            f"unlisted refused ({unlisted.get('proxy_status')})"
            if refused
            else f"unlisted not refused ({unlisted.get('proxy_status') or 'no answer'})",
            "no credential 407"
            if challenged
            else f"no credential {anonymous.get('proxy_status') or 'no answer'}",
        )
    )
    return Outcome(
        "T6",
        number,
        tunnelled and refused and challenged,
        lines,
        {"egress": {"allowed": allowed, "unlisted": unlisted, "anonymous": anonymous}},
    )


def egress_call(
    base: str, host: str, credentials: bool, http: Http
) -> tuple[int | None, dict[str, Any]]:
    """Ask the probe app to tunnel to ``host``; its HTTP status and its JSON answer."""
    query = urllib.parse.urlencode({"host": host, "credentials": "yes" if credentials else "no"})
    answer = http(
        f"{base}/egress?{query}", session_headers(CookieJar().get(host_of(base))), EGRESS_TIMEOUT_S
    )
    try:
        body = json.loads(answer.body)
    except ValueError:
        body = {}
    return answer.status, body if isinstance(body, dict) else {}


def run_egress(args: argparse.Namespace, http: Http) -> Outcome:
    """The three calls of ``t6 egress`` and their verdict."""
    base = str(args.base).rstrip("/")
    if not base.startswith("https://"):
        raise CommandError("--base is the probe app's https:// URL from `ssc status`")
    calls = (("allowed", args.allow, True), ("unlisted", args.unlisted, True))
    calls += (("no credential", args.allow, False),)
    answers: list[dict[str, Any]] = []
    for label, host, credentials in calls:
        status, body = egress_call(base, host, credentials, http)
        if status != 200:
            return Outcome("T6", "the probe app did not answer", None, [f"{label}: HTTP {status}"])
        answers.append(body)
    return egress_verdict(answers[0], answers[1], answers[2], args.nat_ip)


def run(args: argparse.Namespace, run: Run = run_command, http: Http = fetch) -> Outcome:
    if args.step == "egress":
        return run_egress(args, http)
    if args.step == "envoy-config":
        allowed = tuple(args.allow or DEFAULT_ALLOWED)
        args.out.write_text(envoy_config(allowed, args.envoy_image, args.resolver))
        return Outcome(
            "T6 envoy-config", f"{len(allowed)} allowed host(s)", True, [f"wrote {args.out}"]
        )
    if args.step == "nat":
        execution = execute(run, args.project, args.job, ["nat", *(args.target or ())])
        return nat_verdict(read_report(run, args.project, args.job, execution), args.nat_ip)
    allowed = args.allow or list(DEFAULT_ALLOWED)
    unlisted = args.unlisted or list(DEFAULT_UNLISTED)
    job_args = ["proxy", PROXY, *(f"allow={h}" for h in allowed), *(f"deny={h}" for h in unlisted)]
    try:
        execution = execute(run, args.project, args.job, job_args)
    except CommandError as exc:
        return Outcome("T6", "proxy job did not run", None, [last_line(str(exc))])
    return proxy_verdict(read_report(run, args.project, args.job, execution), args.nat_ip)
