"""GA-4.2: file storage live, through the cell's file broker and Cloud Storage.

``files --app <files fixture app> --label <cell label> [--env preview] [--wait-expiry] [--disable]``

The app is ``apps/files`` (needs ``[files]`` and a cell with ``connections`` on). The kit reaches
it through its public host with the session cookie, and Cloud Storage from the laptop with the
signed link alone.

1. put: ``POST /files/put`` stores 4 KiB of random bytes under ``ga42/<time>.bin``.
2. get by link: the app's ``get`` link works from the laptop (200, same bytes, an attachment
   disposition, signed by ``ssc-data@ssc-c-<label>``); ``expires_at`` is printed.
3. prefix isolation: the same link with the environment id in its path swapped for the other
   environment's is refused (403 ``SignatureDoesNotMatch``).
4. expiry (``--wait-expiry``, about 11 minutes): 30 s after the link's ``X-Goog-Date`` plus
   ``X-Goog-Expires`` it answers 400 ``ExpiredToken``.
5. kill switch (``--disable``) **[real]**: runs ``ssc disable <app>``. While the app's
   ``/files/loop`` asks for a ``put`` link every second, the broker must answer
   ``APP_NOT_ACTIVE`` (5a, from the stream, else from ``ssc logs``). 5b is the disclosed fact: a
   link handed out before the disable still answers 200. ``ssc enable <app>`` always runs
   afterwards; 5c is a new put after it.
6. clean-up: ``POST /files/delete``.

Nothing is saved that could replay a link: the results hold the link's path, credential, date and
lifetime, never its signature, and never the operator token.
"""

import argparse
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from proofrun.common import (
    BODY_LIMIT,
    REPO,
    USER_AGENT,
    CommandError,
    CookieJar,
    Done,
    Fetched,
    Outcome,
    Run,
    fence,
    host_of,
    last_line,
    parse_time,
    results_dir,
    run_command,
    session_headers,
    ssc_prefix,
)
from proofrun.t8 import control_api

ENVIRONMENTS: Final = ("preview", "prod")
PAYLOAD_BYTES: Final = 4096
LOOP_SECONDS: Final = 90
LOOP_WAIT_S: Final = 100.0
HOLD_S: Final = 20.0
EXPIRY_GRACE_S: Final = 30.0
LOGS_READS: Final = 3
LOGS_GAP_S: Final = 15.0
PUT_TRIES: Final = 5
PUT_GAP_S: Final = 5.0
LOG_SKEW_S: Final = 5.0
NOT_ACTIVE: Final = "APP_NOT_ACTIVE"
PASSED: Final = "PASS"
FAIL: Final = "FAIL"
NOT_RUN: Final = "not run"
INCOMPLETE: Final = "INCOMPLETE"
_LOOP: Final = re.compile(r"LOOP at=(\S+) result=(\S+)")
_GOOG_DATE: Final = "%Y%m%dT%H%M%SZ"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app", required=True, help="the files fixture app's slug")
    parser.add_argument("--label", required=True, help="the cell's label")
    parser.add_argument("--env", choices=ENVIRONMENTS, default="preview")
    parser.add_argument(
        "--wait-expiry", action="store_true", help="also wait out the link's 10 minutes (check 4)"
    )
    parser.add_argument(
        "--disable", action="store_true", help="also run the kill switch drill (check 5) [real]"
    )


class Send(Protocol):
    """Sends one request; the real one is :func:`request`, tests pass a fake."""

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Fetched: ...


class Stream(Protocol):
    """Reads a GET answer line by line as it arrives; the real one is :func:`open_stream`."""

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> Iterator[str]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


_OPENER: Final = urllib.request.build_opener(_NoRedirect)


def request(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
) -> Fetched:
    """One request without following redirects, timing the first byte of the answer."""
    fence(url)
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))  # noqa: S310
    started = time.monotonic()
    try:
        with _OPENER.open(req, timeout=timeout) as response:
            first = time.monotonic() - started
            data = response.read(BODY_LIMIT)
            return Fetched(response.status, first, None, data, dict(response.headers.items()))
    except urllib.error.HTTPError as exc:
        first = time.monotonic() - started
        data = exc.read(BODY_LIMIT) if exc.fp else b""
        return Fetched(exc.code, first, None, data, dict(exc.headers.items()))
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return Fetched(None, time.monotonic() - started, f"{type(reason).__name__}: {reason}")


def open_stream(url: str, headers: Mapping[str, str], timeout: float) -> Iterator[str]:
    """The lines of a GET answer as they arrive; raises ``OSError`` when it is not a 200."""
    fence(url)
    req = urllib.request.Request(url, headers=dict(headers))  # noqa: S310
    try:
        with _OPENER.open(req, timeout=timeout) as response:
            for raw in response:
                yield raw.decode("utf-8", "replace").rstrip("\r\n")
    except urllib.error.HTTPError as exc:
        raise OSError(f"HTTP {exc.code}") from None


@dataclass(frozen=True, slots=True)
class LoopLine:
    """One ``LOOP at=<time> result=<ok|CODE>`` line of the app's loop."""

    at: datetime
    result: str


@dataclass(frozen=True, slots=True)
class Check:
    number: str
    name: str
    status: str
    detail: str = ""

    def line(self) -> str:
        return f"{self.number} {self.name}: {self.status}" + (
            f" ({self.detail})" if self.detail else ""
        )


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}


def swap_env(url: str, this_env: str, other_env: str) -> str:
    """``url`` with the ``files/<this_env>/`` segment of its path changed to the other
    environment's; the query stays as it was."""
    parts = urllib.parse.urlsplit(url)
    needle = f"/files/{this_env}/"
    if needle not in parts.path:
        raise ValueError(f"the link's path has no {needle}")
    path = parts.path.replace(needle, f"/files/{other_env}/", 1)
    return urllib.parse.urlunsplit(parts._replace(path=path))


def credential_ok(url: str, label: str) -> bool:
    """Whether the link was signed by the cell's data account, ``ssc-data@ssc-c-<label>``."""
    credential = _query(url).get("X-Goog-Credential", "")
    return credential.startswith(f"ssc-data@ssc-c-{label}.iam.gserviceaccount.com")


def expiry_epoch(url: str) -> float:
    """When the link expires: ``X-Goog-Date`` plus ``X-Goog-Expires`` seconds, as a Unix time."""
    query = _query(url)
    try:
        start = datetime.strptime(query["X-Goog-Date"], _GOOG_DATE).replace(tzinfo=UTC)
        return start.timestamp() + int(query["X-Goog-Expires"])
    except (KeyError, ValueError) as exc:
        raise ValueError("the link has no usable X-Goog-Date and X-Goog-Expires") from exc


def strip_signature(url: str) -> str:
    """``url`` without its ``X-Goog-Signature`` pair."""
    parts = urllib.parse.urlsplit(url)
    kept = [p for p in parts.query.split("&") if not p.startswith("X-Goog-Signature=")]
    return urllib.parse.urlunsplit(parts._replace(query="&".join(kept)))


def link_meta(url: str) -> dict[str, str | None]:
    """What may be saved of a link: its path, credential, date and lifetime."""
    query = _query(url)
    return {
        "path": urllib.parse.urlsplit(url).path,
        "credential": query.get("X-Goog-Credential"),
        "date": query.get("X-Goog-Date"),
        "expires": query.get("X-Goog-Expires"),
    }


def parse_loop(line: str) -> LoopLine | None:
    """The ``LOOP`` line in ``line`` (a stream line or a log line's text), or None."""
    m = _LOOP.search(line)
    if m is None:
        return None
    try:
        return LoopLine(parse_time(m[1]), m[2])
    except ValueError:
        return None


def first_not_active(lines: list[LoopLine]) -> LoopLine | None:
    """The earliest line where the broker refused with ``APP_NOT_ACTIVE``."""
    refused = [line for line in lines if line.result == NOT_ACTIVE]
    return min(refused, key=lambda line: line.at) if refused else None


def log_loop_lines(page: Mapping[str, Any], since: datetime | None = None) -> list[LoopLine]:
    """The loop lines in one ``ssc logs --json`` page, from ``since`` on."""
    found = []
    for row in page.get("lines", []):
        parsed = parse_loop(str(row.get("text", "")))
        if parsed is not None and (since is None or parsed.at >= since):
            found.append(parsed)
    return found


def header(headers: Mapping[str, str], name: str) -> str:
    """A response header by name, whatever its case."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def _json(answer: Fetched) -> dict[str, Any]:
    try:
        decoded = json.loads(answer.body)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class Loop:
    """What the loop stream delivered, filled by a thread."""

    lines: list[str] = field(default_factory=list)
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def parsed(self) -> list[LoopLine]:
        with self.lock:
            return [p for p in map(parse_loop, self.lines) if p is not None]


def follow_loop(stream: Stream, url: str, headers: Mapping[str, str], loop: Loop) -> None:
    try:
        for line in stream(url, headers, 30.0):
            with loop.lock:
                loop.lines.append(line)
    except Exception as exc:  # noqa: BLE001  (any end of the stream is the end of the loop)
        with loop.lock:
            loop.error = f"{type(exc).__name__}: {exc}"[:200]


@dataclass
class Drill:
    """Check 5's findings."""

    checks: list[Check] = field(default_factory=list)
    loop_lines: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    timings: dict[str, float | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class World:
    """What every check shares."""

    args: argparse.Namespace
    base: str
    headers: Mapping[str, str]
    this_env: str
    other_env: str
    http: Send
    name: str
    payload: bytes

    def app(
        self, method: str, path: str, body: bytes | None = None, timeout: float = 120.0
    ) -> Fetched:
        headers = dict(self.headers)
        if body is not None:
            headers["Content-Type"] = "application/octet-stream"
        return self.http(method, self.base + path, headers, body, timeout)

    def file_path(self, route: str, **extra: str) -> str:
        query = urllib.parse.urlencode({**extra, "name": self.name}, safe="/")
        return f"{route}?{query}"

    def put(self) -> dict[str, Any]:
        return _json(self.app("POST", self.file_path("/files/put"), self.payload))

    def link(self, op: str) -> dict[str, Any]:
        return _json(self.app("GET", self.file_path("/files/link", op=op)))


def resolve_environments(
    world_api: str, headers: Mapping[str, str], http: Send, args: Any
) -> tuple[str, str, str]:
    """The app's two environment ids and this environment's URL, from the control plane."""
    apps = http("GET", f"{world_api}/v1/apps", headers, None, 30.0)
    found = [a for a in _json(apps).get("apps", []) if a.get("slug") == args.app]
    if not found:
        raise CommandError(f"no app with slug {args.app}: check `ssc apps`")
    one = http("GET", f"{world_api}/v1/apps/{found[0]['id']}", headers, None, 30.0)
    environments = {e["name"]: e for e in _json(one).get("environments", [])}
    missing = [n for n in ENVIRONMENTS if n not in environments]
    if missing:
        raise CommandError(f"{args.app} has no {' or '.join(missing)} environment")
    this = environments[args.env]
    if not this.get("url"):
        raise CommandError(f"{args.app} {args.env} has no URL yet: deploy it first")
    other = environments[next(n for n in ENVIRONMENTS if n != args.env)]
    return this["id"], other["id"], str(this["url"])


def check_put(world: World) -> Check:
    answer = world.put()
    ok = answer.get("ok") is True and answer.get("size") == len(world.payload)
    detail = f"size {answer.get('size')}" if ok else f"answer {answer.get('code') or answer}"
    return Check("1", "put", PASSED if ok else FAIL, detail)


def check_get(world: World, label: str) -> tuple[Check, str | None, str]:
    """Check 2; also the link's URL (kept in memory only) and its ``expires_at``."""
    link = world.link("get")
    url = link.get("url")
    if not isinstance(url, str):
        return Check("2", "get by link", FAIL, f"no link: {link.get('code') or link}"), None, ""
    got = world.http("GET", url, {"User-Agent": USER_AGENT}, None, 60.0)
    disposition = header(got.headers, "Content-Disposition")
    problems = []
    if got.status != 200:
        problems.append(f"status {got.status or got.error}")
    if got.body != world.payload:
        problems.append("body differs")
    if not disposition.lower().startswith("attachment"):
        problems.append(f"disposition {disposition!r}")
    if not credential_ok(url, label):
        problems.append("credential is not ssc-data of this cell")
    expires_at = str(link.get("expires_at", ""))
    detail = f"expires_at {expires_at}" if not problems else "; ".join(problems)
    return Check("2", "get by link", FAIL if problems else PASSED, detail), url, expires_at


def check_prefix(world: World, url: str | None) -> Check:
    if url is None:
        return Check("3", "prefix isolation", FAIL, "no link to edit")
    edited = swap_env(url, world.this_env, world.other_env)
    got = world.http("GET", edited, {"User-Agent": USER_AGENT}, None, 60.0)
    ok = got.status == 403 and b"SignatureDoesNotMatch" in got.body
    return Check(
        "3", "prefix isolation", PASSED if ok else FAIL, f"status {got.status or got.error}"
    )


def check_expiry(
    world: World, url: str | None, sleep: Callable[[float], None], clock: Callable[[], float]
) -> Check:
    if url is None:
        return Check("4", "expiry", FAIL, "no link to wait on")
    wait = max(0.0, expiry_epoch(url) + EXPIRY_GRACE_S - clock())
    print(f"waiting {wait:.0f} s for the link to expire", flush=True)
    sleep(wait)
    got = world.http("GET", url, {"User-Agent": USER_AGENT}, None, 60.0)
    ok = got.status == 400 and b"ExpiredToken" in got.body
    return Check("4", "expiry", PASSED if ok else FAIL, f"waited {wait:.0f} s, status {got.status}")


def read_logs(run: Run, world: World, since: datetime, drill: Drill, sleep: Callable) -> None:
    """Up to three reads of the app's log, 15 s apart, until a refused loop line shows."""
    argv = [
        *ssc_prefix(),
        "logs",
        world.args.app,
        "--env",
        world.args.env,
        "--source",
        "app",
        "--since",
        "10m",
        "--json",
    ]
    for attempt in range(LOGS_READS):
        if attempt:
            sleep(LOGS_GAP_S)
        done = run(argv, cwd=REPO)
        if done.returncode != 0:
            drill.notes.append(f"ssc logs failed: {last_line(done.stderr or done.stdout)}")
            continue
        try:
            found = log_loop_lines(json.loads(done.stdout), since)
        except ValueError:
            drill.notes.append("ssc logs did not print JSON")
            continue
        drill.log_lines = [
            f"LOOP at={x.at.strftime('%Y-%m-%dT%H:%M:%SZ')} result={x.result}" for x in found
        ]
        if first_not_active(found) is not None:
            return


def kill_drill(  # noqa: PLR0913  (keyword-only)
    *,
    world: World,
    run: Run,
    stream: Stream,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    out: list[str],
) -> Drill:
    """Check 5. ``ssc enable`` runs whatever happens after ``ssc disable`` was started."""
    drill = Drill()
    slug = world.args.app
    l0 = world.link("get").get("url")
    if not isinstance(l0, str):
        l0 = None
    loop = Loop()
    url = world.base + world.file_path("/files/loop", seconds=str(LOOP_SECONDS))
    thread = threading.Thread(
        target=follow_loop, args=(stream, url, world.headers, loop), daemon=True
    )
    thread.start()
    started = clock()
    disabled = Done(1, "", "ssc disable did not run")
    after: Fetched | None = None
    try:
        disabled = run([*ssc_prefix(), "disable", slug, "--json"], cwd=REPO)
        drill.timings["disable_s"] = clock() - started
        # The gateway cuts the stream a few seconds in; the app's loop goes on logging, so hold
        # at least HOLD_S after the disable before enabling, else wait for the stream to end.
        while disabled.returncode == 0 and clock() < started + LOOP_WAIT_S:
            if not thread.is_alive() and clock() >= started + HOLD_S:
                break
            sleep(1.0)
        if l0 is not None:
            after = world.http("GET", l0, {"User-Agent": USER_AGENT}, None, 60.0)
    finally:
        enable_started = clock()
        enabled = run([*ssc_prefix(), "enable", slug, "--json"], cwd=REPO)
        drill.timings["enable_s"] = clock() - enable_started
        if enabled.returncode != 0:
            out.append(f"ssc enable failed: {last_line(enabled.stderr or enabled.stdout)}")
            out.append(f"undo: ssc enable {slug}")
    since = datetime.fromtimestamp(started, UTC)
    seen = loop.parsed()
    refused = first_not_active(seen)
    if refused is None and enabled.returncode == 0:
        read_logs(run, world, datetime.fromtimestamp(started - LOG_SKEW_S, UTC), drill, sleep)
        refused = first_not_active([x for x in map(parse_loop, drill.log_lines) if x is not None])
    drill.loop_lines = list(loop.lines)
    lines_seen = bool(seen or drill.log_lines)
    drill.checks.append(judge_refusal(disabled, refused, since, lines_seen, drill))
    old = after.status if after is not None else None
    drill.checks.append(
        Check(
            "5b",
            "link handed out before still works (disclosed)",
            PASSED if old == 200 and disabled.returncode == 0 else FAIL,
            f"existing link answered {old} after the disable; links live up to 10 minutes"
            if disabled.returncode == 0
            else "the disable did not run",
        )
    )
    drill.checks.append(re_put(world, sleep, enabled.returncode == 0))
    return drill


def judge_refusal(
    disabled: Done, refused: LoopLine | None, since: datetime, lines_seen: bool, drill: Drill
) -> Check:
    """Check 5a from what the stream and the logs showed."""
    name = "broker refuses new links"
    if disabled.returncode != 0:
        return Check(
            "5a", name, FAIL, f"ssc disable failed: {last_line(disabled.stderr or disabled.stdout)}"
        )
    if refused is not None:
        gap = (refused.at - since).total_seconds()
        drill.timings["refused_after_s"] = gap
        return Check(
            "5a",
            name,
            PASSED,
            f"first {NOT_ACTIVE} at {refused.at:%H:%M:%S}Z, disable started {since:%H:%M:%S}Z, "
            f"{gap:.1f} s later (includes clock skew)",
        )
    if lines_seen:
        return Check("5a", name, FAIL, "the loop saw links but no refusal")
    return Check("5a", name, INCOMPLETE, "no loop line in stream or logs")


def re_put(world: World, sleep: Callable[[float], None], enabled: bool) -> Check:
    """Check 5c: a new put after enable, tried up to five times while the snapshot catches up."""
    if not enabled:
        return Check("5c", "put after enable", FAIL, "ssc enable failed")
    code: object = None
    for tries in range(1, PUT_TRIES + 1):
        answer = world.put()
        if answer.get("ok") is True:
            return Check("5c", "put after enable", PASSED, f"ok on try {tries}")
        code = answer.get("code") or "no answer"
        if tries < PUT_TRIES:
            sleep(PUT_GAP_S)
    return Check("5c", "put after enable", FAIL, f"{PUT_TRIES} tries, last {code}")


def check_delete(world: World) -> Check:
    got = world.app("POST", world.file_path("/files/delete"))
    ok = _json(got).get("ok") is True
    return Check("6", "clean-up", PASSED if ok else FAIL, f"status {got.status or got.error}")


def verdict(checks: list[Check]) -> bool | None:
    """PASS when every check that ran passed; FAIL on any FAIL; None when one was unreadable."""
    statuses = [c.status for c in checks]
    if FAIL in statuses:
        return False
    return None if INCOMPLETE in statuses else True


def run(  # noqa: PLR0913, PLR0917  (every seam is injectable)
    args: argparse.Namespace,
    run: Run = run_command,
    http: Send = request,
    stream: Stream = open_stream,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> Outcome:
    api, token = control_api(run)
    api_headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    this_env, other_env, url = resolve_environments(api, api_headers, http, args)
    base = url.rstrip("/")
    started = clock()
    world = World(
        args=args,
        base=base,
        headers=session_headers(CookieJar().get(host_of(base))),
        this_env=this_env,
        other_env=other_env,
        http=http,
        name=f"ga42/{_stamp(started)}.bin",
        payload=os.urandom(PAYLOAD_BYTES),
    )
    out: list[str] = []
    checks = [check_put(world)]
    second, link_url, expires_at = check_get(world, args.label)
    checks += [second, check_prefix(world, link_url)]
    meta = link_meta(link_url) if link_url else {}
    drill = Drill()
    if args.wait_expiry:
        checks.append(check_expiry(world, link_url, sleep, clock))
    else:
        checks.append(Check("4", "expiry", NOT_RUN, "pass --wait-expiry"))
    if args.disable:
        drill = kill_drill(world=world, run=run, stream=stream, sleep=sleep, clock=clock, out=out)
        checks += drill.checks
    else:
        checks.append(Check("5", "kill switch", NOT_RUN, "pass --disable"))
    checks.append(check_delete(world))
    passed = verdict(checks)
    lines = [c.line() for c in checks] + out
    if expires_at and not any("expires_at" in line for line in lines):
        lines.append(f"expires_at {expires_at}")
    lines += [f"note: {n}" for n in drill.notes]
    data = {
        "app": args.app,
        "env": args.env,
        "name": world.name,
        "checks": [asdict(c) for c in checks],
        "link": meta,
        "loop_lines": drill.loop_lines,
        "log_lines": drill.log_lines,
        "timings": drill.timings,
    }
    saved = results_dir()
    saved.mkdir(parents=True, exist_ok=True)
    (saved / f"files-{args.app}-{_stamp(started)}.json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n"
    )
    number = ", ".join(f"{c.number} {c.status}" for c in checks)
    return Outcome("GA-4.2", number, passed, lines, data)
