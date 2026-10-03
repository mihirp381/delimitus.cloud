"""T2: the cell's load balancer, wildcard certificate and public host.

``t2 --label <cell 1> --project <cell 1 project> --zone-project <apps zone project> --app <slug>``

1. Certificate issue time: from the Cloud DNS change that added the authorisation CNAME
   (``_acme-challenge.<label>.``) to the certificate's last update once it is ``ACTIVE``.
2. ``ssc_infra.entry_probe``: the public ``www`` host answers, the gateway's ``run.app`` host is
   refused.
3. The probe app answers on its public host with the session cookie a browser login left in the
   cookie jar. A cookie sealed by ``seal_cookie.py`` passes too, but the result then says the
   real login was not exercised.

Pass: certificate ``ACTIVE`` within 30 minutes of the record, ``entry_probe`` passed, and the
app answered 200.
"""

import argparse
from datetime import datetime
from typing import Any, Final

from proofrun.common import (
    REPO,
    CookieJar,
    Fetched,
    Http,
    Outcome,
    Run,
    app_environment,
    fetch,
    gcloud_json,
    host_of,
    last_line,
    parse_time,
    run_command,
    session_headers,
)

CERTIFICATE: Final = "ssc-cell-wildcard"
ISSUE_LIMIT_MINUTES: Final = 30.0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--label", required=True, help="cell 1's label")
    parser.add_argument("--project", required=True, help="cell 1's project id")
    parser.add_argument("--project-number", help="cell 1's project number (else from the stack)")
    parser.add_argument("--zone-project", required=True, help="project holding the apps zone")
    parser.add_argument("--zone", default="delimitusapps", help="the apps zone's name")
    parser.add_argument("--app", required=True, help="the probe app's slug")
    parser.add_argument("--env", default="preview", help="the environment deployed to")


def record_added(changes: list[dict[str, Any]], label: str) -> datetime | None:
    """When the authorisation CNAME for ``label`` was first added to the apps zone."""
    name = f"_acme-challenge.{label}."
    times = [
        parse_time(c["startTime"])
        for c in changes
        if any(
            a.get("type") == "CNAME" and str(a.get("name", "")).startswith(name)
            for a in c.get("additions", [])
        )
    ]
    return min(times) if times else None


def issue_minutes(certificate: dict[str, Any], added: datetime | None) -> tuple[str, float | None]:
    """The certificate's state, and minutes from the record to its last update once active."""
    state = str(certificate.get("managed", {}).get("state", "UNKNOWN"))
    if state != "ACTIVE" or added is None:
        return state, None
    return state, (parse_time(certificate["updateTime"]) - added).total_seconds() / 60


def direct_line(stderr: str) -> str:
    """What ``entry_probe`` saw on the gateway's ``run.app`` host."""
    found = [line for line in stderr.splitlines() if line.startswith("direct:")]
    return found[-1] if found else last_line(stderr)


def verdict(  # noqa: PLR0913  (keyword-only)
    *,
    state: str,
    minutes: float | None,
    entry_passed: bool,
    entry_line: str,
    answer: Fetched,
    cookie_source: str,
) -> Outcome:
    lines = [
        f"certificate {CERTIFICATE}: {state}, "
        + (f"{minutes:.1f} min after the record" if minutes is not None else "issue time unknown"),
        f"entry_probe: {'PASS' if entry_passed else 'FAIL'} ({entry_line})",
        f"probe app on its public host: {answer.status or answer.error} "
        f"in {answer.seconds:.2f} s, cookie from a {cookie_source} session",
    ]
    if cookie_source == "sealed":
        lines.append("the real login was not exercised: the cookie was sealed by seal_cookie.py")
    cert_ok = minutes is not None and minutes <= ISSUE_LIMIT_MINUTES
    passed = cert_ok and entry_passed and answer.status == 200
    number = f"certificate {minutes:.1f} min" if minutes is not None else "certificate n/a"
    return Outcome("T2", f"{number}, app HTTP {answer.status}", passed, lines)


def run(args: argparse.Namespace, run: Run = run_command, http: Http = fetch) -> Outcome:
    certificate = gcloud_json(
        run,
        "certificate-manager",
        "certificates",
        "describe",
        CERTIFICATE,
        "--location=global",
        f"--project={args.project}",
    )
    changes = gcloud_json(
        run,
        "dns",
        "record-sets",
        "changes",
        "list",
        f"--zone={args.zone}",
        f"--project={args.zone_project}",
        "--sort-order=ascending",
    )
    state, minutes = issue_minutes(certificate, record_added(changes or [], args.label))
    entry_args = [args.label, *([args.project_number] if args.project_number else [])]
    entry = run(
        ["uv", "run", "python", "-m", "ssc_infra.entry_probe", *entry_args], cwd=REPO / "infra"
    )
    env = app_environment(run, args.app, args.env)
    cookie = CookieJar().get(host_of(env["url"]))
    answer = http(env["url"].rstrip("/") + "/health", session_headers(cookie))
    return verdict(
        state=state,
        minutes=minutes,
        entry_passed=entry.returncode == 0,
        entry_line=direct_line(entry.stderr),
        answer=answer,
        cookie_source=cookie.source,
    )
