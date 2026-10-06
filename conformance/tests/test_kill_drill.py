"""The kill switch drill (SSC-054) against fakes: the control plane and Google's APIs behind mock
transports, the front door as a script, and a virtual clock so a 26-minute wait costs nothing."""

import asyncio
import heapq
import itertools
import json
import re
import time
import urllib.parse
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from google.auth.exceptions import RefreshError
from websockets.asyncio.server import serve

from ssc_conformance import evidence as ev
from ssc_conformance import kill_drill as kd
from ssc_edge.session import COOKIE_NAME

BASE = 1_790_000_000.0
APP = "app_" + "a" * 20
ENV = "env_" + "b" * 20
ORG = "org_" + "c" * 20
USER = "usr_" + "d" * 20
SERVICE = "ssc-a-" + "b" * 20
HOST = "quiet-river-7f3k.abcdefghijkl.delimitusapps.com"
STEP_SECONDS = {
    "gateway_deny": 1.8,
    "datagw_suspend": 2.0,
    "egress_remove": 2.2,
    "scale_to_zero": 4.0,
    "pause_timers": 4.5,
}
ENVIRON = {
    "SSC_DRILL_API_URL": "https://api.test",
    "SSC_DRILL_TOKEN": "admin-token-value",
    "SSC_DRILL_APP_ID": APP,
    "SSC_DRILL_ENV_ID": ENV,
    "SSC_DRILL_HOST": HOST,
    "SSC_DRILL_PROJECT": "ssc-c-abcdefghijkl",
    "SSC_DRILL_ORG_ID": ORG,
    "SSC_DRILL_SESSION_COOKIE": "v1.s1.cookie",
}


class Clock:
    """Virtual time: a sleep ends when ``run`` has nothing else to do and moves time to it."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._count = itertools.count()

    def __call__(self) -> float:
        return self.now

    def wall(self) -> float:
        return BASE + self.now

    async def sleep(self, seconds: float) -> None:
        waiting = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.now + seconds, next(self._count), waiting))
        await waiting

    async def run[T](self, work: Coroutine[Any, Any, T]) -> T:
        task = asyncio.ensure_future(work)
        for _ in range(100_000):
            for _ in range(50):
                await asyncio.sleep(0)
            if task.done():
                return task.result()
            if self._sleepers:
                when, _, waiting = heapq.heappop(self._sleepers)
                self.now = max(self.now, when)
                waiting.set_result(None)
        raise AssertionError("the run never finished")


class FakeStream:
    def __init__(self, world: World) -> None:
        self.world = world
        self.closed = False

    async def drain(self) -> None:
        w = self.world
        while w.kill_at is None or w.clock.now < w.kill_at + w.cut:
            await w.clock.sleep(0.25)

    async def aclose(self) -> None:
        self.closed = True


class FakeFront:
    def __init__(self, world: World) -> None:
        self.world = world
        self.started = False

    async def health(self, wait: float = 5.0) -> int | None:
        w = self.world
        return 403 if w.kill_at is not None and w.clock.now >= w.kill_at + w.denial else 200

    async def start(self, run: str) -> bool:
        self.world.run = run
        return self.world.starts

    async def open_stream(self, run: str) -> FakeStream:
        return FakeStream(self.world)


class World:
    """One cell as the drill sees it. Times are seconds from the command."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.kill_at: float | None = None
        self.run = ""
        self.starts = True
        self.denial = 2.4
        self.cut = 2.5
        self.query_end: float | None = 3.1
        self.tunnel_end: float | None = 5.2
        self.query_running = True
        self.proxy_line_at: float | None = BASE - 1
        self.steps = dict(STEP_SECONDS)
        self.requests: list[Any] = []
        self.quiet_for_calls = 0
        self.app_requests = False
        self.gateway_requests = True
        self.enables = 0
        self.kill_calls = 0
        self.logs_asked: list[str] = []

    def control(self, request: httpx2.Request) -> httpx2.Response:
        path, now = request.url.path, self.clock.now
        assert request.headers["Authorization"] == "Bearer admin-token-value"
        if request.method == "POST":
            assert request.headers["Idempotency-Key"]
        if path.endswith("/kill-switch"):
            assert json.loads(request.content) == {"mode": "disable"}
            self.kill_at = now
            self.kill_calls += 1
            return httpx2.Response(202, json={"run_id": "ksr_1", "state": "running"})
        if path.endswith("/kill-switch/ksr_1"):
            assert self.kill_at is not None
            elapsed = now - self.kill_at
            steps = [
                {"name": n, "state": "done" if elapsed >= t else "running"}
                for n, t in self.steps.items()
            ]
            done = all(s["state"] == "done" for s in steps)
            return httpx2.Response(
                200, json={"state": "completed" if done else "running", "steps": steps}
            )
        if path.endswith("/enable"):
            self.enables += 1
            self.kill_at = None
            return httpx2.Response(200, json={"status": "active"})
        if path == "/v1/audit":
            query = dict(request.url.params)
            assert query["target_id"] == "ksr_1"
            assert query["action"] == "kill_switch.step"
            events = [
                {"after": {"step": n, "state": "running", "since_command_ms": None}}
                for n in self.steps
            ] + [
                {"after": {"step": n, "state": "done", "since_command_ms": int(t * 1000)}}
                for n, t in self.steps.items()
            ]
            return httpx2.Response(200, json={"events": events, "next_before_seq": None})
        return httpx2.Response(404)

    def drill_line(self, leg: str, seconds: float | None) -> list[dict[str, Any]]:
        if seconds is None:
            return []
        assert self.kill_at is not None
        at = BASE + self.kill_at + seconds
        running = self.query_running if leg == "query" else True
        payload = {"leg": leg, "event": "end", "run": self.run, "at": at, "running": running}
        return [{"jsonPayload": {"drill": payload | {"outcome": "APP_NOT_ACTIVE"}}}]

    def cloud(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if "/storage/v1/b/" in url:
            assert url.endswith(f"ssc-c-abcdefghijkl-cell/o/snapshots%2F{ORG}%2Flatest.json")
            assert self.kill_at is not None
            return httpx2.Response(200, json={"updated": kd.stamp(BASE + self.kill_at + 1.9)})
        body = json.loads(request.content)
        flt: str = body["filter"]
        self.logs_asked.append(flt)
        entries: list[dict[str, Any]] = []
        if 'jsonPayload.drill.event="end"' in flt:
            assert f'jsonPayload.drill.run="{self.run}"' in flt
            entries = self.drill_line("query", self.query_end) + self.drill_line(
                "tunnel", self.tunnel_end
            )
        elif 'resource.type="gce_instance"' in flt:
            stamp = re.search(r'timestamp>="([^"]+)"', flt)
            assert stamp is not None
            since = kd.epoch_of(stamp.group(1))
            seen = (
                self.proxy_line_at is not None and since is not None and since <= self.proxy_line_at
            )
            entries = [{"textPayload": "tunnel"}] if seen else []
        elif 'OR "ssc-datagw")' in flt:
            assert body["orderBy"] == "timestamp desc"
            if self.quiet_for_calls > 0:
                self.quiet_for_calls -= 1
                entries = [{"timestamp": kd.stamp(self.clock.wall() - 600)}]
        elif f'service_name=("{SERVICE}")' in flt:
            entries = [{"timestamp": "t"}] if self.app_requests else []
        elif 'service_name=("ssc-gateway")' in flt:
            entries = [{"timestamp": "t"}] if self.gateway_requests else []
        elif 'jsonPayload.drill.event="ready"' in flt:
            entries = []
        return httpx2.Response(200, json={"entries": entries} if entries else {})


def drill(world: World, clock: Clock, runs: int = 1) -> kd.Drill:
    cfg = kd.config_from_env(ENVIRON | {"SSC_DRILL_RUNS": str(runs)})

    async def token() -> str:
        return "google-token"

    return kd.Drill(
        cfg=cfg,
        api=kd.ControlPlane(
            cfg.api_url,
            str(cfg.token),
            client=httpx2.AsyncClient(transport=httpx2.MockTransport(world.control)),
        ),
        cloud=kd.CloudApi(
            cfg.project,
            token,
            client=httpx2.AsyncClient(transport=httpx2.MockTransport(world.cloud)),
        ),
        front=FakeFront(world),
        sleep=clock.sleep,
        clock=clock,
        wall=clock.wall,
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def world(clock: Clock) -> World:
    return World(clock)


def test_config_needs_every_variable_and_one_way_in() -> None:
    assert kd.config_from_env(ENVIRON).runs == 10
    for name in kd.ENV.values():
        with pytest.raises(kd.DrillError, match=name):
            kd.config_from_env({k: v for k, v in ENVIRON.items() if k != name})
    for left_out in (kd.TOKEN_ENV, kd.COOKIE_ENV):
        with pytest.raises(kd.DrillError, match=kd.CREDENTIALS_FILE_ENV):
            kd.config_from_env({k: v for k, v in ENVIRON.items() if k != left_out})
    filed = {k: v for k, v in ENVIRON.items() if k not in (kd.TOKEN_ENV, kd.COOKIE_ENV)}
    cfg = kd.config_from_env(filed | {kd.CREDENTIALS_FILE_ENV: "/run/sign-in.json"})
    assert (cfg.credentials_file, cfg.token, cfg.cookie) == ("/run/sign-in.json", None, None)
    for bad in ("x", "0"):
        with pytest.raises(kd.DrillError, match=kd.RUNS_ENV):
            kd.config_from_env(ENVIRON | {kd.RUNS_ENV: bad})


def sign_in_file(tmp_path: Path, **changes: object) -> Path:
    body = {
        "auth_url": "https://auth.test/",
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_at": BASE + 900,
        "cookie": "v1.s1.filed-cookie",
    } | changes
    path = tmp_path / "sign-in.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_the_sign_in_file_is_read_and_a_bad_one_is_never_echoed(tmp_path: Path) -> None:
    creds = kd.load_credentials(sign_in_file(tmp_path))
    assert (creds.auth_url, creds.access_token, creds.cookie) == (
        "https://auth.test",
        "access-1",
        "v1.s1.filed-cookie",
    )
    with pytest.raises(kd.DrillError, match="not a sign-in file"):
        kd.load_credentials(tmp_path / "missing.json")
    broken = sign_in_file(tmp_path, expires_at="secret-looking-value")
    with pytest.raises(kd.DrillError) as refused:
        kd.load_credentials(broken)
    assert "secret-looking-value" not in str(refused.value)
    broken.write_text('{"auth_url": "x"}', encoding="utf-8")
    with pytest.raises(kd.DrillError, match="KeyError"):
        kd.load_credentials(broken)


class AuthHost:
    """The auth host's ``/token``: each refresh token works once and brings the next."""

    def __init__(self) -> None:
        self.valid = "refresh-1"
        self.issued = 0
        self.asked: list[dict[str, str]] = []
        self.refuse = False

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        form = {k: v[0] for k, v in urllib.parse.parse_qs(request.content.decode()).items()}
        self.asked.append(form)
        assert str(request.url) == "https://auth.test/token"
        if self.refuse or form.get("refresh_token") != self.valid:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        self.issued += 1
        self.valid = f"refresh-{self.issued + 1}"
        return httpx2.Response(
            200,
            json={
                "access_token": f"access-{self.issued + 1}",
                "token_type": "Bearer",
                "expires_in": 900,
                "refresh_token": self.valid,
            },
        )


def signer(host: AuthHost, tmp_path: Path, clock: Clock) -> kd.SignIn:
    return kd.SignIn(
        kd.load_credentials(sign_in_file(tmp_path)),
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(host.handle)),
        wall=clock.wall,
    )


async def test_the_admins_token_is_refreshed_a_minute_before_it_ends(
    tmp_path: Path, clock: Clock
) -> None:
    host = AuthHost()
    token = signer(host, tmp_path, clock)
    assert await token() == "access-1"
    clock.now = 839  # 61 s before the first token ends
    assert await token() == "access-1"
    assert host.asked == []
    clock.now = 841
    assert await token() == "access-2"
    assert host.asked == [{"grant_type": "refresh_token", "refresh_token": "refresh-1"}]
    assert await token() == "access-2"
    clock.now = 841 + 900 - 59
    assert await token() == "access-3"
    assert host.asked[-1]["refresh_token"] == "refresh-2"
    await token.aclose()


async def test_an_ended_sign_in_stops_the_drill_without_echoing_the_token(
    tmp_path: Path, clock: Clock
) -> None:
    host = AuthHost()
    host.refuse = True
    token = signer(host, tmp_path, clock)
    clock.now = 900
    with pytest.raises(kd.DrillError) as refused:
        await token()
    assert "invalid_grant" in str(refused.value)
    assert "refresh-1" not in str(refused.value)
    await token.aclose()


async def test_the_control_plane_asks_the_token_source_for_every_call() -> None:
    seen: list[str] = []
    counter = iter(range(100))

    async def source() -> str:
        return f"token-{next(counter)}"

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["Authorization"])
        return httpx2.Response(200, json={})

    api = kd.ControlPlane(
        "https://api.test",
        source,
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    await api.enable(APP)
    await api.enable(APP)
    assert seen == ["Bearer token-0", "Bearer token-1"]


async def test_the_credentials_file_wins_over_a_hand_run_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources: list[object] = []

    class StoppedError(Exception):
        pass

    def stop(_base: str, token: object) -> None:
        sources.append(token)
        raise StoppedError

    monkeypatch.setattr(kd, "ControlPlane", stop)
    path = sign_in_file(tmp_path, expires_at=time.time() + 3600)
    with pytest.raises(StoppedError):
        await kd.main_async(ENVIRON | {kd.CREDENTIALS_FILE_ENV: str(path)})
    with pytest.raises(StoppedError):
        await kd.main_async(ENVIRON)
    filed, by_hand = sources
    assert isinstance(filed, kd.SignIn)
    assert await filed() == "access-1"
    await filed.aclose()
    assert by_hand == "admin-token-value"


async def test_the_control_plane_calls_and_never_shows_the_token() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path.endswith("/enable") and len(seen) == 2:
            return httpx2.Response(409, json={"code": "APP_ALREADY_ACTIVE"})
        if request.url.path.endswith("/enable"):
            return httpx2.Response(403, json={"code": "FORBIDDEN", "detail": "admin-token-value"})
        return httpx2.Response(202, json={"run_id": "ksr_9", "state": "running"})

    api = kd.ControlPlane(
        "https://api.test/",
        "admin-token-value",
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    assert await api.kill(APP) == "ksr_9"
    await api.enable(APP)
    with pytest.raises(kd.DrillError) as refused:
        await api.enable(APP)
    assert "FORBIDDEN" in str(refused.value)
    assert "admin-token-value" not in str(refused.value)
    assert seen[0].url == f"https://api.test/v1/apps/{APP}/kill-switch"
    assert seen[0].headers["Idempotency-Key"] != seen[1].headers["Idempotency-Key"]


async def test_the_front_door_over_http_and_a_real_websocket() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["Cookie"] == f"{COOKIE_NAME}=cookie-value"
        if request.url.path == "/start":
            return httpx2.Response(200 if request.url.params["run"] == "r1" else 503)
        return httpx2.Response(403)

    seen: dict[str, str] = {}

    async def ticks(ws: Any) -> None:
        seen["cookie"] = ws.request.headers["Cookie"]
        seen["origin"] = ws.request.headers["Origin"]
        seen["path"] = ws.request.path
        for n in range(3):
            await ws.send(str(n))
        await ws.close()

    async with serve(ticks, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        front = kd.FrontDoor(
            f"http://127.0.0.1:{port}",
            "cookie-value",
            client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        )
        assert await front.health() == 403
        assert await front.start("r1") is True
        assert await front.start("r2") is False
        stream = await front.open_stream("r1")
        await asyncio.wait_for(stream.drain(), 5)
        assert stream.closed
        await stream.aclose()
    assert seen == {
        "cookie": f"{COOKIE_NAME}=cookie-value",
        "origin": f"http://127.0.0.1:{port}",
        "path": "/ws?run=r1",
    }


async def test_a_refused_websocket_is_a_drill_error() -> None:
    front = kd.FrontDoor("http://127.0.0.1:9", "c", client=httpx2.AsyncClient())
    with pytest.raises(kd.DrillError, match="WebSocket"):
        await front.open_stream("r1")


async def test_an_awake_run_is_timed_from_the_command(world: World, clock: Clock) -> None:
    run = await clock.run(drill(world, clock).awake())
    assert run.problems == ()
    assert run.door == pytest.approx(2.5)
    assert run.door_status == 403
    assert run.stream == pytest.approx(2.5)
    assert run.query == pytest.approx(3.1)
    assert run.tunnel == pytest.approx(5.2)
    assert run.instance_stop == pytest.approx(4.0)
    assert run.timer_pause == pytest.approx(4.5)
    assert run.latest == pytest.approx(1.9)
    assert run.proxy_logged is True
    assert {n: s.state for n, s in run.steps.items()} == dict.fromkeys(STEP_SECONDS, "done")
    assert kd.judge(run) == []
    assert world.enables == 2


async def test_an_awake_run_names_what_it_did_not_see(world: World, clock: Clock) -> None:
    world.query_running = False
    world.tunnel_end = None
    world.proxy_line_at = None
    run = await clock.run(drill(world, clock).awake())
    assert run.tunnel is None
    assert run.proxy_logged is False
    assert "query was not running at the kill (APP_NOT_ACTIVE)" in run.problems[0]
    assert "no end line for tunnel" in run.problems
    assert "tunnel close not seen" in kd.judge(run)
    assert clock.now > kd.LOGS_LIMIT_SECONDS


async def test_a_run_over_a_limit_fails(world: World, clock: Clock) -> None:
    world.denial = 12.0
    world.steps["scale_to_zero"] = 61.0
    run = await clock.run(drill(world, clock).awake())
    problems = kd.judge(run)
    assert "front door denial took 12.0 s (limit 10 s)" in problems
    assert "step scale_to_zero took 61.0 s (limit 60 s)" in problems


async def test_the_command_is_not_blamed_when_the_stream_died_first(
    world: World, clock: Clock
) -> None:
    class Dead(FakeStream):
        def __init__(self, w: World) -> None:
            super().__init__(w)
            self.closed = True

    front = FakeFront(world)

    async def open_dead(run: str) -> FakeStream:
        return Dead(world)

    front.open_stream = open_dead
    d = drill(world, clock)
    d._front = front
    with pytest.raises(kd.DrillError, match="closed before the command"):
        await clock.run(d.awake())
    assert world.kill_calls == 0


async def test_an_asleep_run_proves_nothing_started_from_the_logs(
    world: World, clock: Clock
) -> None:
    world.denial = 1.8
    world.quiet_for_calls = 1
    run = await clock.run(drill(world, clock).asleep())
    assert run.problems == ()
    assert run.nothing_started is True
    assert run.door == pytest.approx(2.0)
    assert run.door_status == 403
    assert run.stream is None
    assert kd.judge(run) == []
    assert clock.now > kd.ASLEEP_AFTER_SECONDS - 600
    asked = " ".join(world.logs_asked)
    for needle in ("ssc-datagw", "datagw ready", "datagw query", "gce_instance", "log_id"):
        assert needle in asked


async def test_asleep_proof_fails_when_something_started(world: World, clock: Clock) -> None:
    world.denial = 1.8
    world.app_requests = True
    world.gateway_requests = False
    run = await clock.run(drill(world, clock).asleep())
    assert run.nothing_started is False
    assert "the app received a request" in run.problems
    assert any("logs prove nothing" in p for p in run.problems)
    assert "nothing started is not proved" in kd.judge(run)


async def test_an_asleep_request_that_is_served_is_a_failure(world: World, clock: Clock) -> None:
    world.denial = 1_000.0
    run = await clock.run(drill(world, clock).asleep())
    assert run.door is None
    assert "the request after the kill was not refused (200)" in run.problems


async def test_a_busy_cell_is_waited_out_then_given_up_on(world: World, clock: Clock) -> None:
    world.quiet_for_calls = 10_000
    with pytest.raises(kd.DrillError, match="did not go quiet"):
        await clock.run(drill(world, clock)._wait_quiet())


async def test_both_states_make_one_report(world: World, clock: Clock) -> None:
    world.denial = 1.8
    report = await clock.run(drill(world, clock, runs=2).execute())
    assert [r.state for r in report.runs] == ["awake", "awake", "asleep", "asleep"]
    assert report.failures == []
    assert report.longest_stream == pytest.approx(2.5)
    text = report.markdown()
    awake = ["2.0 / 2.0", "2.5 / 2.5", "3.1 / 3.1", "5.2 / 5.2", "4.0 / 4.0", "4.5 / 4.5"]
    assert "| awake | 2 | " + " | ".join([*awake, "1.9 / 1.9"]) + " |" in text
    asleep = ["2.0 / 2.0", "none open", "nothing started", "nothing started"]
    assert (
        "| asleep | 2 | " + " | ".join([*asleep, "4.0 / 4.0", "4.5 / 4.5", "missing"]) + " |"
        in text
    )
    assert "Longest an open stream survived: 2.5 s" in text
    assert "Verdict: PASS" in text


async def test_a_run_that_cannot_be_taken_ends_its_state(world: World, clock: Clock) -> None:
    world.starts = False
    report = await clock.run(drill(world, clock, runs=3).execute())
    awake = report.of("awake")
    assert len(awake) == 1
    assert "run not taken: the drill app did not start" in awake[0].problems[0]
    assert report.markdown().count("Verdict: FAIL") == 1
    assert world.kill_calls == 0 or world.enables >= 1


def test_asleep_needs_the_proxy_log_seen_when_awake() -> None:
    steps = {n: kd.StepTime("done", 3.0) for n in kd.STEPS}
    awake = kd.Observed(
        "awake", door=1.0, stream=1.0, query=1.0, tunnel=1.0, steps=steps, proxy_logged=False
    )
    asleep = kd.Observed("asleep", door=1.0, steps=steps, nothing_started=True)
    failures = kd.Report([awake, asleep]).failures
    assert len(failures) == 1
    assert "proxy" in failures[0]
    assert kd.Report([awake]).failures == ["asleep: no runs"]
    assert kd.Report([]).failures == ["awake: no runs", "asleep: no runs"]


def test_the_table_says_missing_rather_than_guess() -> None:
    steps = {n: kd.StepTime("done", 3.0) for n in kd.STEPS}
    run = kd.Observed("awake", door=1.0, stream=None, query=1.0, tunnel=1.0, steps=steps)
    text = kd.Report([run]).markdown()
    assert "| awake | 1 | 1.0 / 1.0 | missing |" in text
    assert "Longest an open stream survived: not seen" in text
    assert "Verdict: FAIL" in text
    assert "stream cut not seen" in text


async def test_the_drills_line_for_the_nightly_page(world: World, clock: Clock) -> None:
    world.denial = 1.8
    report = await clock.run(drill(world, clock, runs=2).execute())
    assert report.result() == ev.Result("drill", ev.OK, "2 runs per state, longest stream 2.5 s")
    failed = kd.Report([]).result()
    assert (failed.status, failed.reason) == (ev.FAIL, "awake: no runs (and 1 more)")


def test_main_adds_the_drills_line_to_the_evidence_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "evidence.json"
    for name in kd.ENV.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SSC_DRILL_PROJECT", "ssc-c-one")
    monkeypatch.setenv(ev.EVIDENCE_ENV, str(path))
    assert kd.main() == 1
    written = ev.read_file(path)
    assert written.cell == "ssc-c-one"
    assert [(r.proof, r.status) for r in written.results] == [("drill", ev.FAIL)]
    assert "SSC_DRILL_API_URL" in written.results[0].reason


def test_step_outcomes_other_than_done_fail() -> None:
    steps = {n: kd.StepTime("done", 3.0) for n in kd.STEPS}
    steps["egress_remove"] = kd.StepTime("unconfirmed", 3.0)
    del steps["pause_timers"]
    problems = kd.judge(kd.Observed("asleep", door=1.0, steps=steps, nothing_started=True))
    assert problems == ["step egress_remove unconfirmed", "step pause_timers missing"]


def test_log_filters_name_the_run_and_the_window() -> None:
    since = BASE
    flt = kd.leg_filter(SERVICE, "r1", since)
    assert 'jsonPayload.drill.run="r1"' in flt
    assert f'timestamp>="{kd.stamp(since - kd.LOG_CLOCK_MARGIN_SECONDS)}"' in flt
    assert kd.stamp(0.5) == "1970-01-01T00:00:00.500Z"
    assert kd.epoch_of("2026-10-03T12:00:00.123456789Z") == pytest.approx(1791028800.123456)
    assert kd.epoch_of("nonsense") is None


async def test_the_object_time_is_none_when_it_is_missing() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={})

    async def token() -> str:
        return "t"

    cloud = kd.CloudApi(
        "p", token, client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )
    assert await cloud.object_updated("b", "o") is None


def test_main_reports_a_missing_setting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in kd.ENV.values():
        monkeypatch.delenv(name, raising=False)
    assert kd.main() == 1


class FakeCredentials:
    def __init__(self, token: str | None, ends_in: float | None) -> None:
        self.token = token
        self.expiry = (
            None
            if ends_in is None
            else datetime.fromtimestamp(NOW + ends_in, UTC).replace(tzinfo=None)
        )
        self.refreshed = 0


NOW = 1_800_000_000.0
FILE_ENV = {kd.CREDENTIALS_ENV: "/creds.json"}


def refresher(credentials: FakeCredentials) -> None:
    credentials.refreshed += 1
    credentials.token = f"fresh-{credentials.refreshed}"
    credentials.expiry = datetime.fromtimestamp(NOW + 3600, UTC).replace(tzinfo=None)


def tokens(environ: dict[str, str], credentials: FakeCredentials | None = None) -> Any:
    return kd.google_access_tokens(
        environ,
        load=lambda: credentials,
        refresh=refresher,
        wall=lambda: NOW,
    )


async def test_a_given_token_wins_over_the_credential_file() -> None:
    credentials = FakeCredentials("file-token", 3600)
    source = tokens({kd.ACCESS_ENV: "given-token"} | FILE_ENV, credentials)
    assert await source() == "given-token"
    assert credentials.refreshed == 0


async def test_the_credential_file_is_used_and_refreshed_only_near_expiry() -> None:
    credentials = FakeCredentials("file-token", 3600)
    source = tokens(FILE_ENV, credentials)
    assert await source() == "file-token"
    credentials.expiry = datetime.fromtimestamp(NOW + 301, UTC).replace(tzinfo=None)
    assert await source() == "file-token"
    assert credentials.refreshed == 0
    credentials.expiry = datetime.fromtimestamp(NOW + 299, UTC).replace(tzinfo=None)
    assert await source() == "fresh-1"
    assert await source() == "fresh-1"
    assert credentials.refreshed == 1


async def test_a_credential_with_no_token_or_expiry_is_refreshed() -> None:
    for blank in (FakeCredentials(None, 3600), FakeCredentials("t", None)):
        assert await tokens(FILE_ENV, blank)() == "fresh-1"


async def test_without_either_the_token_comes_from_gcloud(monkeypatch: pytest.MonkeyPatch) -> None:
    async def gcloud(*args: str) -> str:
        assert args == ("print-access-token",)
        return "gcloud-token"

    monkeypatch.setattr("ssc_conformance.nightly._gcloud", gcloud)
    monkeypatch.delenv(kd.ACCESS_ENV, raising=False)
    assert await tokens({})() == "gcloud-token"


async def test_a_credential_failure_never_shows_the_token() -> None:
    credentials = FakeCredentials("secret-google-token", 10)

    def failing(_: FakeCredentials) -> None:
        raise RefreshError("secret-google-token rejected")

    source = kd.google_access_tokens(
        FILE_ENV, load=lambda: credentials, refresh=failing, wall=lambda: NOW
    )
    with pytest.raises(kd.DrillError) as refused:
        await source()
    assert "secret-google-token" not in str(refused.value)
    assert refused.value.__cause__ is None
