"""The kill switch drill (SSC-054): ``python -m ssc_conformance.kill_drill``.

Fires the kill switch at the drill app (``conformance/kill_drill_app``) in a staging cell as the
console does, with ``POST /v1/apps/{id}/kill-switch`` and an admin token, and times how fast each
thing is cut off. Every run is made twice over, in two states, ``SSC_DRILL_RUNS`` times each (ten),
with ``POST /v1/apps/{id}/enable`` between runs:

- **awake**: the app holds an open WebSocket, a long query through the data gateway and an open
  tunnel through the egress proxy. Timed from the command: the first refused ``/health`` through
  the cell's public load balancer, the end of the WebSocket as the client sees it, the end of the
  query and of the tunnel as the drill app logged them (Cloud Logging), the instance stop and the
  timer pause from the audit's ``kill_switch.step`` rows (``since_command_ms``), and when
  ``snapshots/<org>/latest.json`` last moved.
- **asleep**: the app, the gateway and the data gateway have had no request for 26 minutes. The
  kill is fired, and one request arrives once its ``gateway_deny`` step is done. Nothing can send
  a query or open a tunnel as an app that is at zero, so those cells are published as "nothing
  started", proved from the logs: no request reached the app, no new app instance, no new data
  gateway instance or query, no proxy line for the environment.

Pass: front-door denial under 10 s and everything under 60 s, in both states. Prints each state's
median and maximum, the longest an open stream survived, the verdict and a markdown table.

Configuration, all required: ``SSC_DRILL_API_URL``, ``SSC_DRILL_APP_ID``, ``SSC_DRILL_ENV_ID``
(the environment the drill app serves), ``SSC_DRILL_HOST`` (its public host),
``SSC_DRILL_PROJECT`` (the cell project, whose bucket is ``<project>-cell``) and
``SSC_DRILL_ORG_ID``. The admin's access token and the front door's session of a person granted
on the app come from ``SSC_DRILL_CREDENTIALS_FILE``, the JSON the night's sign-in writes
(``e2e/isolation/night-login.ts``): the drill refreshes the token a minute before it ends, since
a drill outlasts one, and keeps the rotated refresh token in memory. For a hand run,
``SSC_DRILL_TOKEN`` (an admin's access token) and ``SSC_DRILL_SESSION_COOKIE`` (a cookie value)
instead; the file wins when both are set. Optional: ``SSC_DRILL_RUNS``, and ``SSC_EVIDENCE_FILE``,
the file this run adds its ``drill`` line to for the one-page result (``ssc_conformance.matrix``);
the cell is ``SSC_DRILL_PROJECT``.
Google calls use ``SSC_ACCESS_TOKEN``, else the credential file ``GOOGLE_APPLICATION_CREDENTIALS``
names (refreshed as it nears expiry, since a drill outlasts one token), else ``gcloud``; the
caller needs log read on the cell project and read on its bucket. Exits 1 when the verdict is not
a pass.
"""

import asyncio
import json
import logging
import os
import secrets
import statistics
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, cast
from urllib.parse import quote, urlencode, urlsplit

import httpx2
from google.auth.exceptions import GoogleAuthError
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException
from websockets.typing import Origin

from ssc_conformance import evidence as ev
from ssc_conformance import matrix
from ssc_conformance.nightly import LOGGING_API, Clock, Json, Sleep, gcloud_access_tokens
from ssc_control.runtime.cell_agent import AccessTokens
from ssc_edge.session import COOKIE_NAME
from ssc_shared.runtime import service_name

log = logging.getLogger("ssc_conformance.kill_drill")

ENV: Final = {
    "api_url": "SSC_DRILL_API_URL",
    "app_id": "SSC_DRILL_APP_ID",
    "env_id": "SSC_DRILL_ENV_ID",
    "host": "SSC_DRILL_HOST",
    "project": "SSC_DRILL_PROJECT",
    "org_id": "SSC_DRILL_ORG_ID",
}
ACCESS_ENV: Final = "SSC_ACCESS_TOKEN"
CREDENTIALS_ENV: Final = "GOOGLE_APPLICATION_CREDENTIALS"
CLOUD_SCOPE: Final = "https://www.googleapis.com/auth/cloud-platform"
REFRESH_MARGIN_SECONDS: Final = 300.0
TOKEN_ENV: Final = "SSC_DRILL_TOKEN"  # noqa: S105
COOKIE_ENV: Final = "SSC_DRILL_SESSION_COOKIE"
CREDENTIALS_FILE_ENV: Final = "SSC_DRILL_CREDENTIALS_FILE"
SIGN_IN_MARGIN_SECONDS: Final = 60.0
RUNS_ENV: Final = "SSC_DRILL_RUNS"
DEFAULT_RUNS: Final = 10
STATES: Final = ("awake", "asleep")
STEPS: Final = ("gateway_deny", "datagw_suspend", "egress_remove", "scale_to_zero", "pause_timers")
OK: Final = 200
DOOR_LIMIT_SECONDS: Final = 10.0
ALL_LIMIT_SECONDS: Final = 60.0
DOOR_POLL_SECONDS: Final = 0.25
FOLLOW_POLL_SECONDS: Final = 0.5
FOLLOW_LIMIT_SECONDS: Final = 300.0
WATCH_SECONDS: Final = 60.0
AWAKE_LIMIT_SECONDS: Final = 300.0
AWAKE_POLL_SECONDS: Final = 2.0
LOGS_LIMIT_SECONDS: Final = 180.0
LOGS_POLL_SECONDS: Final = 5.0
SETTLE_SECONDS: Final = 60.0
ASLEEP_AFTER_SECONDS: Final = 26 * 60.0
QUIET_LIMIT_SECONDS: Final = 75 * 60.0
HEALTH_TIMEOUT_SECONDS: Final = 5.0
COLD_HEALTH_TIMEOUT_SECONDS: Final = 30.0
CALL_TIMEOUT_SECONDS: Final = 30.0
GATEWAY_SERVICE: Final = "ssc-gateway"
DATAGW_SERVICE: Final = "ssc-datagw"
STORAGE_API: Final = "https://storage.googleapis.com/storage/v1"
REQUESTS_LOG: Final = "run.googleapis.com/requests"
SERVICE_RESOURCE: Final = 'resource.type="cloud_run_revision"'
LEG_ENDS: Final = ("query", "tunnel")
LOG_CLOCK_MARGIN_SECONDS: Final = 5.0
PROXY_LOOKBACK_SECONDS: Final = 120.0


class DrillError(Exception):
    """The drill could not take a measurement: a call failed or the setup is not as required."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DrillConfig:
    api_url: str
    app_id: str
    env_id: str
    host: str
    project: str
    org_id: str
    credentials_file: str | None
    token: str | None
    cookie: str | None
    runs: int


def config_from_env(environ: Mapping[str, str]) -> DrillConfig:
    """The drill's settings, or ``DrillError`` naming what is missing. Never echoes a value."""
    missing = [name for name in ENV.values() if not environ.get(name)]
    if missing:
        raise DrillError(f"missing {', '.join(missing)}")
    file, token, cookie = (
        environ.get(n) or None for n in (CREDENTIALS_FILE_ENV, TOKEN_ENV, COOKIE_ENV)
    )
    if file is None and (token is None or cookie is None):
        raise DrillError(f"set {CREDENTIALS_FILE_ENV}, or {TOKEN_ENV} with {COOKIE_ENV}")
    try:
        runs = int(environ.get(RUNS_ENV) or DEFAULT_RUNS)
    except ValueError:
        raise DrillError(f"{RUNS_ENV} is not a number") from None
    if runs < 1:
        raise DrillError(f"{RUNS_ENV} must be at least 1")
    return DrillConfig(
        **{field_: environ[name] for field_, name in ENV.items()},
        credentials_file=file,
        token=token,
        cookie=cookie,
        runs=runs,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Credentials:
    """The night's sign-in as the admin: what ``night-login.ts`` writes."""

    auth_url: str
    access_token: str
    refresh_token: str
    expires_at: float
    cookie: str


def load_credentials(path: Path) -> Credentials:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        return Credentials(
            auth_url=str(body["auth_url"]).rstrip("/"),
            access_token=str(body["access_token"]),
            refresh_token=str(body["refresh_token"]),
            expires_at=float(body["expires_at"]),
            cookie=str(body["cookie"]),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DrillError(
            f"{CREDENTIALS_FILE_ENV}: not a sign-in file ({type(exc).__name__})"
        ) from None


class SignIn:
    """The admin's access token, refreshed at the auth host a minute before it ends. The refresh
    token rotates on every use; the newest is kept in memory and never written back."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        client: httpx2.AsyncClient | None = None,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._auth_url = credentials.auth_url
        self._access = credentials.access_token
        self._refresh = credentials.refresh_token
        self._expires_at = credentials.expires_at
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)
        self._wall = wall
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __call__(self) -> str:
        async with self._lock:
            if self._expires_at - self._wall() < SIGN_IN_MARGIN_SECONDS:
                await self._renew()
            return self._access

    async def _renew(self) -> None:
        form = {"grant_type": "refresh_token", "refresh_token": self._refresh}
        try:
            response = await self._client.post(f"{self._auth_url}/token", data=form)
        except httpx2.HTTPError as exc:
            raise DrillError(f"refreshing the admin's sign-in: {type(exc).__name__}") from None
        body = _obj(response.json()) if response.content else {}
        if response.status_code != OK:
            code = str(body.get("error") or response.status_code)
            raise DrillError(f"the admin's sign-in could not be refreshed ({code})")
        try:
            self._access = str(body["access_token"])
            self._refresh = str(body["refresh_token"])
            self._expires_at = self._wall() + float(body["expires_in"])
        except KeyError, TypeError, ValueError:
            raise DrillError("the auth host's refresh answer was not understood") from None


def _default_credentials() -> Any:
    import google.auth  # noqa: PLC0415

    default = cast(Callable[..., tuple[Any, str | None]], google.auth.default)  # pyright: ignore[reportUnknownMemberType]
    return default(scopes=[CLOUD_SCOPE])[0]


def _refresh_credentials(credentials: Any) -> None:
    from google.auth.transport.requests import Request  # noqa: PLC0415

    credentials.refresh(Request())


def google_access_tokens(
    environ: Mapping[str, str],
    *,
    load: Callable[[], Any] = _default_credentials,
    refresh: Callable[[Any], None] = _refresh_credentials,
    wall: Callable[[], float] = time.time,
) -> AccessTokens:
    """The Google token source: ``SSC_ACCESS_TOKEN``, else the credential file named by
    ``GOOGLE_APPLICATION_CREDENTIALS``, refreshed in a thread whenever the token is missing or
    ends within five minutes, else ``gcloud``. Never logged."""
    if fixed := environ.get(ACCESS_ENV):

        async def given() -> str:
            return fixed

        return given
    if not environ.get(CREDENTIALS_ENV):
        return gcloud_access_tokens()
    loaded: list[Any] = []

    def current() -> str:
        if not loaded:
            loaded.append(load())
        credentials = loaded[0]
        expiry = credentials.expiry
        ends = None if expiry is None else expiry.replace(tzinfo=UTC).timestamp()
        if not credentials.token or ends is None or ends - wall() < REFRESH_MARGIN_SECONDS:
            refresh(credentials)
        return str(credentials.token)

    async def token() -> str:
        try:
            return await asyncio.to_thread(current)
        except GoogleAuthError:
            raise DrillError("the Google credential could not be loaded or refreshed") from None

    return token


def _fixed(value: str) -> AccessTokens:
    async def given() -> str:
        return value

    return given


def _obj(value: object) -> Json:
    return cast(Json, value) if isinstance(value, dict) else {}


def _objs(value: object) -> list[Json]:
    items = cast("list[object]", value) if isinstance(value, list) else []
    return [cast(Json, v) for v in items if isinstance(v, dict)]


def stamp(epoch: float) -> str:
    """``epoch`` as an RFC 3339 UTC time with milliseconds."""
    return (
        datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def epoch_of(value: object) -> float | None:
    """The seconds since the epoch of an RFC 3339 time, or None when it is not one."""
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class StepTime:
    """One finished kill switch step: its state and seconds from the command."""

    state: str
    seconds: float | None


class ControlPlane:
    """The control plane API, as the console calls it. The token is never logged or echoed."""

    def __init__(
        self, base_url: str, token: str | AccessTokens, *, client: httpx2.AsyncClient | None = None
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = _fixed(token) if isinstance(token, str) else token
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def kill(self, app_id: str) -> str:
        """Pulls the kill switch (``disable``) and returns the run's id."""
        body = await self._call("POST", f"/v1/apps/{app_id}/kill-switch", {"mode": "disable"})
        run_id = str(body.get("run_id") or "")
        if not run_id:
            raise DrillError("the kill switch answered with no run id")
        return run_id

    async def run(self, app_id: str, run_id: str) -> Json:
        return await self._call("GET", f"/v1/apps/{app_id}/kill-switch/{run_id}")

    async def enable(self, app_id: str) -> None:
        """Makes the app active again; an app that is active already is fine."""
        await self._call("POST", f"/v1/apps/{app_id}/enable", ok_codes=("APP_ALREADY_ACTIVE",))

    async def steps(self, run_id: str) -> dict[str, StepTime]:
        """Each finished step of the run from the audit: its state and ``since_command_ms``."""
        query = urlencode(
            {
                "action": "kill_switch.step",
                "target_kind": "kill_switch_run",
                "target_id": run_id,
                "limit": 50,
            }
        )
        found: dict[str, StepTime] = {}
        for event in _objs((await self._call("GET", f"/v1/audit?{query}")).get("events")):
            after = _obj(event.get("after"))
            if after.get("state") == "running":
                continue
            ms = after.get("since_command_ms")
            seconds = float(ms) / 1000 if isinstance(ms, int | float) else None
            found[str(after.get("step"))] = StepTime(str(after.get("state")), seconds)
        return found

    async def _call(
        self,
        method: str,
        path: str,
        body: Json | None = None,
        *,
        ok_codes: Sequence[str] = (),
    ) -> Json:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        if method == "POST":
            headers["Idempotency-Key"] = str(uuid.uuid4())
        try:
            response = await self._client.request(
                method, self._base + path, json=body, headers=headers
            )
        except httpx2.HTTPError as exc:
            raise DrillError(f"{method} {path}: {type(exc).__name__}") from None
        if response.is_success:
            return _obj(response.json())
        code = str(_obj(response.json() if response.content else None).get("code") or "")
        if code and code in ok_codes:
            return {}
        raise DrillError(f"{method} {path}: HTTP {response.status_code} {code}".strip())


class CloudApi:
    """Cloud Logging and Cloud Storage reads with the caller's access token."""

    def __init__(
        self,
        project: str,
        access_tokens: AccessTokens,
        *,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._project = project
        self._access_tokens = access_tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def entries(
        self, log_filter: str, *, newest_first: bool = False, one_page: bool = False
    ) -> list[Json]:
        """The log entries that match, every page (or the first, with ``one_page``)."""
        query: Json = {
            "resourceNames": [f"projects/{self._project}"],
            "filter": log_filter,
            "orderBy": "timestamp desc" if newest_first else "timestamp asc",
            "pageSize": 1 if one_page else 200,
        }
        found: list[Json] = []
        token = ""
        while True:
            page = query | {"pageToken": token} if token else query
            body = await self._call("POST", f"{LOGGING_API}/entries:list", page)
            found.extend(_objs(body.get("entries")))
            token = str(body.get("nextPageToken") or "")
            if one_page or not token:
                return found

    async def object_updated(self, bucket: str, name: str) -> float | None:
        """When the object last changed, or None when it is not there."""
        url = f"{STORAGE_API}/b/{bucket}/o/{quote(name, safe='')}"
        try:
            body = await self._call("GET", url)
        except DrillError:
            return None
        return epoch_of(body.get("updated"))

    async def _call(self, method: str, url: str, body: Json | None = None) -> Json:
        token = await self._access_tokens()
        try:
            response = await self._client.request(
                method, url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx2.HTTPError as exc:
            raise DrillError(f"{method} {url}: {type(exc).__name__}") from None
        if not response.is_success:
            raise DrillError(f"{method} {url}: HTTP {response.status_code} {response.text[:300]}")
        return _obj(response.json())


class Stream(Protocol):
    """An open WebSocket to the drill app."""

    @property
    def closed(self) -> bool: ...

    async def drain(self) -> None:
        """Reads (and so answers pings) until the other side ends the stream."""
        ...

    async def aclose(self) -> None: ...


class Front(Protocol):
    """The drill app through the cell's public load balancer, with a signed-in session."""

    async def health(self, wait: float = HEALTH_TIMEOUT_SECONDS) -> int | None:
        """``/health``'s status, or None when no answer came."""
        ...

    async def start(self, run: str) -> bool:
        """Has the app start its query and tunnel for ``run``; True once both are running."""
        ...

    async def open_stream(self, run: str) -> Stream: ...


class _Socket:
    def __init__(self, ws: Any) -> None:
        self._ws = ws

    @property
    def closed(self) -> bool:
        return bool(self._ws.close_code is not None)

    async def drain(self) -> None:
        try:
            async for _ in self._ws:
                continue
        except WebSocketException:
            return

    async def aclose(self) -> None:
        await self._ws.close()


class FrontDoor:
    """``Front`` over HTTPS and WSS. ``base`` is ``https://<host>``; the cookie never leaves the
    request headers."""

    def __init__(
        self, base: str, cookie_value: str, *, client: httpx2.AsyncClient | None = None
    ) -> None:
        self._base = base.rstrip("/")
        self._headers = {"Cookie": f"{COOKIE_NAME}={cookie_value}", "User-Agent": "ssc-kill-drill"}
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self, wait: float = HEALTH_TIMEOUT_SECONDS) -> int | None:
        try:
            response = await self._client.get(
                f"{self._base}/health", headers=self._headers, timeout=wait
            )
        except httpx2.HTTPError:
            return None
        return response.status_code

    async def start(self, run: str) -> bool:
        try:
            response = await self._client.get(
                f"{self._base}/start", params={"run": run}, headers=self._headers, timeout=60.0
            )
        except httpx2.HTTPError:
            return False
        return response.status_code == OK

    async def open_stream(self, run: str) -> Stream:
        parts = urlsplit(self._base)
        scheme = "wss" if parts.scheme == "https" else "ws"
        try:
            ws = await connect(
                f"{scheme}://{parts.netloc}/ws?{urlencode({'run': run})}",
                origin=Origin(self._base),
                additional_headers=self._headers,
                open_timeout=120,
            )
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise DrillError(f"WebSocket: {type(exc).__name__}") from None
        return _Socket(ws)


def requests_filter(services: Iterable[str], since: float) -> str:
    """Cloud Run request log lines of ``services`` since ``since``."""
    names = " OR ".join(f'"{s}"' for s in services)
    return (
        f"{SERVICE_RESOURCE} AND resource.labels.service_name=({names}) "
        f'AND log_id("{REQUESTS_LOG}") AND timestamp>="{stamp(since)}"'
    )


def leg_filter(service: str, run: str, since: float) -> str:
    """The drill app's own end-of-leg lines for ``run``."""
    return (
        f'{SERVICE_RESOURCE} AND resource.labels.service_name="{service}" '
        f'AND jsonPayload.drill.run="{run}" AND jsonPayload.drill.event="end" '
        f'AND timestamp>="{stamp(since - LOG_CLOCK_MARGIN_SECONDS)}"'
    )


def started_filter(service: str, since: float) -> str:
    """The drill app's ``ready`` lines, one per instance process, since ``since``."""
    return (
        f'{SERVICE_RESOURCE} AND resource.labels.service_name="{service}" '
        f'AND jsonPayload.drill.event="ready" AND timestamp>="{stamp(since)}"'
    )


def datagw_filter(env_id: str, since: float) -> str:
    """The data gateway's ``ready`` lines and this environment's query lines since ``since``."""
    return (
        f'{SERVICE_RESOURCE} AND resource.labels.service_name="{DATAGW_SERVICE}" '
        f'AND (textPayload:"datagw ready" '
        f'OR (textPayload:"datagw query" AND textPayload:"{env_id}")) '
        f'AND timestamp>="{stamp(since)}"'
    )


def proxy_filter(env_id: str, since: float) -> str:
    """The egress proxy machine's lines that name this environment's credential."""
    return f'resource.type="gce_instance" AND "{env_id}" AND timestamp>="{stamp(since)}"'


@dataclass(frozen=True, slots=True)
class Observed:
    """One run, in seconds from the command. ``None`` is not measured. ``nothing_started`` is the
    asleep state's proof, ``proxy_logged`` the awake state's check that the proxy's log is
    searchable (which the asleep proof relies on)."""

    state: str
    door: float | None = None
    door_status: int | None = None
    stream: float | None = None
    query: float | None = None
    tunnel: float | None = None
    steps: Mapping[str, StepTime] = field(default_factory=dict[str, StepTime])
    latest: float | None = None
    nothing_started: bool | None = None
    proxy_logged: bool | None = None
    problems: tuple[str, ...] = ()

    @property
    def instance_stop(self) -> float | None:
        return _seconds(self.steps.get("scale_to_zero"))

    @property
    def timer_pause(self) -> float | None:
        return _seconds(self.steps.get("pause_timers"))


def _seconds(step: StepTime | None) -> float | None:
    return None if step is None else step.seconds


def judge(run: Observed) -> list[str]:
    """Why ``run`` is not a pass: the faults the drill found, then each measure that is missing
    or over its limit."""
    problems = list(run.problems)
    measures: dict[str, float | None] = {
        "front door denial": run.door,
        **(
            {"stream cut": run.stream, "query end": run.query, "tunnel close": run.tunnel}
            if run.state == "awake"
            else {}
        ),
    }
    for name, value in measures.items():
        limit = DOOR_LIMIT_SECONDS if name == "front door denial" else ALL_LIMIT_SECONDS
        if value is None:
            problems.append(f"{name} not seen")
        elif value >= limit:
            problems.append(f"{name} took {value:.1f} s (limit {limit:.0f} s)")
    for name in STEPS:
        step = run.steps.get(name)
        if step is None or step.state != "done" or step.seconds is None:
            problems.append(f"step {name} {'missing' if step is None else step.state}")
        elif step.seconds >= ALL_LIMIT_SECONDS:
            problems.append(
                f"step {name} took {step.seconds:.1f} s (limit {ALL_LIMIT_SECONDS:.0f} s)"
            )
    if run.state == "asleep" and run.nothing_started is not True:
        problems.append("nothing started is not proved")
    return problems


def _median_max(values: Sequence[float | None]) -> str:
    found = [v for v in values if v is not None]
    if not found or len(found) != len(values):
        return "missing"
    return f"{statistics.median(found):.1f} / {max(found):.1f}"


@dataclass(frozen=True, slots=True)
class Report:
    runs: Sequence[Observed]

    def of(self, state: str) -> list[Observed]:
        return [r for r in self.runs if r.state == state]

    @property
    def longest_stream(self) -> float | None:
        """The longest an open stream survived the command, over the awake runs."""
        seen = [r.stream for r in self.of("awake") if r.stream is not None]
        return max(seen, default=None)

    @property
    def failures(self) -> list[str]:
        out = [
            f"{r.state} run {i}: {problem}"
            for state in STATES
            for i, r in enumerate(self.of(state), 1)
            for problem in judge(r)
        ]
        out += [f"{state}: no runs" for state in STATES if not self.of(state)]
        awake = self.of("awake")
        if self.of("asleep") and not all(r.proxy_logged for r in awake):
            out.append(
                "asleep: the proxy's log was not seen in every awake run, so 'nothing started' "
                "is not proved for the proxy"
            )
        return out

    def result(self) -> ev.Result:
        """The drill's line on the nightly page: pass, or fail with the first problem."""
        if self.failures:
            more = len(self.failures) - 1
            first = self.failures[0] + (f" (and {more} more)" if more else "")
            return ev.Result(matrix.DRILL, ev.FAIL, first)
        runs = len(self.of("awake"))
        longest = self.longest_stream
        stream = "no stream open" if longest is None else f"longest stream {longest:.1f} s"
        return ev.Result(matrix.DRILL, ev.OK, f"{runs} runs per state, {stream}")

    def markdown(self) -> str:
        """The results table: a row per state, ``median / max`` seconds in each cell."""
        lines = [
            "| State | Runs | Front door | Stream cut | Query end | Tunnel close "
            "| Instance stop | Timer pause | latest.json moved |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for state in STATES:
            runs = self.of(state)
            awake = state == "awake"
            proved = bool(runs) and all(r.nothing_started for r in runs)
            idle = "nothing started" if proved else "not proved"
            cells = [
                _median_max([r.door for r in runs]),
                _median_max([r.stream for r in runs]) if awake else "none open",
                _median_max([r.query for r in runs]) if awake else idle,
                _median_max([r.tunnel for r in runs]) if awake else idle,
                _median_max([r.instance_stop for r in runs]),
                _median_max([r.timer_pause for r in runs]),
                _median_max([r.latest for r in runs]),
            ]
            lines.append(f"| {state} | {len(runs)} | " + " | ".join(cells) + " |")
        longest = self.longest_stream
        lines += [
            "",
            "Seconds from the command, median / max.",
            "Longest an open stream survived: "
            + ("not seen" if longest is None else f"{longest:.1f} s"),
            f"Verdict: {'FAIL' if self.failures else 'PASS'} "
            f"(front door under {DOOR_LIMIT_SECONDS:.0f} s, everything under "
            f"{ALL_LIMIT_SECONDS:.0f} s, both states)",
        ]
        lines += ["", *(f"- FAILED {f}" for f in self.failures)] if self.failures else []
        return "\n".join(lines) + "\n"


class Drill:
    """Runs the drill in both states against one app."""

    def __init__(  # noqa: PLR0913  (keyword-only)
        self,
        *,
        cfg: DrillConfig,
        api: ControlPlane,
        cloud: CloudApi,
        front: Front,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = cfg
        self._api = api
        self._cloud = cloud
        self._front = front
        self._sleep = sleep
        self._clock = clock
        self._wall = wall
        self._app_service = service_name(cfg.env_id)

    async def execute(self) -> Report:
        """``runs`` awake runs, then ``runs`` asleep runs. A run that cannot be taken ends its
        state and is recorded as a failure."""
        observed: list[Observed] = []
        for state, take in (("awake", self.awake), ("asleep", self.asleep)):
            for number in range(1, self._cfg.runs + 1):
                log.info("kill drill run", extra={"state": state, "number": number})
                try:
                    observed.append(await take())
                except DrillError as exc:
                    observed.append(Observed(state, problems=(f"run not taken: {exc}",)))
                    await self._enable_quietly()
                    break
        return Report(observed)

    async def awake(self) -> Observed:
        """One run with a WebSocket, a query and a tunnel open when the kill is pulled."""
        run = secrets.token_hex(6)
        await self._ready()
        stream = await self._front.open_stream(run)
        watchers: list[asyncio.Task[Any]] = []
        try:
            if not await self._front.start(run):
                raise DrillError("the drill app did not start its query and tunnel")
            if stream.closed:
                raise DrillError("the WebSocket closed before the command")
            wall0, t0 = self._wall(), self._clock()
            refusal = asyncio.create_task(self._refusal(t0))
            cut = asyncio.create_task(self._ended(stream))
            watchers += [refusal, cut]
            run_id = await self._api.kill(self._cfg.app_id)
            final = await self._follow(run_id)
            refused, status = await refusal
            end = await cut
        finally:
            for watcher in watchers:
                watcher.cancel()
            await stream.aclose()
        steps = await self._api.steps(run_id)
        legs, proxy_logged = await self._leg_ends(run, wall0)
        problems = [] if final.get("state") == "completed" else [f"run {final.get('state')}"]
        problems += [
            f"{leg} was not running at the kill ({_obj(legs[leg]).get('outcome')})"
            for leg in LEG_ENDS
            if leg in legs and not _obj(legs[leg]).get("running")
        ]
        problems += [f"no end line for {leg}" for leg in LEG_ENDS if leg not in legs]
        updated = await self._cloud.object_updated(
            f"{self._cfg.project}-cell", f"snapshots/{self._cfg.org_id}/latest.json"
        )
        await self._api.enable(self._cfg.app_id)
        return Observed(
            "awake",
            door=refused,
            door_status=status,
            stream=None if end is None else end - t0,
            query=_since(legs.get("query"), wall0),
            tunnel=_since(legs.get("tunnel"), wall0),
            steps=steps,
            latest=None if updated is None else updated - wall0,
            proxy_logged=proxy_logged,
            problems=tuple(problems),
        )

    async def asleep(self) -> Observed:
        """One run with the app, gateway and data gateway at zero: the kill, then one request."""
        await self._wait_quiet()
        wall0, t0 = self._wall(), self._clock()
        run_id = await self._api.kill(self._cfg.app_id)
        await self._follow(run_id, until_step=STEPS[0])
        status = await self._front.health(COLD_HEALTH_TIMEOUT_SECONDS)
        door = self._clock() - t0
        final = await self._follow(run_id)
        steps = await self._api.steps(run_id)
        await self._sleep(SETTLE_SECONDS)
        problems = await self._started_anywhere(wall0)
        if status in (None, OK):
            problems.append(f"the request after the kill was not refused ({status})")
        if final.get("state") != "completed":
            problems.append(f"kill switch run {final.get('state')}")
        await self._api.enable(self._cfg.app_id)
        return Observed(
            "asleep",
            door=None if status in (None, OK) else door,
            door_status=status,
            steps=steps,
            nothing_started=not problems,
            problems=tuple(problems),
        )

    async def _ready(self) -> None:
        """Enables the app and waits until its ``/health`` answers 200."""
        await self._api.enable(self._cfg.app_id)
        started = self._clock()
        while await self._front.health(COLD_HEALTH_TIMEOUT_SECONDS) != OK:
            if self._clock() - started > AWAKE_LIMIT_SECONDS:
                raise DrillError("the drill app did not answer /health after enabling")
            await self._sleep(AWAKE_POLL_SECONDS)

    async def _enable_quietly(self) -> None:
        try:
            await self._api.enable(self._cfg.app_id)
        except DrillError as exc:
            log.warning("could not enable the drill app: %s", exc)

    async def _ended(self, stream: Stream) -> float | None:
        try:
            await asyncio.wait_for(stream.drain(), WATCH_SECONDS)
        except TimeoutError:
            return None
        return self._clock()

    async def _refusal(self, t0: float) -> tuple[float | None, int | None]:
        """Asks ``/health`` until an answer is not 200: seconds from ``t0`` and its status."""
        while self._clock() - t0 < WATCH_SECONDS:
            status = await self._front.health()
            if status is not None and status != OK:
                return self._clock() - t0, status
            await self._sleep(DOOR_POLL_SECONDS)
        return None, None

    async def _follow(self, run_id: str, *, until_step: str | None = None) -> Json:
        """Polls the kill switch run until it ends, or until ``until_step`` has finished."""
        started = self._clock()
        while True:
            body = await self._api.run(self._cfg.app_id, run_id)
            done = [
                s
                for s in _objs(body.get("steps"))
                if s.get("name") == until_step and s.get("state") != "running"
            ]
            if body.get("state") != "running" or done:
                return body
            if self._clock() - started > FOLLOW_LIMIT_SECONDS:
                raise DrillError(f"kill switch run {run_id} still running")
            await self._sleep(FOLLOW_POLL_SECONDS)

    async def _leg_ends(self, run: str, since: float) -> tuple[dict[str, Json], bool]:
        """The drill app's end-of-leg lines for the query and the tunnel, and whether the proxy's
        own log named the environment, once all three are in Cloud Logging or the wait is up."""
        started = self._clock()
        while True:
            lines = await self._cloud.entries(leg_filter(self._app_service, run, since))
            legs = {
                str(p["leg"]): p
                for p in (_obj(_obj(e.get("jsonPayload")).get("drill")) for e in lines)
                if p.get("leg") in LEG_ENDS
            }
            proxy = bool(
                await self._cloud.entries(
                    proxy_filter(self._cfg.env_id, since - PROXY_LOOKBACK_SECONDS), one_page=True
                )
            )
            if (
                len(legs) == len(LEG_ENDS) and proxy
            ) or self._clock() - started > LOGS_LIMIT_SECONDS:
                return legs, proxy
            await self._sleep(LOGS_POLL_SECONDS)

    async def _wait_quiet(self) -> None:
        """Waits until the app, the gateway and the data gateway have had no request for 26
        minutes, by the Cloud Run request log: an idle request-billed service is at zero."""
        services = (self._app_service, GATEWAY_SERVICE, DATAGW_SERVICE)
        started = self._clock()
        while True:
            now = self._wall()
            newest = await self._cloud.entries(
                requests_filter(services, now - ASLEEP_AFTER_SECONDS),
                newest_first=True,
                one_page=True,
            )
            last = epoch_of(newest[0].get("timestamp")) if newest else None
            if last is None:
                return
            if self._clock() - started > QUIET_LIMIT_SECONDS:
                raise DrillError("the cell did not go quiet")
            await self._sleep(max(last + ASLEEP_AFTER_SECONDS - now, 1.0) + 1.0)

    async def _started_anywhere(self, since: float) -> list[str]:
        """What the logs show started since ``since`` in the asleep state; empty is the proof.
        The gateway's own request line for the refused request must be there, or the logs are not
        being read and prove nothing."""
        found = {
            "the app received a request": requests_filter([self._app_service], since),
            "an app instance started": started_filter(self._app_service, since),
            "the data gateway started or ran a query": datagw_filter(self._cfg.env_id, since),
            "the proxy logged the environment": proxy_filter(self._cfg.env_id, since),
        }
        problems = [
            what for what, query in found.items() if await self._cloud.entries(query, one_page=True)
        ]
        if not await self._cloud.entries(requests_filter([GATEWAY_SERVICE], since), one_page=True):
            problems.append("the gateway's own log shows no request, so the logs prove nothing")
        return problems


def _since(leg: Json | None, wall0: float) -> float | None:
    at = _obj(leg).get("at")
    return float(at) - wall0 if isinstance(at, int | float) else None


async def main_async(environ: Mapping[str, str]) -> Report:
    cfg = config_from_env(environ)
    sign_in = None
    if cfg.credentials_file:
        credentials = load_credentials(Path(cfg.credentials_file))
        sign_in, token, cookie = SignIn(credentials), None, credentials.cookie
    else:
        token, cookie = cfg.token, cfg.cookie
    api = ControlPlane(cfg.api_url, sign_in or str(token))
    cloud = CloudApi(cfg.project, google_access_tokens(environ))
    front = FrontDoor(f"https://{cfg.host}", str(cookie))
    try:
        return await Drill(cfg=cfg, api=api, cloud=cloud, front=front).execute()
    finally:
        await api.aclose()
        await cloud.aclose()
        await front.aclose()
        if sign_in is not None:
            await sign_in.aclose()


def write_evidence(environ: Mapping[str, str], result: ev.Result) -> None:
    """Adds the drill's line to ``SSC_EVIDENCE_FILE`` for the cell ``SSC_DRILL_PROJECT`` names."""
    path, project = environ.get(ev.EVIDENCE_ENV), environ.get(ENV["project"])
    if path and project:
        ev.write(Path(path), ev.Evidence(project, peer=False, results=(result,)))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        report = asyncio.run(main_async(os.environ))
    except DrillError as exc:
        sys.stderr.write(f"kill_drill: {exc}\n")
        write_evidence(os.environ, ev.Result(matrix.DRILL, ev.FAIL, str(exc)))
        return 1
    write_evidence(os.environ, report.result())
    text = report.markdown()
    sys.stdout.write(text)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as f:
            f.write("## SSC-054 kill switch drill\n\n" + text)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
