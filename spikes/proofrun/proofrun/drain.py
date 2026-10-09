"""GA-6.1: live revoke and drain. Remove a host from the org's egress allowlist while an app holds
a tunnel to it: the open tunnel is cut and a new one is refused (403).

``drain --app <egress probe app> --label <cell label> [--env preview] [--host www.cloudflare.com]
[--org <org id>] [--project ssc-c-<label>] [--hold-seconds 90]``

The app is ``apps/egress`` (``pegress``), deployed with its ``/hold`` route. The kit reaches it
through its public host with the session cookie from the jar. The allowlist and the audit log are
read and changed with the CLI's login, which must be an org admin and not an agent session; the
org is the login's (``ssc whoami``), and ``--org`` must name the same one. ``latest.json`` and
Cloud Logging are read with the operator's ``gcloud`` login.

1. Read only: the host is on the allowlist and ``/egress?host=`` tunnels (proxy 200). Else the
   kit stops and nothing is changed.
2. ``/hold`` streams while the app holds a tunnel to the host; the kit waits for ``open`` and
   three answered keep-alive ticks.
3. **[real]** ``DELETE /v1/egress/hosts/<host>``, timed from just before it is sent.
4. Up to 30 s for the hold's ``end`` line, the time the app saw the tunnel close.
5. A new ``/egress?host=``: the proxy must answer 403.
6. The update time of ``snapshots/<org>/latest.json``, read before the host is put back.
7. **[real]** Always, also on Ctrl-C, once the delete answered 200: ``PUT`` the host back and
   read the allowlist. The last line says whether it is back; when it is not, the command exits
   1 whatever the verdict.
8. Evidence, up to 120 s for Cloud Logging: the proxy's first ``egress listener written`` line
   after the delete, the proxy's access line for the held tunnel (only its time, status, flags
   and duration are shown, never its user), the app's ``egress hold end`` line and the audit
   rows ``org.updated`` on ``egress_host`` for the removal and the re-add.

Numbers: compile (delete to ``latest.json``), poll (``latest.json`` to the listener), drain (the
listener to the cut), proxy (``latest.json`` to the cut) and end to end (the delete to the kit
seeing the end line, not judged). Pass: the tunnel was cut (not held to its end), by the first
listener written after the change (``latest.json`` <= listener <= cut), the proxy number within
``POLL_SECONDS + DRAIN_SECONDS + 1`` s, the new tunnel refused with 403, and the removal row in
the audit log.
"""

import argparse
import json
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from proofrun import t6, t8
from proofrun.common import (
    USER_AGENT,
    CommandError,
    CookieJar,
    Fetched,
    Http,
    Outcome,
    Run,
    app_environment,
    app_service,
    emit,
    fence,
    gcloud_json,
    host_of,
    parse_time,
    run_command,
    session_headers,
    ssc_json,
)
from proofrun.files import Send, Stream, open_stream, request

PROOF: Final = "GA-6.1"
DEFAULT_HOST: Final = t6.EGRESS_ALLOWED
POLL_SECONDS: Final = 2.0
"""``ssc_shared.snapshot_feed.POLL_SECONDS``: how often the proxy reads the snapshot."""
DRAIN_SECONDS: Final = 5.0
"""``ssc_contracts.egress.DRAIN_SECONDS``: Envoy's ``--drain-time-s``."""
SLACK_S: Final = 1.0
LIMIT_S: Final = POLL_SECONDS + DRAIN_SECONDS + SLACK_S
READY_TICKS: Final = 3
READY_WAIT_S: Final = 30.0
CUT_WAIT_S: Final = 30.0
STREAM_TIMEOUT_S: Final = 30.0
LOG_WAIT_S: Final = 120.0
LOG_POLL_S: Final = 10.0
AUDIT_LOOKBACK: Final = timedelta(seconds=30)
REFUSED: Final = 403
TUNNELLED: Final = 200
LISTENER_LINE: Final = "egress listener written"
APP_LINE: Final = "egress hold end"
ACTION: Final = "org.updated"
TARGET_KIND: Final = "egress_host"
NOT_A_CUT: Final = ("max", "client_gone", "no HTTPS_PROXY")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app", required=True, help="the egress probe app's slug (pegress)")
    parser.add_argument("--label", required=True, help="the cell's label (its bucket)")
    parser.add_argument("--env", default="preview")
    parser.add_argument("--host", default=DEFAULT_HOST, help="a host on the org's allowlist")
    parser.add_argument("--org", default=None, help="must be the CLI login's org")
    parser.add_argument("--project", default=None, help="the cell's project (ssc-c-<label>)")
    parser.add_argument("--hold-seconds", type=int, default=90, help="the hold's limit, 5-150")


@dataclass
class HoldWatch:
    """What the hold's stream said. ``opened_at`` is the app's clock; ``end_seen`` the kit's
    monotonic clock when the end line arrived."""

    opened_at: float | None = None
    answered: int = 0
    end: dict[str, Any] | None = None
    end_seen: float | None = None
    error: str | None = None
    ready: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def up(self) -> bool:
        return self.opened_at is not None and self.answered >= READY_TICKS and self.end is None


def watch_hold(
    stream: Stream,
    url: str,
    headers: Mapping[str, str],
    watch: HoldWatch,
    clock: Callable[[], float],
) -> None:
    """Read the hold's lines until its end line; ``ready`` once it is up or over."""
    try:
        for line in stream(url, headers, STREAM_TIMEOUT_S):
            if not line.strip():
                continue
            event = json.loads(line)
            kind = event.get("event")
            if kind == "open":
                watch.opened_at = float(event["at"])
            elif kind == "alive" and int(event.get("bytes") or 0) > 0:
                watch.answered += 1
            elif kind == "end":
                watch.end, watch.end_seen = event, clock()
                break
            if watch.up:
                watch.ready.set()
        else:
            watch.error = "the hold's stream ended without an end line"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        watch.error = f"the hold's stream failed: {type(exc).__name__}: {exc}"
    finally:
        watch.ready.set()
        watch.done.set()


def log_payload(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """The JSON object a log entry carries: its ``jsonPayload``, or the JSON in that payload's
    ``message`` or in its ``textPayload``."""
    payload = entry.get("jsonPayload")
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message.lstrip().startswith("{"):
            decoded = _object(message)
            if decoded is not None:
                return decoded
        return payload
    text = entry.get("textPayload")
    if isinstance(text, str) and "{" in text:
        return _object(text[text.index("{") :])
    return None


def _object(text: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(text)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class TunnelLine:
    """The proxy's access line for the held tunnel. Its ``user`` is never kept."""

    started: datetime
    status: int | None
    flags: str
    ms: int | None

    @property
    def ended(self) -> datetime | None:
        return None if self.ms is None else self.started + timedelta(milliseconds=self.ms)

    def describe(self) -> str:
        ended = self.ended.isoformat() if self.ended else "n/a"
        return (
            f"proxy access line: CONNECT {self.status}, flags {self.flags}, {self.ms} ms, "
            f"started {self.started.isoformat()}, ended {ended}"
        )


def tunnel_line(entries: Iterable[Mapping[str, Any]], host: str) -> TunnelLine | None:
    """The longest tunnel the proxy let through to ``host:443`` among ``entries``: the held one,
    not the short ``/egress`` checks."""
    found: list[TunnelLine] = []
    for entry in entries:
        payload = log_payload(entry)
        if not payload or payload.get("authority") != f"{host}:443":
            continue
        status = _int(payload.get("status"))
        if status != TUNNELLED or not payload.get("at"):
            continue
        flags = str(payload.get("flags") or "-")
        started = parse_time(str(payload["at"]))
        found.append(TunnelLine(started, status, flags, _int(payload.get("ms"))))
    return max(found, key=lambda t: t.ms or 0, default=None)


def first_listener(entries: Iterable[Mapping[str, Any]], since: datetime) -> datetime | None:
    """The first time the proxy wrote a listener at or after ``since``."""
    times = [
        parse_time(str(e["timestamp"]))
        for e in entries
        if e.get("timestamp")
        and LISTENER_LINE in json.dumps([e.get("jsonPayload"), e.get("textPayload")])
    ]
    return min((t for t in times if t >= since), default=None)


def app_end(entries: Iterable[Mapping[str, Any]], run_id: str) -> dict[str, Any] | None:
    """The app's ``egress hold end`` line for this run."""
    for entry in entries:
        hold = (log_payload(entry) or {}).get("hold")
        if isinstance(hold, dict) and hold.get("run") == run_id:
            return hold
    return None


def audit_rows(
    events: Iterable[Mapping[str, Any]], host: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The oldest removal (``before`` and no ``after``) and the newest addition (``after`` and no
    ``before``) of ``host`` among ``org.updated`` events on ``egress_host``."""
    mine = [
        dict(e)
        for e in events
        if e.get("action") == ACTION
        and (e.get("target") or {}).get("kind") == TARGET_KIND
        and (e.get("target") or {}).get("id") == host
    ]
    removed = [e for e in mine if e.get("before") and not e.get("after")]
    added = [e for e in mine if e.get("after") and not e.get("before")]
    return (
        min(removed, key=lambda e: int(e["seq"]), default=None),
        max(added, key=lambda e: int(e["seq"]), default=None),
    )


@dataclass
class Found:
    """One run's measurements. Times are UTC; ``cli_s`` and ``e2e_s`` are the kit's clock."""

    label: str
    app: str
    env: str
    host: str
    run_id: str
    t0: datetime | None = None
    cli_s: float | None = None
    e2e_s: float | None = None
    cut_at: datetime | None = None
    cut_reason: str | None = None
    cut_source: str | None = None
    alive: int | None = None
    latest_at: datetime | None = None
    listener_at: datetime | None = None
    refused: int | None = None
    refused_error: str | None = None
    tunnel: TunnelLine | None = None
    removal: dict[str, Any] | None = None
    readd: dict[str, Any] | None = None
    audit_read: bool = False
    restored: bool | None = None
    problems: list[str] = field(default_factory=list)

    def gap(self, start: datetime | None, end: datetime | None) -> float | None:
        return None if start is None or end is None else (end - start).total_seconds()

    @property
    def compile_s(self) -> float | None:
        return self.gap(self.t0, self.latest_at)

    @property
    def poll_s(self) -> float | None:
        return self.gap(self.latest_at, self.listener_at)

    @property
    def drain_s(self) -> float | None:
        return self.gap(self.listener_at, self.cut_at)

    @property
    def proxy_s(self) -> float | None:
        return self.gap(self.latest_at, self.cut_at)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    result: bool | None
    detail: str

    def line(self) -> str:
        word = {True: "PASS", False: "FAIL", None: "not read"}[self.result]
        return f"{word}: {self.name}: {self.detail}"


def _s(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f} s"


def checks(f: Found) -> list[Check]:
    """The five checks the verdict is made of."""
    reason = f.cut_reason
    cut: bool | None = None if reason is None else reason not in NOT_A_CUT
    if reason is not None and reason.startswith("proxy_"):
        cut = False
    order: bool | None = None
    if f.latest_at and f.listener_at and f.cut_at:
        order = f.latest_at <= f.listener_at <= f.cut_at
    within = None if f.proxy_s is None else f.proxy_s <= LIMIT_S
    refused = None if f.refused is None and f.refused_error else f.refused == REFUSED
    seq = (f.removal or {}).get("seq")
    return [
        Check("open tunnel cut", cut, f"ended {reason or 'not seen'} ({f.cut_source or '-'})"),
        Check(
            "cut by the first listener after the change",
            order,
            f"latest.json {_iso(f.latest_at)}, listener {_iso(f.listener_at)}, "
            f"cut {_iso(f.cut_at)}",
        ),
        Check(f"cut within {LIMIT_S:.0f} s of the snapshot", within, _s(f.proxy_s)),
        Check(
            "new CONNECT refused 403",
            refused,
            f"proxy answered {f.refused if f.refused is not None else f.refused_error}",
        ),
        Check(
            "audit row",
            (f.removal is not None) if f.audit_read else None,
            f"org.updated egress_host seq {seq}",
        ),
    ]


def _iso(value: datetime | None) -> str:
    return "n/a" if value is None else value.isoformat(timespec="milliseconds")


def verdict(found: list[Check]) -> bool | None:
    if any(c.result is False for c in found):
        return False
    return None if any(c.result is None for c in found) else True


def number(f: Found) -> str:
    return (
        f"cut {_s(f.proxy_s)} after the snapshot (poll {_s(f.poll_s)}, drain {_s(f.drain_s)}), "
        f"compile {_s(f.compile_s)}, end to end {_s(f.e2e_s)}, new CONNECT {f.refused}"
    )


def restored_line(f: Found) -> str:
    if f.restored is None:
        return f"host back on the allowlist: not removed ({f.host})"
    if f.restored:
        return f"host back on the allowlist: yes ({f.host})"
    return (
        f"host back on the allowlist: NO ({f.host}). Put it back by hand as an org admin: "
        f"PUT /v1/egress/hosts/{f.host} with body {{}}"
    )


def results_row(f: Found, word: str) -> str:
    """One markdown row for RESULTS.md."""
    removed = (f.removal or {}).get("seq")
    added = (f.readd or {}).get("seq")
    when = _iso(f.t0)
    return (
        f"| GA-6.1 live revoke and drain | {f.label}, `{f.app}` {f.env}, {f.host} | "
        f"**{word}** {when}: latest.json +{_s(f.compile_s)} after the delete, listener "
        f"+{_s(f.poll_s)}, tunnel cut +{_s(f.drain_s)} ({f.cut_reason}); cut {_s(f.proxy_s)} "
        f"after the snapshot (line {LIMIT_S:.0f} s), end to end {_s(f.e2e_s)} (not judged); "
        f"new CONNECT {f.refused}; audit seq {removed} (removed), {added} (re-added); "
        f"host back: {'yes' if f.restored else 'NO' if f.restored is False else 'n/a'} |"
    )


def outcome_of(f: Found) -> Outcome:
    found = checks(f)
    passed = verdict(found)
    word = {True: "PASS", False: "FAIL", None: "INCOMPLETE"}[passed]
    lines = [c.line() for c in found]
    if f.tunnel is not None:
        lines.append(f.tunnel.describe())
    lines += [f"note: {p}" for p in f.problems]
    lines += [
        f"delete answered in {_s(f.cli_s)}; hold run {f.run_id}, {f.alive} keep-alive ticks",
        results_row(f, word),
        restored_line(f),
    ]
    data = {
        "run_id": f.run_id,
        "t0": _iso(f.t0),
        "latest_at": _iso(f.latest_at),
        "listener_at": _iso(f.listener_at),
        "cut_at": _iso(f.cut_at),
        "cut_reason": f.cut_reason,
        "cut_source": f.cut_source,
        "compile_s": f.compile_s,
        "poll_s": f.poll_s,
        "drain_s": f.drain_s,
        "proxy_s": f.proxy_s,
        "e2e_s": f.e2e_s,
        "cli_s": f.cli_s,
        "refused": f.refused,
        "removal_seq": (f.removal or {}).get("seq"),
        "readd_seq": (f.readd or {}).get("seq"),
        "restored": f.restored,
        "problems": f.problems,
    }
    return Outcome(PROOF, number(f), passed, lines, data)


@dataclass
class Api:
    """The control API with the CLI's login; the token stays in the header."""

    send: Send
    url: str
    token: str

    def call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Fetched:
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        return self.send(method, self.url + path, headers, data, 30.0)

    def get(
        self, url: str, headers: Mapping[str, str] | None = None, timeout: float = 90.0
    ) -> Fetched:
        return self.send("GET", url, dict(headers or {}), None, timeout)

    def listed(self) -> list[str]:
        answer = self.call("GET", "/v1/egress")
        if answer.status != 200:
            raise CommandError(f"GET /v1/egress answered {answer.status or answer.error}")
        return [str(h["host"]) for h in json.loads(answer.body).get("hosts", [])]

    def host_path(self, host: str) -> str:
        return "/v1/egress/hosts/" + urllib.parse.quote(host, safe="")

    def audit(self, host: str, since: datetime) -> list[dict[str, Any]]:
        stamp = since.astimezone(UTC).isoformat().replace("+00:00", "Z")
        filters = {
            "action": ACTION,
            "target_kind": TARGET_KIND,
            "target_id": host,
            "since": stamp,
            "limit": 50,
        }
        return t8.search_audit(self.get, self.url, self.token, filters)


def restore(api: Api, f: Found) -> None:
    """Put the host back and read the allowlist to see that it is."""
    try:
        answer = api.call("PUT", api.host_path(f.host), {})
        f.restored = answer.status == 200 and f.host in api.listed()
        if answer.status != 200:
            f.problems.append(f"PUT the host back answered {answer.status or answer.error}")
    except CommandError as exc:
        f.restored = False
        f.problems.append(f"reading the allowlist after the PUT: {exc}")


def read_logs(run: Run, project: str, query: str) -> list[dict[str, Any]]:
    entries = gcloud_json(
        run,
        "logging",
        "read",
        query,
        f"--project={project}",
        "--freshness=1h",
        "--limit=50",
        "--order=asc",
    )
    return list(entries or [])


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def gather(  # noqa: PLR0913, PLR0917  (the evidence's inputs)
    run: Run,
    api: Api,
    f: Found,
    project: str,
    env_id: str,
    hold_asked: datetime,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Cloud Logging and the audit log, read until everything is there or ``LOG_WAIT_S``."""
    assert f.t0 is not None
    service = app_service(env_id)
    listener_q = (
        f'resource.type="gce_instance" AND "{LISTENER_LINE}" AND timestamp>="{_stamp(f.t0)}"'
    )
    tunnel_q = (
        f'resource.type="gce_instance" AND "{f.host}:443" AND "{env_id}" '
        f'AND timestamp>="{_stamp(hold_asked)}"'
    )
    app_q = (
        f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}" '
        f'AND jsonPayload.hold.run="{f.run_id}"'
    )
    deadline = clock() + LOG_WAIT_S
    app_line: dict[str, Any] | None = None
    while True:
        try:
            f.listener_at = first_listener(read_logs(run, project, listener_q), f.t0)
            f.tunnel = tunnel_line(read_logs(run, project, tunnel_q), f.host)
            app_line = app_end(read_logs(run, project, app_q), f.run_id)
            f.removal, f.readd = audit_rows(api.audit(f.host, f.t0 - AUDIT_LOOKBACK), f.host)
            f.audit_read = True
        except CommandError as exc:
            f.problems.append(f"evidence: {exc}")
            return
        complete = f.listener_at and f.tunnel and app_line and f.removal
        if complete or clock() >= deadline:
            break
        sleep(LOG_POLL_S)
    if app_line is None:
        f.problems.append("the app's hold end line is not in Cloud Logging")
    elif f.cut_at is None and app_line.get("at") is not None:
        f.cut_at = datetime.fromtimestamp(float(app_line["at"]), UTC)
        f.cut_reason, f.cut_source = str(app_line.get("reason")), "app log"
        f.alive = _int(app_line.get("alive"))
    if f.listener_at is None:
        f.problems.append("no proxy listener line after the delete in Cloud Logging")
    if f.tunnel is None:
        f.problems.append("no proxy access line for the held tunnel in Cloud Logging")


def measure(
    f: Found,
    watch: HoldWatch,
    started: float,
    egress: Callable[[], tuple[int | None, dict[str, Any]]],
    latest: Callable[[], datetime],
) -> None:
    """After the delete: the hold's end, a new tunnel's answer and ``latest.json``'s time."""
    watch.done.wait(CUT_WAIT_S)
    if watch.end is not None and watch.end_seen is not None:
        f.e2e_s = watch.end_seen - started
        f.cut_at = datetime.fromtimestamp(float(watch.end["at"]), UTC)
        f.cut_reason, f.cut_source = str(watch.end.get("reason")), "stream"
        f.alive = _int(watch.end.get("alive"))
    elif watch.error:
        f.problems.append(watch.error)
    status, after = egress()
    f.refused = after.get("proxy_status")
    f.refused_error = after.get("error") or (None if status == 200 else f"HTTP {status}")
    f.latest_at = latest()


def precheck(
    api: Api, host: str, egress: Callable[[], tuple[int | None, dict[str, Any]]]
) -> str | None:
    """Why the kit must not change anything, or None: the host is listed and tunnels now."""
    if host not in api.listed():
        return "the host is not on the allowlist"
    status, before = egress()
    if status != 200 or before.get("proxy_status") != TUNNELLED:
        return (
            f"the host does not tunnel before the change (/egress HTTP {status}, "
            f"proxy {before.get('proxy_status')}, {before.get('error')})"
        )
    return None


def _get(send: Send) -> Http:
    def get(url: str, headers: Mapping[str, str] | None = None, timeout: float = 90.0) -> Fetched:
        return send("GET", url, dict(headers or {}), None, timeout)

    return get


def run(  # noqa: PLR0913, PLR0915, PLR0917  (the kit's proof signature plus every seam)
    args: argparse.Namespace,
    run: Run = run_command,
    send: Send = request,
    stream: Stream = open_stream,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id: Callable[[], str] = lambda: secrets.token_hex(4),
) -> Outcome:
    project = args.project or f"ssc-c-{args.label}"
    fence(args.app, args.label, project, args.host)
    if not 5 <= args.hold_seconds <= 150:
        raise CommandError("--hold-seconds is 5 to 150")
    env = app_environment(run, args.app, args.env)
    base, env_id = str(env["url"]).rstrip("/"), str(env["id"])
    org_id = str(ssc_json(run, "whoami")["org_id"])
    if args.org and args.org != org_id:
        raise CommandError(f"--org {args.org} is not the CLI login's org {org_id}")
    api_url, token = t8.control_api(run)
    api = Api(send, api_url.rstrip("/"), token)
    http = _get(send)
    headers = session_headers(CookieJar().get(host_of(base)))
    f = Found(args.label, args.app, args.env, args.host, run_id())

    stop = precheck(api, args.host, lambda: t6.egress_call(base, args.host, True, http))
    if stop is not None:
        return Outcome(PROOF, stop, None, [restored_line(f)])

    query = urllib.parse.urlencode(
        {"host": args.host, "seconds": args.hold_seconds, "run": f.run_id}
    )
    watch = HoldWatch()
    hold_asked = wall()
    threading.Thread(
        target=watch_hold,
        args=(stream, f"{base}/hold?{query}", headers, watch, clock),
        daemon=True,
    ).start()
    watch.ready.wait(READY_WAIT_S)
    if not watch.up:
        why = watch.error or f"end {(watch.end or {}).get('reason')}, {watch.answered} ticks"
        return Outcome(PROOF, "the hold did not come up", None, [why, restored_line(f)])

    removed, finished = False, False
    try:
        f.t0 = wall()
        started = clock()
        answer = api.call("DELETE", api.host_path(args.host))
        f.cli_s = clock() - started
        removed = answer.status == 200 or args.host not in api.listed()
        if answer.status != 200:
            f.problems.append(
                f"DELETE answered {answer.status or answer.error}; "
                + ("the host is off the list all the same" if removed else "nothing removed")
            )
        if removed:
            measure(
                f,
                watch,
                started,
                lambda: t6.egress_call(base, args.host, True, http),
                lambda: t8.latest_changed(run, args.label, org_id),
            )
        finished = True
    except CommandError as exc:
        f.problems.append(str(exc))
        finished = True
    finally:
        if removed:
            restore(api, f)
            if not finished:
                print(restored_line(f))
    if not removed:
        return Outcome(PROOF, "the host was not removed", None, [*f.problems, restored_line(f)])
    gather(run, api, f, project, env_id, hold_asked, sleep, clock)
    outcome = outcome_of(f)
    if f.restored is False:
        emit(outcome)
        raise SystemExit(1)
    return outcome
