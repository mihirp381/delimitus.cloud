"""GA-4.8: the warm option (SSC-092) live, the environment part: flag on, one reconciler pass, no
waking page; flag off returns to zero.

``warm --app <slug> --label <cell label> [--project ssc-c-<label>] [--idle-minutes 20]
[--settle-seconds 300]``

The app is ``apps/warm`` (runtime only), live on prod and shared with the person whose session
cookie for the prod host is in the jar. The control API is read and set with the CLI's login (an
org admin, never an agent session). Every ``PUT /v1/warm`` sends ``gateway: false``: the gateway
part runs the cell deployer, which is not run here.

1. ``GET /v1/warm``: nothing warm (no environment, ``monthly_usd`` 0, gateway not wanted and
   ``off``), the fixture's prod environment listed, its service at minimum 0. Else the kit stops
   before any change.
2. Two refusals, each ``422 VALIDATION_FAILED``: the cost off by one, and the preview environment.
   The setting is unchanged after.
3. ``PUT`` the prod environment at its cost: warm, ``monthly_usd`` the cost, gateway ``off``.
4. One reconciler pass: the service's minimum (``run.googleapis.com/minScale``, the v1 view of
   the ``scaling.minInstanceCount`` the cell agent writes) reaches 1 and the service is ready,
   within ``--settle-seconds``, on the same serving revision. Then 30 s, and three ``/health``
   reads 5 s apart record the kept instance.
5. The ``org.updated`` row on ``warm`` with the environment and the cost shown.
6. ``--idle-minutes`` with no request to the app, one GET to the cell's ``www`` host (the
   gateway's own answer, so its cold start is not counted), then one browser page load of ``/``:
   the fixture page, not the waking page, from a kept instance, first byte under 2 s.
7. ``PUT`` nothing warm (cost 0); the service's minimum back to 0.
8. The ``org.updated`` row for the change off.
9. The same hold and page load: the eventual app answer (after the waking page's own retries,
   if it showed) comes from a new process.
10. Manual: the console's monthly add, ``results/ga-4.8-console.png``; never in the verdict.

Pass: no check failed and none is "not read". Whatever happens after the change on, warm is set
off again before the command ends (also on Ctrl-C), and the final line says whether it is off.
"""

import argparse
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from proofrun import cloudrun, t8
from proofrun.common import (
    COOKIE_NAME,
    USER_AGENT,
    CommandError,
    Cookie,
    CookieError,
    CookieJar,
    Fetched,
    Http,
    Outcome,
    Run,
    app_environment,
    app_service,
    fence,
    fetch,
    host_of,
    results_dir,
    run_command,
    session_headers,
    www_host,
)
from proofrun.files import Send, request

PROOF: Final = "GA-4.8"
MIN_IDLE_MINUTES: Final = 16
"""Cloud Run takes an idle request-billed instance away after about 15 minutes."""
POLL_S: Final = 5.0
SETTLE_AFTER_S: Final = 30.0
HEALTH_READS: Final = 3
HEALTH_GAP_S: Final = 5.0
WAKE_LIMIT_S: Final = 2.0
"""The wake route's per-try timeout (``ssc_edge.envoy.WAKE_SECONDS``)."""
WAKE_RETRY_S: Final = 2.0
"""The waking page's own refresh (``ssc_edge.pages.WAKE_RETRY_SECONDS``)."""
RETRY_LIMIT_S: Final = 60.0
SINCE_MARGIN_S: Final = 120
ACTION: Final = "org.updated"
TARGET_KIND: Final = "warm"
WAKING_MARKER: Final = "<title>Waking up</title>"
"""In ``ssc_edge.pages.WAKING``, the gateway's 503 when the app did not answer in 2 s."""
WAKE_COOKIE: Final = "__Host-ssc-wake"
PAGE_TITLE: Final = "SSC proof run: warm"
PAGE_ACCEPT: Final = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
SCREENSHOT: Final = "ga-4.8-console.png"
_META: Final = re.compile(r"<meta name=ssc-(started-at|pid) content=([^>\s]+)>")
NAMES: Final = {
    1: "nothing warm before; the prod environment listed; gateway off; service at minimum 0",
    2: "refusals: cost off by one and the preview environment, 422 VALIDATION_FAILED, no change",
    3: "PUT the prod environment warm: 200, warm, monthly_usd is the cost, gateway off",
    4: "one reconciler pass: service minimum 1 and ready, the same serving revision",
    5: "audit: org.updated on warm with the environment and the cost shown",
    6: "after the idle hold a page load gets the kept instance, no waking page, under 2 s",
    7: "PUT nothing warm: 200, monthly_usd 0; service minimum back to 0",
    8: "audit: org.updated on warm for the change off",
    9: "after the idle hold the app answers from a new process (returned to zero)",
}


def idle_minutes(value: str) -> int:
    minutes = int(value)
    if minutes < MIN_IDLE_MINUTES:
        raise argparse.ArgumentTypeError(
            f"at least {MIN_IDLE_MINUTES}: Cloud Run keeps an idle instance about 15 minutes"
        )
    return minutes


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app", required=True, help="the warm fixture app's slug (ga4warm)")
    parser.add_argument("--label", required=True, help="the cell's label")
    parser.add_argument("--project", default=None, help="the cell's project (ssc-c-<label>)")
    parser.add_argument("--idle-minutes", type=idle_minutes, default=20)
    parser.add_argument("--settle-seconds", type=int, default=300)


@dataclass(frozen=True, slots=True)
class Check:
    """One numbered check; ``result`` is None when it could not be read."""

    n: int
    result: bool | None
    detail: str

    @property
    def word(self) -> str:
        return {True: "PASS", False: "FAIL", None: "not read"}[self.result]

    def line(self) -> str:
        return f"check {self.n} {self.word}: {NAMES[self.n]} ({self.detail})"

    def data(self) -> dict[str, Any]:
        return {"n": self.n, "name": NAMES[self.n], "result": self.word, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Identity:
    """Which process answered: ``started_at`` and ``pid`` as the fixture reports them."""

    started_at: str | None
    pid: str | None

    def same(self, other: Identity) -> bool:
        return self.started_at is not None and self.started_at == other.started_at

    def describe(self) -> str:
        return f"started_at {self.started_at}, pid {self.pid}"


@dataclass(frozen=True, slots=True)
class Observed:
    """The app's Cloud Run service as one read saw it."""

    service_min: int
    template_min: int
    revision: str | None
    ready: bool

    def describe(self) -> str:
        return (
            f"service min {self.service_min} (template {self.template_min}), "
            f"serving {self.revision}, ready {self.ready}"
        )


@dataclass
class ControlApi:
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

    def get_warm(self) -> dict[str, Any]:
        answer = self.call("GET", "/v1/warm")
        if answer.status != 200:
            raise CommandError(f"GET /v1/warm answered {answer.status or answer.error}")
        return json.loads(answer.body)

    def put_warm(self, environment_ids: Sequence[str], monthly_usd_shown: int) -> Fetched:
        """The gateway part is never asked for: ``gateway`` is always false."""
        body = {
            "environment_ids": list(environment_ids),
            "gateway": False,
            "monthly_usd_shown": monthly_usd_shown,
        }
        return self.call("PUT", "/v1/warm", body)

    def get(
        self, url: str, headers: Mapping[str, str] | None = None, timeout: float = 90.0
    ) -> Fetched:
        return self.send("GET", url, dict(headers or {}), None, timeout)

    def audit(self, since: datetime) -> list[dict[str, Any]]:
        stamp = since.astimezone(UTC).isoformat().replace("+00:00", "Z")
        filters = {"action": ACTION, "target_kind": TARGET_KIND, "since": stamp, "limit": 50}
        return t8.search_audit(self.get, self.url, self.token, filters)


@dataclass
class World:
    """One run's seams, settings and what it found so far."""

    args: argparse.Namespace
    run: Run
    api: ControlApi
    http: Http
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    wall: Callable[[], datetime]
    project: str
    env_id: str
    preview_id: str
    base: str
    www: str
    cookie: Cookie
    t0: float = 0.0
    cost: int = 0
    warm_by_kit: bool = False
    left_off: bool | None = True
    before: Observed | None = None
    kept: list[Identity] = field(default_factory=list)
    warm_seen: Identity | None = None
    t_on: float = 0.0
    wall_on: datetime | None = None
    t_off: float = 0.0
    wall_off: datetime | None = None
    on_seq: int = 0
    checks: list[Check] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def service(self) -> str:
        return app_service(self.env_id)

    def say(self, text: str) -> None:
        print(text, flush=True)

    def mark(self, event: str, **fields: Any) -> None:
        entry = {"t_s": round(self.clock() - self.t0, 3), "at": self.wall().isoformat()}
        self.timeline.append(entry | {"event": event} | fields)


# ── reading answers ──────────────────────────────────────────────────────────


def _json(answer: Fetched) -> dict[str, Any]:
    try:
        doc = json.loads(answer.body)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def problem_code(answer: Fetched) -> str | None:
    return _json(answer).get("code")


def env_entry(doc: Mapping[str, Any], env_id: str) -> Mapping[str, Any] | None:
    return next((e for e in doc.get("environments", []) if e.get("environment_id") == env_id), None)


def warm_of(doc: Mapping[str, Any], env_id: str) -> bool | None:
    entry = env_entry(doc, env_id)
    return None if entry is None else bool(entry.get("warm"))


def gateway_of(doc: Mapping[str, Any]) -> tuple[bool | None, str | None]:
    gateway = doc.get("gateway") or {}
    return gateway.get("warm"), gateway.get("state")


def page_headers(cookie: Cookie, *, wake: bool = False) -> dict[str, str]:
    """A browser loading a page into a tab, as ``ssc_edge.gate.page_load`` reads it: GET,
    ``Sec-Fetch-Mode: navigate``, ``Sec-Fetch-Dest: document``, ``text/html`` in ``Accept``, no
    ``Upgrade``. Without the wake cookie the gateway sends it down the wake route."""
    value = f"{COOKIE_NAME}={cookie.value}" + (f"; {WAKE_COOKIE}=1" if wake else "")
    return {
        "Cookie": value,
        "Accept": PAGE_ACCEPT,
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "User-Agent": USER_AGENT,
    }


def is_waking(answer: Fetched) -> bool:
    return answer.status == 503 and WAKING_MARKER.encode() in answer.body


def is_app_page(answer: Fetched) -> bool:
    return answer.status == 200 and PAGE_TITLE.encode() in answer.body


def wake_cookie_set(answer: Fetched) -> bool:
    return any(k.lower() == "set-cookie" and WAKE_COOKIE in v for k, v in answer.headers.items())


def page_identity(answer: Fetched) -> Identity | None:
    if not is_app_page(answer):
        return None
    found = dict(_META.findall(answer.body.decode(errors="replace")))
    return Identity(found.get("started-at"), found.get("pid"))


def health_identity(answer: Fetched) -> Identity | None:
    doc = _json(answer) if answer.status == 200 else {}
    if not doc.get("started_at"):
        return None
    return Identity(str(doc["started_at"]), str(doc.get("pid")))


def serving_revision(doc: Mapping[str, Any]) -> str | None:
    status = doc.get("status", {})
    routed = [t for t in status.get("traffic", []) if t.get("revisionName") and t.get("percent")]
    if routed:
        return str(max(routed, key=lambda t: int(t["percent"]))["revisionName"])
    return status.get("latestReadyRevisionName")


def service_ready(doc: Mapping[str, Any]) -> bool:
    """Cloud Run has acted on the latest change and says the service is ready."""
    status = doc.get("status", {})
    generation = doc.get("metadata", {}).get("generation")
    caught_up = generation is not None and status.get("observedGeneration") == generation
    conditions = status.get("conditions", [])
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
    return caught_up and ready


def observe(world: World) -> Observed:
    doc = cloudrun.describe(world.run, world.project, world.service)
    settings = cloudrun.settings(doc)
    return Observed(
        service_min=settings.service_min_instances,
        template_min=settings.min_instances,
        revision=serving_revision(doc),
        ready=service_ready(doc),
    )


def _s(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} s"


# ── checks ───────────────────────────────────────────────────────────────────


def check_initial(world: World) -> Check:
    doc = world.api.get_warm()
    world.cost = int(doc.get("environment_monthly_usd") or 0)
    warm_envs = [e.get("app_slug") for e in doc.get("environments", []) if e.get("warm")]
    gw_warm, gw_state = gateway_of(doc)
    world.before = observe(world)
    world.data.update(monthly_usd_before=doc.get("monthly_usd"), environment_monthly_usd=world.cost)
    world.mark("warm read", monthly_usd=doc.get("monthly_usd"), cost=world.cost)
    ok = (
        warm_of(doc, world.env_id) is False
        and not warm_envs
        and doc.get("monthly_usd") == 0
        and gw_warm is False
        and gw_state == "off"
        and world.cost > 0
        and world.before.service_min == 0
    )
    detail = (
        f"prod listed {warm_of(doc, world.env_id) is not None}, warm now {warm_envs or 'none'}, "
        f"monthly_usd {doc.get('monthly_usd')}, environment_monthly_usd {world.cost}, "
        f"gateway warm {gw_warm} state {gw_state}; {world.before.describe()}"
    )
    return Check(1, ok, detail)


def check_refusals(world: World) -> Check:
    off_by_one = world.api.put_warm([world.env_id], world.cost + 1)
    preview = world.api.put_warm([world.preview_id], world.cost)
    if 200 in (off_by_one.status, preview.status):
        world.warm_by_kit = True  # a refusal that was not one changed the setting
    after = world.api.get_warm()
    world.mark("refusals", off_by_one=off_by_one.status, preview=preview.status)
    refused = all(
        a.status == 422 and problem_code(a) == "VALIDATION_FAILED" for a in (off_by_one, preview)
    )
    unchanged = warm_of(after, world.env_id) is False and after.get("monthly_usd") == 0
    detail = (
        f"off by one: HTTP {off_by_one.status} {problem_code(off_by_one)}; preview: HTTP "
        f"{preview.status} {problem_code(preview)}; after: warm {warm_of(after, world.env_id)}, "
        f"monthly_usd {after.get('monthly_usd')}, gateway {gateway_of(after)[1]}"
    )
    return Check(2, refused and unchanged and gateway_of(after)[1] == "off", detail)


def check_on(world: World) -> Check:
    answer = world.api.put_warm([world.env_id], world.cost)
    world.t_on, world.wall_on = world.clock(), world.wall()
    world.mark("warm on", status=answer.status)
    if answer.status == 200:
        world.warm_by_kit = True
    doc = _json(answer)
    gw_warm, gw_state = gateway_of(doc)
    ok = (
        answer.status == 200
        and warm_of(doc, world.env_id) is True
        and doc.get("monthly_usd") == world.cost
        and gw_warm is False
        and gw_state == "off"
    )
    detail = (
        f"HTTP {answer.status or answer.error}, warm {warm_of(doc, world.env_id)}, "
        f"monthly_usd {doc.get('monthly_usd')}, gateway warm {gw_warm} state {gw_state}"
    )
    return Check(3, ok, detail)


def wait_min(
    world: World, wanted: int, since: float
) -> tuple[Observed, float | None, float | None]:
    """Read the service every 5 s, up to ``--settle-seconds``, until its minimum is ``wanted``
    and it is ready. Returns the last read and the seconds to each, or None."""
    min_s: float | None = None
    while True:
        seen = observe(world)
        if seen.service_min == wanted and min_s is None:
            min_s = world.clock() - since
            world.mark(f"service min {wanted}", revision=seen.revision)
        if min_s is not None and seen.service_min == wanted and seen.ready:
            ready_s = world.clock() - since
            world.mark("service ready", revision=seen.revision)
            return seen, min_s, ready_s
        if world.clock() - since >= world.args.settle_seconds:
            world.mark(f"service min {wanted} not reached", seen=seen.describe())
            return seen, min_s, None
        world.sleep(POLL_S)


def check_min_on(world: World) -> Check:
    seen, min_s, ready_s = wait_min(world, 1, world.t_on)
    before = world.before.revision if world.before else None
    same = seen.revision is not None and seen.revision == before
    world.data.update(min_on_s=min_s, ready_on_s=ready_s, revision=seen.revision)
    detail = (
        f"min 1 after {_s(min_s)}, ready after {_s(ready_s)} from the PUT; {seen.describe()}; "
        f"before {before}" + ("" if same else "; the serving revision changed")
    )
    return Check(4, min_s is not None and ready_s is not None and same, detail)


def read_kept(world: World) -> None:
    """30 s for the minimum instance to start, then three ``/health`` reads 5 s apart."""
    world.sleep(SETTLE_AFTER_S)
    headers = session_headers(world.cookie)
    for i in range(HEALTH_READS):
        if i:
            world.sleep(HEALTH_GAP_S)
        answer = world.http(world.base + "/health", headers, 120.0)
        seen = health_identity(answer)
        world.mark("health", read=i + 1, status=answer.status, seen=seen and seen.describe())
        if seen is not None:
            world.kept.append(seen)
    world.data["kept"] = [k.describe() for k in world.kept]


def find_row(
    world: World, since: datetime | None, wanted: Callable[[Mapping[str, Any]], bool]
) -> tuple[Mapping[str, Any] | None, str]:
    """The newest audit row on ``warm`` since ``since`` (less a margin for clock skew) that
    ``wanted`` accepts, and what was searched."""
    start = (since or world.wall()) - timedelta(seconds=SINCE_MARGIN_S)
    rows = world.api.audit(start)
    found = next((r for r in rows if wanted(r)), None)
    return found, f"{len(rows)} {ACTION} rows on {TARGET_KIND} since {start.isoformat()}"


def row_detail(row: Mapping[str, Any]) -> str:
    before, after = row.get("before") or {}, row.get("after") or {}
    return (
        f"seq {row.get('seq')} at {row.get('at')}, before {before.get('environment_ids')}, "
        f"after {after.get('environment_ids')}, gateway {after.get('gateway')}, "
        f"monthly_usd_shown {after.get('monthly_usd_shown')}"
    )


def check_audit(world: World, n: int, on: bool) -> Check:
    env = world.env_id

    def wanted(row: Mapping[str, Any]) -> bool:
        before, after = row.get("before") or {}, row.get("after") or {}
        ids, was = after.get("environment_ids"), before.get("environment_ids") or []
        shown = after.get("monthly_usd_shown")
        if after.get("gateway") is not False:
            return False
        if on:
            return ids == [env] and shown == world.cost and env not in was
        return ids == [] and shown == 0 and env in was and int(row.get("seq") or 0) > world.on_seq

    try:
        row, searched = find_row(world, world.wall_on if on else world.wall_off, wanted)
    except CommandError as exc:
        return Check(n, None, str(exc))
    if row is None:
        return Check(n, False, f"{searched}, none matched")
    if on:
        world.on_seq = int(row.get("seq") or 0)
    world.mark("audit row", seq=row.get("seq"), on=on)
    return Check(n, True, row_detail(row))


def idle(world: World, label: str) -> None:
    minutes = world.args.idle_minutes
    until = world.wall() + timedelta(minutes=minutes)
    world.say(f"{label}: no request to the app for {minutes} min, until {until:%H:%M:%S} UTC")
    world.mark(f"{label} idle start", minutes=minutes)
    for minute in range(1, minutes + 1):
        world.sleep(60.0)
        if minute % 5 == 0 and minute < minutes:
            world.say(f"{label}: {minute} of {minutes} min idle")
    world.mark(f"{label} idle end")


def warm_gateway(world: World) -> str:
    """One GET to the cell's ``www`` host, which the gateway answers itself: its cold start is
    paid here, not by the page load. The app is not asked."""
    answer = world.http(f"https://{world.www}/", {"User-Agent": USER_AGENT}, 120.0)
    world.mark("gateway warm-up", status=answer.status, seconds=round(answer.seconds, 3))
    return f"www warm-up HTTP {answer.status or answer.error} in {answer.seconds:.2f} s"


def page_load(world: World, label: str) -> Fetched:
    answer = world.http(world.base + "/", page_headers(world.cookie), 120.0)
    world.mark(
        f"{label} page load",
        status=answer.status,
        seconds=round(answer.seconds, 3),
        waking=is_waking(answer),
        wake_cookie=wake_cookie_set(answer),
    )
    return answer


def check_warm_load(world: World) -> Check:
    idle(world, "warm")
    gateway = warm_gateway(world)
    answer = page_load(world, "warm")
    seen = page_identity(answer)
    world.warm_seen = seen
    match = next((i for i, k in enumerate(world.kept, 1) if seen and seen.same(k)), None)
    after = health_identity(world.http(world.base + "/health", session_headers(world.cookie)))
    world.data.update(warm_ttfb_s=answer.seconds, warm_waking=is_waking(answer))
    ok = (
        is_app_page(answer)
        and not is_waking(answer)
        and match is not None
        and answer.seconds < WAKE_LIMIT_S
    )
    detail = (
        f"{gateway}; page HTTP {answer.status or answer.error} first byte {answer.seconds:.2f} s, "
        f"waking page {is_waking(answer)}, wake cookie set {wake_cookie_set(answer)}, "
        f"{seen.describe() if seen else 'no fixture page'}, "
        + (f"matches /health read {match} of {len(world.kept)}" if match else "matches no read")
        + f"; /health after: {after.describe() if after else 'unread'}"
    )
    return Check(6, ok, detail)


def check_off(world: World) -> Check:
    answer = world.api.put_warm([], 0)
    world.t_off, world.wall_off = world.clock(), world.wall()
    world.mark("warm off", status=answer.status)
    doc = _json(answer)
    if answer.status == 200 and warm_of(doc, world.env_id) is False:
        world.warm_by_kit, world.left_off = False, True
    seen: Observed | None = None
    min_s = ready_s = None
    if not world.warm_by_kit:
        seen, min_s, ready_s = wait_min(world, 0, world.t_off)
    world.data.update(min_off_s=min_s, ready_off_s=ready_s)
    ok = (
        not world.warm_by_kit
        and doc.get("monthly_usd") == 0
        and gateway_of(doc)[1] == "off"
        and min_s is not None
        and ready_s is not None
    )
    detail = (
        f"HTTP {answer.status or answer.error}, warm {warm_of(doc, world.env_id)}, monthly_usd "
        f"{doc.get('monthly_usd')}, gateway {gateway_of(doc)[1]}; min 0 after {_s(min_s)}, "
        f"ready after {_s(ready_s)}; {seen.describe() if seen else 'service not read'}"
    )
    return Check(7, ok, detail)


def follow_waking(world: World, first: Fetched, started: float) -> tuple[Fetched, int]:
    """What the waking page does by itself: load the page again every 2 s with the wake cookie,
    up to 60 s, until the app answers."""
    answer, retries = first, 0
    while not is_app_page(answer) and world.clock() - started < RETRY_LIMIT_S:
        world.sleep(WAKE_RETRY_S)
        answer = world.http(world.base + "/", page_headers(world.cookie, wake=True), 120.0)
        retries += 1
    world.mark("cold app answer", status=answer.status, retries=retries)
    return answer, retries


def check_cold_load(world: World) -> Check:
    idle(world, "cold")
    gateway = warm_gateway(world)
    started = world.clock()
    first = page_load(world, "cold")
    final, retries = follow_waking(world, first, started)
    cold_s = max(world.clock() - started, final.seconds)
    seen = page_identity(final)
    health = health_identity(world.http(world.base + "/health", session_headers(world.cookie)))
    earlier = [world.warm_seen] if world.warm_seen else list(world.kept)
    world.data.update(cold_waking=is_waking(first), cold_s=cold_s, cold_retries=retries)
    detail = (
        f"{gateway}; first HTTP {first.status or first.error} in {first.seconds:.2f} s, waking "
        f"page {is_waking(first)}, wake cookie set {wake_cookie_set(first)}, {retries} "
        f"retries, app answered after {cold_s:.1f} s; "
        f"{seen.describe() if seen else 'no fixture page'}; /health "
        f"{health.describe() if health else 'unread'}; "
        f"earlier {', '.join(e.describe() for e in earlier) or 'none known'}"
    )
    if seen is None:
        return Check(9, False, detail)
    if not earlier:
        return Check(9, None, detail)
    return Check(9, not any(seen.same(e) for e in earlier), detail)


# ── the run ──────────────────────────────────────────────────────────────────


def prove(world: World) -> None:
    """Checks 1 to 9 in order; a check the rest depend on stops the run when it fails."""
    world.checks.append(check_initial(world))
    if world.checks[-1].result is not True:
        world.lines.append("stopped before any change: the starting state is not as needed")
        return
    world.checks.append(check_refusals(world))
    world.checks.append(check_on(world))
    if not world.warm_by_kit:
        return
    world.checks.append(check_min_on(world))
    if world.data.get("min_on_s") is None:
        return
    read_kept(world)
    world.checks.append(check_audit(world, 5, on=True))
    world.checks.append(check_warm_load(world))
    world.checks.append(check_off(world))
    if world.warm_by_kit:
        return
    world.checks.append(check_audit(world, 8, on=False))
    world.checks.append(check_cold_load(world))


def restore(world: World, *, loud: bool) -> None:
    """Set warm off again if the kit set it on and has not yet set it off."""
    if not world.warm_by_kit:
        return
    answer = world.api.put_warm([], 0)
    world.mark("restore warm off", status=answer.status)
    if answer.status == 200 and warm_of(_json(answer), world.env_id) is False:
        world.warm_by_kit, world.left_off = False, True
        line = f"finally: warm set off again for {world.env_id} (HTTP 200)"
    else:
        world.left_off = False
        line = (
            f"finally: setting warm off answered {answer.status or answer.error}; undo by hand: "
            "untick it in the console's Warm option and save, or PUT /v1/warm "
            '{"environment_ids": [], "gateway": false, "monthly_usd_shown": 0}'
        )
    world.lines.append(line)
    if loud:
        world.say(line)


def verdict(checks: Sequence[Check]) -> bool | None:
    if any(c.result is False for c in checks):
        return False
    return None if any(c.result is None for c in checks) else True


def fill(world: World) -> list[Check]:
    """Every check 1 to 9, those not reached "not read"."""
    done = {c.n: c for c in world.checks}
    last = max(done, default=0)
    return [
        done.get(n) or Check(n, None, f"not reached: stopped after check {last}") for n in NAMES
    ]


def console_lines(world: World) -> list[str]:
    shot = results_dir() / SCREENSHOT
    present = shot.is_file() and shot.stat().st_size > 0
    world.data["console_screenshot"] = "present" if present else "absent"
    return [
        f"check 10 manual: console monthly add, results/{SCREENSHOT} "
        f"{'present' if present else 'absent'} (never part of the verdict)",
        "console: sign in as admin2, open the environment screen, Warm option panel; tick "
        f"{world.args.app}, read the monthly add (should be ${world.cost}, "
        f"environment_monthly_usd); untick; do not save. Save a screenshot as "
        f"spikes/proofrun/results/{SCREENSHOT}.",
    ]


def number(world: World) -> str:
    d = world.data
    left = {True: "yes", False: "NO", None: "unknown"}[world.left_off]
    return (
        f"on: min 1 after {_s(d.get('min_on_s'))}; warm load {_s(d.get('warm_ttfb_s'))}, "
        f"waking page {d.get('warm_waking')}; off: min 0 after {_s(d.get('min_off_s'))}; "
        f"cold load waking page {d.get('cold_waking')}, app after {_s(d.get('cold_s'))}; "
        f"warm left off: {left}"
    )


def finish(world: World) -> Outcome:
    checks = fill(world)
    lines = [c.line() for c in checks] + world.lines + console_lines(world)
    world.data.update(checks=[c.data() for c in checks], left_off=world.left_off)
    outcome = Outcome(PROOF, number(world), verdict(checks), lines, world.data)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = results_dir()
    folder.mkdir(parents=True, exist_ok=True)
    record = {"verdict": outcome.verdict, "number": outcome.number, "lines": lines}
    record |= {"timeline": world.timeline, **world.data}
    (folder / f"warm-{stamp}.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return outcome


def prepare(  # noqa: PLR0913, PLR0917  (the run's seams)
    args: argparse.Namespace,
    run: Run,
    send: Send,
    http: Http,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    wall: Callable[[], datetime],
) -> World | Outcome:
    """The preconditions, checked and never fixed: prod live, the prod host's cookie in the jar,
    the control API reachable with the CLI's login."""
    project = args.project or f"ssc-c-{args.label}"
    fence(args.app, args.label, project)
    prod = app_environment(run, args.app, "prod")
    if not prod.get("current_deployment_id"):
        raise CommandError(f"{args.app} prod is not live: ssc promote {args.app} --wait")
    preview = app_environment(run, args.app, "preview")
    base = str(prod["url"]).rstrip("/")
    host = host_of(base)
    www = www_host(host)
    fence(base, host, www)
    try:
        cookie = CookieJar().get(host)
    except CookieError as exc:
        line = f"stopped: {exc}"
        return Outcome(PROOF, f"no session cookie for {host}", False, [line], {"app": args.app})
    url, token = t8.control_api(run)
    fence(url)
    api = ControlApi(send, url.rstrip("/"), token)
    world = World(
        args=args,
        run=run,
        api=api,
        http=http,
        sleep=sleep,
        clock=clock,
        wall=wall,
        project=project,
        env_id=str(prod["id"]),
        preview_id=str(preview["id"]),
        base=base,
        www=www,
        cookie=cookie,
        t0=clock(),
    )
    world.data.update(app=args.app, env_id=world.env_id, host=host, project=project)
    world.data.update(idle_minutes=args.idle_minutes, settle_seconds=args.settle_seconds)
    return world


def run(  # noqa: PLR0913, PLR0917  (the kit's proof signature plus every seam)
    args: argparse.Namespace,
    run: Run = run_command,
    send: Send = request,
    http: Http = fetch,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Outcome:
    if args.idle_minutes < MIN_IDLE_MINUTES:
        raise CommandError(f"--idle-minutes is at least {MIN_IDLE_MINUTES}")
    world = prepare(args, run, send, http, sleep, clock, wall)
    if isinstance(world, Outcome):
        return world
    finished = False
    try:
        try:
            prove(world)
        except CommandError as exc:
            world.lines.append(f"stopped: {exc}")
        finished = True
    finally:
        restore(world, loud=not finished)
    return finish(world)
