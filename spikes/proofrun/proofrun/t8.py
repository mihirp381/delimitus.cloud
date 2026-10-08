"""T8: the kill drill against an open WebSocket, with ``ssc disable`` (the console has no login
until SSC-064, so the CLI is the fallback the ticket names).

``t8 --app <api probe app> --label <cell label> [--env preview]``

1. Checks the app answers ``/health`` through its public host with the session cookie.
2. Opens ``wss://<host>/ws`` (the API probe app sends a tick a second) and starts asking
   ``/health`` every 0.25 s.
3. Runs ``ssc disable <app> --json`` and times, from the moment it starts: the first refused
   ``/health`` (the front door), the WebSocket's end (the stream cut), and the command's return.
4. Reads the run's ``kill_switch.step`` audit events for each step's ``since_command_ms``
   (``scale_to_zero`` is the instances reaching 0, confirmed by the step itself), and the time
   ``snapshots/<org>/latest.json`` changed in the cell bucket (the compile).

Pass: the refusal, the stream cut and every step land within 10 s of the command, and every step
is ``done``. There is no data gateway, so no query or tunnel leg (SSC-054); the results file says
so. Undo with ``ssc enable <app>``.
"""

import argparse
import json
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from proofrun.common import (
    REPO,
    CommandError,
    CookieJar,
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
    ssc_json,
    ssc_prefix,
)

LIMIT_S: Final = 10.0
POLL_S: Final = 0.25
WATCH_S: Final = 60.0
STEP_ACTION: Final = "kill_switch.step"
LOGIN_READER: Final = (
    "import sys\n"
    "from ssc_cli.config import load_config\n"
    "from ssc_cli.credentials import read_token\n"
    "c = load_config()\n"
    "sys.stdout.write(c.api_url + '\\n' + read_token(c.api_url))\n"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app", required=True, help="the API probe app's slug")
    parser.add_argument("--label", required=True, help="the cell's label (its bucket)")
    parser.add_argument("--env", default="preview")
    parser.add_argument("--ws-path", default="/ws")


@dataclass
class Watch:
    """What the watchers saw, in seconds from the command's start."""

    refused_s: float | None = None
    refused_status: int | None = None
    cut_s: float | None = None
    ticks: int = 0
    errors: list[str] = field(default_factory=list)


def since_by_step(page: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Each finished step's ``since_command_ms`` and state from one audit page."""
    out: dict[str, dict[str, Any]] = {}
    for event in page.get("events", []):
        after = event.get("after") or {}
        if event.get("action") != STEP_ACTION or after.get("state") == "running":
            continue
        out[str(after.get("step"))] = {
            "since_command_ms": after.get("since_command_ms"),
            "state": after.get("state"),
        }
    return out


def summarise(  # noqa: PLR0913  (keyword-only)
    *,
    watch: Watch,
    cli_s: float | None,
    result: Mapping[str, Any] | None,
    audit: Mapping[str, Mapping[str, Any]],
    compile_s: float | None,
) -> Outcome:
    """The drill's numbers and verdict."""
    steps = list((result or {}).get("steps", []))
    lines = [
        f"front door refused after {_s(watch.refused_s)} (HTTP {watch.refused_status})",
        f"WebSocket cut after {_s(watch.cut_s)} ({watch.ticks} ticks before)",
        f"ssc disable returned after {_s(cli_s)}, run state {(result or {}).get('state')}",
        f"latest.json changed {_s(compile_s)} after the command (compile and publish)",
    ]
    for step in steps:
        since = audit.get(step["name"], {}).get("since_command_ms")
        lines.append(
            f"step {step['name']}: {step['state']}, {step.get('elapsed_ms')} ms, "
            f"{since} ms since the command, {step.get('attempts')} attempt(s)"
            + (f", error {step['error']}" if step.get("error") else "")
        )
    lines += [f"watcher: {e}" for e in watch.errors]
    since_ms = [
        float(a["since_command_ms"])
        for a in audit.values()
        if a.get("since_command_ms") is not None
    ]
    zero = audit.get("scale_to_zero", {}).get("since_command_ms")
    end = max(
        [v for v in (watch.refused_s, watch.cut_s) if v is not None] + [m / 1000 for m in since_ms],
        default=None,
    )
    complete = watch.refused_s is not None and watch.cut_s is not None and bool(steps)
    all_done = bool(steps) and all(s["state"] == "done" for s in steps)
    passed: bool | None = (
        (end is not None and end < LIMIT_S and all_done and len(since_ms) == len(steps))
        if complete
        else (False if result and not all_done else None)
    )
    number = (
        f"end to end {_s(end)} (refusal {_s(watch.refused_s)}, stream cut {_s(watch.cut_s)}, "
        f"instances 0 at {_s(None if zero is None else zero / 1000)})"
    )
    data = {
        "refused_s": watch.refused_s,
        "cut_s": watch.cut_s,
        "cli_s": cli_s,
        "compile_s": compile_s,
        "audit": dict(audit),
        "steps": steps,
    }
    return Outcome("T8", number, passed, lines, data)


def _s(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f} s"


def control_api(run: Run) -> tuple[str, str]:
    """The control plane's URL and the operator's token, through the CLI's own code, kept in
    memory and never printed."""
    done = run(["uv", "run", "python", "-c", LOGIN_READER], cwd=REPO)
    url, _, token = done.stdout.partition("\n")
    if done.returncode != 0 or not token:
        raise CommandError("no ssc login: run `ssc login` or set SSC_TOKEN")
    return url.strip(), token.strip()


def search_audit(
    http: Http, api: str, token: str, filters: Mapping[str, str | int]
) -> list[dict[str, Any]]:
    """One page of ``GET /v1/audit`` with ``filters`` (action, target_kind, since, ...), newest
    first. The token stays in the header; only the status is said on a refusal."""
    query = urllib.parse.urlencode(dict(filters))
    answer = http(f"{api}/v1/audit?{query}", {"Authorization": f"Bearer {token}"}, 30.0)
    if answer.status != 200:
        raise CommandError(f"audit search answered {answer.status or answer.error}")
    return list(json.loads(answer.body).get("events", []))


def read_audit(run: Run, http: Http, run_id: str) -> dict[str, dict[str, Any]]:
    api, token = control_api(run)
    filters = {
        "action": STEP_ACTION,
        "target_kind": "kill_switch_run",
        "target_id": run_id,
        "limit": 50,
    }
    return since_by_step({"events": search_audit(http, api, token, filters)})


def latest_changed(run: Run, label: str, org_id: str) -> datetime:
    doc = gcloud_json(
        run,
        "storage",
        "objects",
        "describe",
        f"gs://ssc-c-{label}-cell/snapshots/{org_id}/latest.json",
    )
    stamp = doc.get("update_time") or doc.get("updated")
    if not stamp:
        raise CommandError("latest.json has no update time")
    return parse_time(stamp)


def watch_stream(ws: Any, started: Callable[[], float | None], watch: Watch, stop: threading.Event):
    """Count ticks until the server ends the stream; record when, after the command started."""
    try:
        while not stop.is_set():
            try:
                ws.recv(timeout=1.0)
                watch.ticks += 1
            except TimeoutError:
                continue
    except Exception as exc:  # noqa: BLE001  (any end of the stream is the cut)
        t0 = started()
        if t0 is not None:
            watch.cut_s = time.monotonic() - t0
        else:
            watch.errors.append(f"stream ended before the command: {type(exc).__name__}")


def watch_door(  # noqa: PLR0913, PLR0917  (a thread target takes positional arguments)
    http: Http,
    url: str,
    headers: Mapping[str, str],
    started: Callable[[], float | None],
    watch: Watch,
    stop: threading.Event,
) -> None:
    """Ask ``/health`` every 0.25 s; record the first answer that is not 200."""
    while not stop.is_set() and watch.refused_s is None:
        answer = http(url, headers, 5.0)
        t0 = started()
        if t0 is not None and answer.status is not None and answer.status != 200:
            watch.refused_s = time.monotonic() - t0
            watch.refused_status = answer.status
            return
        time.sleep(POLL_S)


def open_socket(url: str, origin: str, headers: Mapping[str, str]) -> Any:
    from websockets.sync.client import connect  # noqa: PLC0415
    from websockets.typing import Origin  # noqa: PLC0415

    return connect(url, origin=Origin(origin), additional_headers=dict(headers), open_timeout=120)


def run(
    args: argparse.Namespace,
    run: Run = run_command,
    http: Http = fetch,
    socket: Callable[[str, str, Mapping[str, str]], Any] = open_socket,
) -> Outcome:
    env = app_environment(run, args.app, args.env)
    base = env["url"].rstrip("/")
    host = host_of(base)
    headers = session_headers(CookieJar().get(host))
    health = http(base + "/health", headers, 120.0)
    if health.status != 200:
        return Outcome("T8", "the app did not answer before the drill", None, [f"{health}"])
    org_id = ssc_json(run, "whoami")["org_id"]
    ws = socket(f"wss://{host}{args.ws_path}", f"https://{host}", headers)
    ws.recv(timeout=30.0)
    watch, stop, t0 = Watch(), threading.Event(), list[float]()
    started: Callable[[], float | None] = lambda: t0[0] if t0 else None  # noqa: E731
    threads = [
        threading.Thread(target=watch_stream, args=(ws, started, watch, stop), daemon=True),
        threading.Thread(
            target=watch_door,
            args=(http, base + "/health", headers, started, watch, stop),
            daemon=True,
        ),
    ]
    for t in threads:
        t.start()
    wall0 = datetime.now(UTC)
    t0.append(time.monotonic())
    done = run([*ssc_prefix(), "disable", args.app, "--json"], cwd=REPO)
    cli_s = time.monotonic() - t0[0]
    deadline = t0[0] + WATCH_S
    while (watch.refused_s is None or watch.cut_s is None) and time.monotonic() < deadline:
        time.sleep(POLL_S)
    stop.set()
    ws.close()
    result: dict[str, Any] | None = None
    audit: dict[str, dict[str, Any]] = {}
    compile_s: float | None = None
    if done.returncode == 0:
        result = json.loads(done.stdout)
        audit = read_audit(run, http, str((result or {})["run_id"]))
        compile_s = (latest_changed(run, args.label, org_id) - wall0).total_seconds()
    else:
        watch.errors.append(f"ssc disable failed: {last_line(done.stderr or done.stdout)}")
    outcome = summarise(watch=watch, cli_s=cli_s, result=result, audit=audit, compile_s=compile_s)
    outcome.lines.append(f"undo: ssc enable {args.app}")
    return outcome
