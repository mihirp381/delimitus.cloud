"""T11: from an app in cell 1, read cell 2's secret and list cell 2's bucket.

``t11 --app <api probe app in cell 1> --peer-label <cell 2's label> [--secret ssc-a-probe]``

The API probe app's ``/deny`` takes its own identity's token from the metadata server and asks
Secret Manager for the latest version of ``projects/ssc-c-<label 2>/secrets/<secret>`` and
Cloud Storage for one object name in ``ssc-c-<label 2>-cell``. It returns each answer's status
and Google's reason, never a payload. Each leg is sorted:

- ``iam``: 401 or 403, Google's IAM refused the identity;
- ``network``: no answer at all (the call never reached Google);
- ``not_found``: 404 (wrong name: the probe proves nothing);
- ``allowed``: 200, a breach.

Pass: both legs are ``iam``. Run it after SSC-095's policies are applied, and before T12
destroys cell 2. The cell's own ``deny_probe`` (infra/README) is the same check from the
operator's side.
"""

import argparse
import json
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any, Final

from proofrun.common import (
    CookieJar,
    Http,
    Outcome,
    Run,
    app_environment,
    fetch,
    host_of,
    run_command,
    session_headers,
)

PROBE_SECRET: Final = "ssc-a-probe"  # noqa: S105  (a secret's name, not a value)
LABEL: Final = re.compile(r"[a-z][a-z0-9]{7,15}")
LEGS: Final = ("secret", "bucket")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app", required=True, help="the API probe app's slug, in cell 1")
    parser.add_argument("--peer-label", required=True, help="cell 2's label")
    parser.add_argument("--secret", default=PROBE_SECRET)
    parser.add_argument("--env", default="preview")


def classify(leg: Mapping[str, Any]) -> str:
    status = leg.get("status")
    if status is None:
        return "network"
    if status in {401, 403}:
        return "iam"
    if status == 404:
        return "not_found"
    return "allowed" if status == 200 else f"http {status}"


def verdict(answer: Mapping[str, Any]) -> Outcome:
    kinds = {leg: classify(answer.get(leg) or {}) for leg in LEGS}
    lines = [
        f"{leg}: {kinds[leg]} (HTTP {(answer.get(leg) or {}).get('status')}, "
        f"{(answer.get(leg) or {}).get('reason') or (answer.get(leg) or {}).get('error')})"
        for leg in LEGS
    ]
    breach = [leg for leg, k in kinds.items() if k == "allowed"]
    if breach:
        lines.append(f"BREACH: cell 1's app read cell 2's {', '.join(breach)}")
    return Outcome(
        "T11",
        "; ".join(f"{leg} {kinds[leg]}" for leg in LEGS),
        all(k == "iam" for k in kinds.values()),
        lines,
        {"kinds": kinds},
    )


def run(args: argparse.Namespace, run: Run = run_command, http: Http = fetch) -> Outcome:
    if not LABEL.fullmatch(args.peer_label):
        raise SystemExit("--peer-label is 8 to 16 lower-case letters and digits, letter first")
    env = app_environment(run, args.app, args.env)
    base = env["url"].rstrip("/")
    query = urllib.parse.urlencode(
        {
            "project": f"ssc-c-{args.peer_label}",
            "secret": args.secret,
            "bucket": f"ssc-c-{args.peer_label}-cell",
        }
    )
    answer = http(f"{base}/deny?{query}", session_headers(CookieJar().get(host_of(base))), 60.0)
    if answer.status != 200:
        return Outcome("T11", "the probe app did not answer", None, [f"HTTP {answer.status}"])
    return verdict(json.loads(answer.body))
