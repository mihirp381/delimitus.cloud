"""T5: the cell's ``db-f1-micro``, ten app databases, the cross-connect test, the restore.

- ``t5 ops --project <cell 1>`` reads Cloud SQL's own operation log for the instance
  (``gcloud sql operations list``, read-only): the instance's ``CREATE`` and each
  ``CREATE_DATABASE``, timed from the request to done. With ``--restore-instance`` it also times
  the clone's creation (the restore drill, README T5). Pass: instance under 15 minutes, at least
  ten databases, each under 1 minute.
- ``t5 cross --app pg01 --other pg02 ... --other pg10`` asks ``pg01`` (``apps/pg``) to connect to
  each other app's database with its own credentials. Pass: its own database connects, and every
  other one is refused with a privilege or login error (not a missing database or a network
  error). The ``postgres`` maintenance database is reported, not counted.
"""

import argparse
import json
import urllib.parse
from datetime import datetime
from typing import Any, Final

from proofrun.common import (
    CookieJar,
    Http,
    Outcome,
    Run,
    app_environment,
    database_name,
    fetch,
    gcloud_json,
    host_of,
    parse_time,
    run_command,
    session_headers,
)

INSTANCE: Final = "ssc-cell"
INSTANCE_LIMIT_S: Final = 15 * 60
DATABASE_LIMIT_S: Final = 60
DATABASES: Final = 10
REFUSED_STATES: Final = frozenset({"42501", "28000", "28P01"})
MAINTENANCE_DB: Final = "postgres"
REFUSED_MESSAGES: Final = {
    "FATAL:  permission denied for database": "42501",
    "FATAL:  password authentication failed for user": "28P01",
    "FATAL:  no pg_hba.conf entry": "28000",
    "FATAL:  pg_hba.conf rejects connection": "28000",
}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="step", required=True)
    ops = sub.add_parser("ops", help="instance and database creation times")
    ops.add_argument("--project", required=True)
    ops.add_argument("--instance", default=INSTANCE)
    ops.add_argument("--restore-instance", help="the clone made by the restore drill")
    cross = sub.add_parser("cross", help="the cross-connect test")
    cross.add_argument("--app", required=True, help="the app that tries")
    cross.add_argument("--other", action="append", required=True, help="another pg app's slug")
    cross.add_argument("--env", default="preview")


def _seconds(op: dict[str, Any], start: str = "insertTime") -> float | None:
    if op.get("status") != "DONE" or not op.get("endTime") or not op.get(start):
        return None
    began: datetime = parse_time(op[start])
    return (parse_time(op["endTime"]) - began).total_seconds()


def creation_times(ops: list[dict[str, Any]]) -> tuple[list[float], list[float], list[str]]:
    """Instance creations, database creations (seconds each) and failed operations."""
    instance = [s for o in ops if o.get("operationType") == "CREATE" if (s := _seconds(o))]
    databases = [
        s for o in ops if o.get("operationType") == "CREATE_DATABASE" if (s := _seconds(o))
    ]
    failed = [
        f"{o.get('operationType')} {o.get('name')}: {o['error']}" for o in ops if o.get("error")
    ]
    return instance, databases, failed


def ops_verdict(ops: list[dict[str, Any]], restore: list[dict[str, Any]] | None = None) -> Outcome:
    instance, databases, failed = creation_times(ops)
    lines = [f"instance created in {s / 60:.1f} min" for s in instance]
    lines += [f"database {i + 1} created in {s:.1f} s" for i, s in enumerate(sorted(databases))]
    lines += [f"failed: {f}" for f in failed]
    if restore is not None:
        clone = [
            s
            for o in restore
            if o.get("operationType") in {"CLONE", "CREATE"}
            if (s := _seconds(o))
        ]
        lines.append(
            "restore clone: "
            + (f"{clone[0] / 60:.1f} min" if clone else "no finished CLONE or CREATE operation")
        )
    worst_db = max(databases) if databases else None
    passed = (
        bool(instance)
        and instance[-1] < INSTANCE_LIMIT_S
        and len(databases) >= DATABASES
        and worst_db is not None
        and worst_db < DATABASE_LIMIT_S
    )
    number = (
        f"instance {instance[-1] / 60:.1f} min, " if instance else "instance n/a, "
    ) + f"{len(databases)} databases, slowest {worst_db or 0:.1f} s"
    return Outcome(
        "T5",
        number,
        passed,
        lines,
        {"instance_s": instance, "databases_s": databases, "failed": failed},
    )


def sqlstate(answer: dict[str, Any]) -> str:
    """The answer's SQLSTATE, else the one Postgres's own refusal text stands for: psycopg
    reports none for an error raised while connecting."""
    if state := str(answer.get("sqlstate") or ""):
        return state
    error = str(answer.get("error") or "")
    return next((s for text, s in REFUSED_MESSAGES.items() if text in error), "")


def classify(answer: dict[str, Any]) -> str:
    """``connected``, ``refused`` (privilege or login), ``missing`` (no such database) or
    ``error`` (anything else, the network included)."""
    if answer.get("connected"):
        return "connected"
    state = sqlstate(answer)
    if state in REFUSED_STATES:
        return "refused"
    return "missing" if state == "3D000" else "error"


def cross_verdict(own: dict[str, Any], others: dict[str, dict[str, Any]]) -> Outcome:
    kinds = {name: classify(a) for name, a in others.items()}
    lines = [f"own database: {classify(own)} ({own.get('database') or own.get('error')})"]
    lines += [
        f"{name}: {kinds[name]} ({sqlstate(a) or None} {a.get('error') or ''})".rstrip()
        for name, a in others.items()
    ]
    counted = {n: k for n, k in kinds.items() if n != MAINTENANCE_DB}
    refused = sum(1 for k in counted.values() if k == "refused")
    passed = classify(own) == "connected" and bool(counted) and refused == len(counted)
    return Outcome(
        "T5",
        f"cross-connect refused {refused}/{len(counted)}",
        passed,
        lines,
        {"kinds": kinds},
    )


def _json(http: Http, url: str, headers: dict[str, str]) -> dict[str, Any]:
    answer = http(url, headers, 60.0)
    if answer.status != 200:
        return {"connected": False, "error": f"HTTP {answer.status or answer.error}"}
    return json.loads(answer.body)


def run(args: argparse.Namespace, run: Run = run_command, http: Http = fetch) -> Outcome:
    if args.step == "ops":
        ops = gcloud_json(
            run,
            "sql",
            "operations",
            "list",
            f"--instance={args.instance}",
            f"--project={args.project}",
            "--limit=500",
        )
        restore = None
        if args.restore_instance:
            restore = gcloud_json(
                run,
                "sql",
                "operations",
                "list",
                f"--instance={args.restore_instance}",
                f"--project={args.project}",
            )
        return ops_verdict(ops or [], restore)
    env = app_environment(run, args.app, args.env)
    base = env["url"].rstrip("/")
    headers = session_headers(CookieJar().get(host_of(env["url"])))
    names = [database_name(app_environment(run, s, args.env)["id"]) for s in args.other]
    own = _json(http, base + "/db/own", headers)
    others = {
        name: _json(http, f"{base}/db/cross?{urllib.parse.urlencode({'name': name})}", headers)
        for name in [*names, MAINTENANCE_DB]
    }
    return cross_verdict(own, others)
