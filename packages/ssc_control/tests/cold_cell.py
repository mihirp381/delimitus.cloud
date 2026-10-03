"""A cell at zero on this machine, for timer runs through the gateway (SSC-041). Imported as
``cold_cell``.

The pieces are those of ``ssc_edge``'s Envoy tests: the gateway's authorisation service in this
process, real Envoy and an app in Docker, the app under the Cloud Run host name the gateway
computes. In front of them, a stand-in for the cell's load balancer, which is always up: it
accepts a connection at once and holds it while the gateway starts, as Cloud Run holds a request
while an instance starts. The gateway is at zero: Envoy is started only when the first connection
arrives, ``gateway_delay`` seconds later, and the snapshot is loaded on its first check. The app
is at zero: it holds its first request ``app_delay`` seconds before answering. Every request the
app answers is written to :meth:`ColdCell.seen`. Without Docker the caller skips locally and fails
in CI.
"""

import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx2
import pytest
import uvicorn

from ssc_app.identity import verify
from ssc_contracts.identity import IdentityNote
from ssc_contracts.snapshot import FORMAT_V1
from ssc_edge.envoy import ENVOY_VERSION, EnvoyConfig, render
from ssc_edge.gate import GateConfig, upstream_host
from ssc_edge.identity_note import jwks
from ssc_edge.keys import Keyring, new_keyring, parse_keyring
from ssc_edge.schedule_token import parse_timer_jwks
from ssc_edge.server import create_app, gate_for
from ssc_shared.access import AccessView

ENVOY_IMAGE: Final = (
    f"envoyproxy/envoy:v{ENVOY_VERSION}"
    "@sha256:d59f7f5fa10cff6d5892b6c5e7df5c9297ddfb2c3683e33fbfb82da24de4fa66"
)
APP_IMAGE: Final = (
    "python:3.11.15-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff"
)
DOMAIN: Final = "apps.test"
PROJECT_NUMBER: Final = "123456789012"
REGION: Final = "us-central1"
READY_SECONDS: Final = 60.0
APP: Final = """
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DELAY = float(os.environ["APP_DELAY"])
first = threading.Lock()
started = []

class App(BaseHTTPRequestHandler):
    def _go(self):
        with first:
            if not started:
                time.sleep(DELAY)
                started.append(True)
        self.rfile.read(int(self.headers.get("content-length") or 0))
        seen = {k.lower(): v for k, v in self.headers.items()}
        with open("/out/seen.jsonl", "a") as out:
            out.write(json.dumps({"method": self.command, "path": self.path, "headers": seen}))
            out.write("\\n")
        self.send_response(204)
        self.end_headers()
    do_GET = do_POST = _go
    def log_message(self, *a): pass

ThreadingHTTPServer(("0.0.0.0", 80), App).serve_forever()
"""


def docker() -> str:
    found = shutil.which("docker")
    if (
        found is None
        or subprocess.run([found, "info"], capture_output=True, check=False).returncode
    ):
        if os.environ.get("CI"):
            pytest.fail("Docker is required in CI for the cold timer test")
        pytest.skip("Docker is not available")
    return found


def _run(*args: str) -> str:
    out = subprocess.run([docker(), *args], capture_output=True, text=True, check=False)
    if out.returncode:
        raise AssertionError(f"docker {args[0]} failed: {out.stderr[-2000:]}")
    return out.stdout.strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _published(name: str, port: str) -> int:
    return int(_run("port", name, port).splitlines()[0].rsplit(":", 1)[1])


def _serve(server: uvicorn.Server) -> threading.Thread:
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    return thread


def _remove(tag: str) -> None:
    subprocess.run(
        [docker(), "rm", "-f", f"{tag}-envoy", f"{tag}-app"], capture_output=True, check=False
    )
    subprocess.run([docker(), "network", "rm", tag], capture_output=True, check=False)


class ToLoadBalancer(httpx2.AsyncBaseTransport):
    """Sends every request to the load balancer stand-in, keeping its ``Host``: what DNS and
    TLS do for the real cell."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._inner = httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self._port)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class ColdCell:
    port: int
    host: str
    keyring: Keyring
    issuer: str
    out: Path
    woken_at: float | None = None
    snapshot_loads: list[float] = field(default_factory=list)

    def transport(self) -> ToLoadBalancer:
        return ToLoadBalancer(self.port)

    def seen(self) -> list[dict[str, Any]]:
        """The requests the app answered, in order."""
        log = self.out / "seen.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def note(self, token: str) -> IdentityNote:
        keys = jwks((self.keyring.signing_key.public_key(), self.keyring.identity_kid))
        return verify(token, audience=f"https://{self.host}", keys=keys, issuer=self.issuer)


def _snapshot(org_id: str, app_id: str, environment_id: str, slug: str) -> dict[str, Any]:
    return {
        "format": FORMAT_V1,
        "org_id": org_id,
        "version": 1,
        "compiled_at": "2026-10-03T12:00:00Z",
        "environments": {
            environment_id: {"app_id": app_id, "name": "prod", "floor": "user", "status": "active"}
        },
        "hosts": {slug: environment_id},
        "grants": {environment_id: []},
        "groups_by_user": {},
        "users": {},
        "ceiling": None,
    }


async def _splice(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        with suppress(Exception):
            writer.close()


async def _ready(port: int, host: str) -> None:
    deadline = time.monotonic() + READY_SECONDS
    async with httpx2.AsyncClient(timeout=1) as client:
        while True:
            try:
                await client.get(f"http://127.0.0.1:{port}/", headers={"host": host})
                return
            except httpx2.HTTPError:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.1)


@asynccontextmanager
async def cold_cell(  # noqa: PLR0913, PLR0915  (keyword-only; one stack, started in order)
    *,
    org_id: str,
    cell_label: str,
    app_id: str,
    environment_id: str,
    slug: str,
    timer_jwks: str,
    workdir: Path,
    gateway_delay: float,
    app_delay: float,
) -> AsyncIterator[ColdCell]:
    """The cell, its app's prod environment at ``https://<slug>.<cell_label>.apps.test``."""
    docker()
    keyring = parse_keyring(new_keyring())
    issuer = f"https://keys.example.test/{cell_label}"
    held = AccessView.from_document(_snapshot(org_id, app_id, environment_id, slug))
    view: list[AccessView] = []
    cell = ColdCell(
        port=0,
        host=f"{slug}.{cell_label}.{DOMAIN}",
        keyring=keyring,
        issuer=issuer,
        out=workdir / "out",
    )

    async def refresh() -> None:
        if not view:
            cell.snapshot_loads.append(time.monotonic())
            view.append(held)

    gate = gate_for(
        GateConfig(
            org_id=org_id,
            cell_label=cell_label,
            apps_domain=DOMAIN,
            auth_url="https://auth.example.test",
            issuer=issuer,
            project_number=PROJECT_NUMBER,
            region=REGION,
            max_body_bytes=1024,
        ),
        keyring,
        view=lambda: view[0] if view else None,
        refresh=refresh,
        timer_keys=parse_timer_jwks(timer_jwks),
    )
    authz_port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(lambda: gate),
            host="0.0.0.0",  # noqa: S104  (Envoy in Docker reaches it)
            port=authz_port,
            log_level="warning",
        )
    )
    thread = await asyncio.to_thread(_serve, server)

    conf = workdir / "conf"
    conf.mkdir()
    cell.out.mkdir()
    cell.out.chmod(0o777)
    (conf / "app.py").write_text(APP)
    envoy_cfg = EnvoyConfig(
        authz_host="host.docker.internal",
        authz_port=authz_port,
        stream_host="host.docker.internal",
        stream_port=_free_port(),
        upstream_tls=False,
    )
    (conf / "envoy.json").write_text(json.dumps(render(envoy_cfg)))
    tag = "ssc041-" + secrets.token_hex(4)
    upstream = upstream_host(environment_id, project_number=PROJECT_NUMBER, region=REGION)
    lock = asyncio.Lock()
    backend: list[int] = []

    async def wake() -> int:
        cell.woken_at = time.monotonic()
        await asyncio.sleep(gateway_delay)
        await asyncio.to_thread(
            _run,
            "run", "-d", "--rm", "--name", f"{tag}-envoy", "--network", tag,
            "--add-host", "host.docker.internal:host-gateway", "-p", "127.0.0.1::8080",
            "-v", f"{conf}:/c:ro", ENVOY_IMAGE, "-c", "/c/envoy.json", "--log-level", "warn",
        )  # fmt: skip
        port = await asyncio.to_thread(_published, f"{tag}-envoy", "8080/tcp")
        await _ready(port, f"nothere.{cell_label}.{DOMAIN}")
        return port

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with lock:
            if not backend:
                backend.append(await wake())
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", backend[0])
        await asyncio.gather(_splice(reader, up_writer), _splice(up_reader, writer))

    _run("network", "create", tag)
    balancer = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        _run(
            "run", "-d", "--rm", "--name", f"{tag}-app", "--network", tag,
            "--network-alias", upstream, "-e", f"APP_DELAY={app_delay}",
            "-v", f"{conf}:/c:ro", "-v", f"{cell.out}:/out", APP_IMAGE, "python", "/c/app.py",
        )  # fmt: skip
        cell.port = int(balancer.sockets[0].getsockname()[1])
        yield cell
    finally:
        balancer.close()
        await asyncio.to_thread(_remove, tag)
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
