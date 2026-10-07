"""The drill app of SSC-054 (``docs/kill-switch-drill.md``). Deployed with ``ssc deploy``; the
drill (``ssc_conformance.kill_drill``) drives it through the cell's public load balancer.

- ``/health``: 200.
- ``/ws?run=``: a WebSocket that sends a tick a second until something ends it.
- ``/drip?run=``: a plain HTTP answer, not a stream to the browser's eyes, that sends a line a
  second until something ends it. The gateway must cut it too (SSC-021).
- ``/start?run=``: starts a long query through the data gateway (``SELECT pg_sleep(25)`` in a
  loop, on the connection ``drill-db``) and a tunnel through the egress proxy (held open with a
  ``GET /rate_limit`` every 20 s), and answers 200 once both are running.

Every leg logs one JSON line when it ends, under ``drill`` with the run, the leg, the time ``at``
(seconds since the epoch), the ``outcome`` and whether the leg ``running`` was cut (a query
refused before it started, or a tunnel that never opened, is not). One ``ready`` line is logged
when the process starts, so a new instance can be told from an old one. Nothing secret is
logged: not the proxy address, which carries the app's credential, and not a token.
"""

import asyncio
import base64
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Callable
from typing import Any, Final

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

CONNECTION: Final = "drill-db"
QUERY_SQL: Final = "SELECT pg_sleep(25)"
WARM_SQL: Final = "SELECT 1"
QUERY_TIMEOUT_MS: Final = 30_000
QUERY_WAIT_SECONDS: Final = 45.0
TUNNEL_HOST: Final = "api.github.com"
TUNNEL_PORT: Final = 443
KEEPALIVE_SECONDS: Final = 20.0
KEEPALIVE: Final = (
    f"GET /rate_limit HTTP/1.1\r\nHost: {TUNNEL_HOST}\r\nUser-Agent: ssc-kill-drill\r\n"
    "Accept: application/json\r\n\r\n"
).encode()
LEG_MAX_SECONDS: Final = 240.0
TICK_SECONDS: Final = 1.0
START_WAIT_SECONDS: Final = 30.0
METADATA: Final = "http://metadata.google.internal/computeMetadata/v1"
RUN_PATTERN: Final = "^[a-z0-9]{1,32}$"
URL_VARIABLE: Final = "SSC_DATAGW_URL"
PROXY_VARIABLE: Final = "HTTPS_PROXY"
HTTP_OK: Final = 200


def emit(run: str, leg: str, event: str, **fields: object) -> None:
    """Writes one structured log line, which Cloud Run turns into ``jsonPayload``."""
    line = {
        "severity": "INFO",
        "message": f"kill drill {leg} {event}",
        "drill": {"run": run, "leg": leg, "event": event, "at": time.time(), **fields},
    }
    sys.stdout.write(json.dumps(line) + "\n")
    sys.stdout.flush()


def _get(url: str, headers: dict[str, str]) -> str:
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    with urllib.request.urlopen(request, timeout=5.0) as response:  # noqa: S310
        return response.read().decode().strip()


def datagw_url() -> str:
    """The cell's data gateway: ``SSC_DATAGW_URL``, else ``ssc-datagw`` in this project and region
    as the metadata server names them."""
    if url := os.environ.get(URL_VARIABLE):
        return url
    parts = _get(f"{METADATA}/instance/region", {"Metadata-Flavor": "Google"}).split("/")
    return f"https://ssc-datagw-{parts[1]}.{parts[3]}.run.app"


def workload_token(audience: str) -> str:
    """The app's own ID token for ``audience``, asked for with ``format=full``."""
    query = urllib.parse.urlencode({"audience": audience, "format": "full"})
    url = f"{METADATA}/instance/service-accounts/default/identity?{query}"
    return _get(url, {"Metadata-Flavor": "Google"})


def post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    """One POST: the status and body, also for a refusal."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def ask_query(sql: str) -> tuple[str, str | None]:
    """Runs ``sql`` through the data gateway as the app itself: ``("served", None)``, or the
    refusal's code and stage, or ``("UNREACHABLE", None)``."""
    try:
        url = datagw_url()
        headers = {
            "Authorization": f"Bearer {workload_token(url)}",
            "Content-Type": "application/json",
        }
        body = json.dumps({"sql": sql, "timeout_ms": QUERY_TIMEOUT_MS}).encode()
        status, answer = post(
            f"{url}/v1/connections/{CONNECTION}/query", headers, body, QUERY_WAIT_SECONDS
        )
    except OSError, ValueError, IndexError:
        return "UNREACHABLE", None
    if status == HTTP_OK:
        return "served", None
    try:
        error = json.loads(answer).get("error", {})
    except ValueError:
        error = {}
    return str(error.get("code") or f"HTTP_{status}"), error.get("stage")


class Leg:
    """One leg's state: ``up`` once it is running, ``done`` once it has ended."""

    def __init__(self) -> None:
        self.up = threading.Event()
        self.done = threading.Event()


def query_leg(run: str, leg: Leg) -> None:
    """Warms the data gateway, then runs the long query again and again until one is refused or
    cut. The first refusal is the end: ``running`` when it came while the query ran."""
    outcome, stage = ask_query(WARM_SQL)
    if outcome == "served":
        leg.up.set()
        deadline = time.monotonic() + LEG_MAX_SECONDS
        while outcome == "served" and time.monotonic() < deadline:
            outcome, stage = ask_query(QUERY_SQL)
        outcome = "timeout" if outcome == "served" else outcome
    emit(run, "query", "end", outcome=outcome, stage=stage, running=stage == "execute")
    leg.done.set()


class TunnelRefusedError(Exception):
    """The proxy answered the CONNECT with something but 200."""

    def __init__(self, status: int) -> None:
        super().__init__(f"proxy answered {status}")
        self.status = status


def tls(sock: socket.socket, host: str) -> socket.socket:
    """TLS to ``host`` over ``sock``."""
    return ssl.create_default_context().wrap_socket(sock, server_hostname=host)


def open_tunnel(
    proxy_url: str, wrap: Callable[[socket.socket, str], socket.socket] = tls
) -> socket.socket:
    """A tunnel to ``TUNNEL_HOST`` through the egress proxy, with the app's credential."""
    parts = urllib.parse.urlsplit(proxy_url)
    login = (
        f"{urllib.parse.unquote(parts.username or '')}:{urllib.parse.unquote(parts.password or '')}"
    )
    auth = base64.b64encode(login.encode()).decode()
    sock = socket.create_connection((parts.hostname or "", parts.port or 3128), timeout=10.0)
    authority = f"{TUNNEL_HOST}:{TUNNEL_PORT}"
    sock.sendall(
        f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
        f"Proxy-Authorization: Basic {auth}\r\n\r\n".encode()
    )
    head = b""
    while b"\r\n\r\n" not in head and len(head) < 8192:
        chunk = sock.recv(1024)
        if not chunk:
            break
        head += chunk
    fields = head.split(b" ", 2)
    status = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0
    if status != HTTP_OK:
        sock.close()
        raise TunnelRefusedError(status)
    return wrap(sock, TUNNEL_HOST)


def tunnel_leg(
    run: str,
    leg: Leg,
    keepalive: float = KEEPALIVE_SECONDS,
    wrap: Callable[[socket.socket, str], socket.socket] = tls,
) -> None:
    """Opens the tunnel and holds it, asking a question every ``keepalive`` seconds so the far
    side does not close it. The end is the first read that finds it closed."""
    proxy = os.environ.get(PROXY_VARIABLE, "")
    try:
        sock = open_tunnel(proxy, wrap)
    except (OSError, ValueError, TunnelRefusedError) as exc:
        reason = (
            f"proxy_{exc.status}" if isinstance(exc, TunnelRefusedError) else type(exc).__name__
        )
        emit(run, "tunnel", "end", outcome=reason, running=False)
        leg.done.set()
        return
    sock.settimeout(keepalive)
    leg.up.set()
    deadline = time.monotonic() + LEG_MAX_SECONDS
    outcome = "timeout"
    running = False
    while time.monotonic() < deadline:
        try:
            sock.sendall(KEEPALIVE)
            while sock.recv(65536):
                continue
            outcome, running = "closed", True
            break
        except TimeoutError:
            continue
        except OSError as exc:
            outcome, running = type(exc).__name__, True
            break
    sock.close()
    emit(run, "tunnel", "end", outcome=outcome, running=running)
    leg.done.set()


LEGS: dict[str, Leg] = {}
app = FastAPI()
emit("", "app", "ready")


@app.get("/")
@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.websocket("/ws")
async def ticks(ws: WebSocket, run: str = Query("", pattern="^[a-z0-9]{0,32}$")) -> None:
    """Sends a tick a second; logs when the stream ends and why."""
    await ws.accept()
    emit(run, "ws", "start")
    reason = "stopped"
    n = 0
    try:
        while True:
            n += 1
            await ws.send_text(json.dumps({"tick": n, "at": time.time()}))
            await asyncio.sleep(TICK_SECONDS)
    except WebSocketDisconnect, RuntimeError:
        reason = "disconnected"
    finally:
        emit(run, "ws", "end", outcome=reason, ticks=n)


@app.get("/drip")
async def drip(run: str = Query("", pattern="^[a-z0-9]{0,32}$")) -> StreamingResponse:
    """A plain answer that sends a line a second; logs when it ends."""

    async def lines() -> AsyncIterator[bytes]:
        emit(run, "drip", "start")
        n = 0
        try:
            while True:
                n += 1
                yield f"{n}\n".encode()
                await asyncio.sleep(TICK_SECONDS)
        finally:
            emit(run, "drip", "end", outcome="stopped", ticks=n)

    return StreamingResponse(lines(), media_type="text/plain")


@app.get("/start")
def start(run: str = Query(pattern=RUN_PATTERN)) -> JSONResponse:
    """Starts the query and the tunnel for ``run``; 200 once both are running, else 503 naming
    the leg that is not."""
    LEGS.clear()
    for name, target in (("query", query_leg), ("tunnel", tunnel_leg)):
        LEGS[name] = Leg()
        threading.Thread(target=target, args=(run, LEGS[name]), daemon=True).start()
    deadline = time.monotonic() + START_WAIT_SECONDS
    while time.monotonic() < deadline and not all(
        leg.up.is_set() or leg.done.is_set() for leg in LEGS.values()
    ):
        time.sleep(0.05)
    state: dict[str, Any] = {name: leg.up.is_set() for name, leg in LEGS.items()}
    return JSONResponse(state, status_code=HTTP_OK if all(state.values()) else 503)
