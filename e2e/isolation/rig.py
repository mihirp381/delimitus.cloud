"""The fast target of the browser isolation suite (SSC-029).

``fast`` stands up one cell on this machine the way the Envoy tests of ``ssc_edge`` do: the
gateway's authorisation service and stream relay in this process, real Envoy in Docker, and the
test app (``apps/server.mjs``) twice, as apps A (``alpha``) and B (``bravo``), which Envoy reaches
under the ``run.app`` names the gateway computes. Around them, what a browser needs and Docker does
not give: a TLS front with a throwaway certificate (the load balancer's stand-in), an auth host
stand-in that hands out one-time login codes and answers ``/internal/redeem`` the way the auth host
does (and, for the nightly sign-in of SSC-056, runs the device flow and an identity provider's
two-step sign-in form), and a CONNECT proxy that sends the cell's host names and the auth host to
those two. The relay cuts every stream at its environment's limit, as Cloud Run ends a request;
bravo's preview environment has a limit of ``--limit`` seconds, the others the request-billed 300.

It prints one JSON line of ``SSC_ISO_*`` (and ``SSC_NIGHT_*``) settings for the suite, then runs
until standard input closes or a signal arrives. On ``SSC_ISO_CONTROL``, ``POST /seal {"host",
"user"}`` answers ``{"value"}``, a session cookie value for that host; ``POST /authoriser {"on"}``
stops or starts the authorisation service as Envoy sees it, and ``POST /snapshot {"on"}`` makes
the snapshot too old to use or fresh again.

    uv run python e2e/isolation/rig.py fast --limit 6
"""

import argparse
import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Generator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ssc_contracts.snapshot import FORMAT_V1
from ssc_edge.envoy import ENVOY_VERSION, EnvoyConfig, render
from ssc_edge.gate import GateConfig, binding_of, upstream_host
from ssc_edge.keys import Keyring, new_keyring, parse_keyring
from ssc_edge.redeemer import HttpRedeemer
from ssc_edge.server import create_app, gate_for
from ssc_edge.session import Session, SessionCodec, new_sid
from ssc_edge.streams import Streams
from ssc_shared.access import AccessView
from ssc_shared.runtime import REQUEST_TIMEOUT_SECONDS

HERE: Final = Path(__file__).resolve().parent
ENVOY_IMAGE: Final = (
    f"envoyproxy/envoy:v{ENVOY_VERSION}"
    "@sha256:d59f7f5fa10cff6d5892b6c5e7df5c9297ddfb2c3683e33fbfb82da24de4fa66"
)
PIPE_IMAGE: Final = (
    "python:3.11.15-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff"
)
ANY: Final = "0.0.0.0"  # noqa: S104
LOOPBACK: Final = "127.0.0.1"
ORG: Final = "org_" + "i" * 20
LABEL: Final = "bcdfghjklmnp"
DOMAIN: Final = "apps.test"
BASE: Final = f"{LABEL}.{DOMAIN}"
AUTH_HOST: Final = "auth.ssc.test"
AUTH_URL: Final = f"https://{AUTH_HOST}"
PERSON_COOKIE: Final = "iso-person"
IDP_COOKIE: Final = "iso-idp"
NIGHT_USERNAME: Final = "ada@example.test"
NIGHT_SECOND_FACTOR_USERNAME: Final = "push@example.test"
USER_CODE_ALPHABET: Final = "BCDFGHJKLMNPQRSTVWXZ"
DEVICE_SECONDS: Final = 600
ACCESS_SECONDS: Final = 900
PROJECT_NUMBER: Final = "123456789012"
REGION: Final = "us-central1"
ADA, BEN, CY = ("usr_" + c * 20 for c in "abc")
PEOPLE: Final = {ADA: "Ada", BEN: "Ben", CY: "Cy"}
ALPHA, BRAVO = "app_" + "a" * 20, "app_" + "b" * 20
ALPHA_PROD, BRAVO_PROD, BRAVO_PREVIEW = ("env_" + c * 20 for c in "abc")
DEFAULT_LIMIT: Final = 6
SESSION_SECONDS: Final = 3600
MAX_BODY_BYTES: Final = 1024 * 1024
CHUNK: Final = 64 * 1024
WAIT_SECONDS: Final = 30.0
FORWARDER: Final = """
import asyncio, sys

async def copy(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    writer.close()

async def handle(reader, writer):
    try:
        up = await asyncio.open_connection("host.docker.internal", int(sys.argv[1]))
    except OSError:
        writer.close()
        return
    await asyncio.gather(copy(reader, up[1]), copy(up[0], writer))

async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", 80)
    await server.serve_forever()

asyncio.run(main())
"""


def snapshot(limit: int) -> dict[str, Any]:
    """The cell's one org: A with a prod environment, B with prod and a preview limited to
    ``limit`` seconds a request. Ada may open all three, Ben A and B's prod, Cy (a member)
    nothing."""

    def grant(n: int, user: str) -> dict[str, Any]:
        return {
            "grant_id": f"gnt_{n:020d}",
            "role": "user",
            "subject_kind": "user",
            "subject_id": user,
        }

    env = {"status": "active", "floor": "user"}
    return {
        "format": FORMAT_V1,
        "org_id": ORG,
        "version": 1,
        "compiled_at": "2026-10-03T00:00:00Z",
        "environments": {
            ALPHA_PROD: {"app_id": ALPHA, "name": "prod", **env},
            BRAVO_PROD: {"app_id": BRAVO, "name": "prod", **env},
            BRAVO_PREVIEW: {"app_id": BRAVO, "name": "preview", "timeout_seconds": limit, **env},
        },
        "hosts": {"alpha": ALPHA_PROD, "bravo": BRAVO_PROD, "bravo--preview": BRAVO_PREVIEW},
        "grants": {
            ALPHA_PROD: [grant(1, ADA), grant(2, BEN)],
            BRAVO_PROD: [grant(3, ADA), grant(4, BEN)],
            BRAVO_PREVIEW: [grant(5, ADA)],
        },
        "groups_by_user": {},
        "users": {user: {"status": "active"} for user in PEOPLE},
        "ceiling": None,
    }


def upstream(environment_id: str) -> str:
    return upstream_host(environment_id, project_number=PROJECT_NUMBER, region=REGION)


def sealer_for(keyring: Keyring, org: str) -> Callable[[str, str], str]:
    """Seals a session of ``user`` for ``host``, issued a minute ago and good for an hour."""
    codec = SessionCodec(keyring.session, active=keyring.session_kid)

    def seal(host: str, user: str) -> str:
        iat = int(time.time()) - 60
        session = Session(
            sid=new_sid(),
            sub=user,
            org=org,
            name="Isolation test",
            email="isolation@example.test",
            iat=iat,
            exp=iat + SESSION_SECONDS,
        )
        return codec.seal(session, host)

    return seal


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Issued:
    """A login code the auth host stand-in handed out, and what it may sign in."""

    host: str
    binding: str
    person: str


@dataclass(slots=True)
class DeviceGrant:
    """A device flow the auth host stand-in started: approved once a person signs in for it."""

    user_code: str
    person: str | None = None


def page(title: str, body: str = "", status: int = 200) -> HTMLResponse:
    head = f"<!doctype html><title>{escape(title)}</title><h1>{escape(title)}</h1>"
    return HTMLResponse(head + body, status_code=status)


def unsigned_jwt(claims: Mapping[str, str]) -> str:
    """A token shaped like the auth host's, for the claims a reader looks at; it signs nothing."""

    def part(value: Mapping[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(claims)}.{secrets.token_urlsafe(16)}"


def local_path(value: str) -> str:
    """``value`` if it is a path on this host, else ``/``."""
    return value if value.startswith("/") and not value.startswith("//") else "/"


def auth_app(secret: str, password: str) -> FastAPI:  # noqa: PLR0915  (one host's routes)
    """The auth host's stand-in (SSC-019). Signing in is a cookie on the auth host naming the
    person (``iso-person``), which the suite sets, as a live browser session lets a person skip
    WorkOS. ``/login`` checks the org and that ``return_to`` is an app host of this cell, then
    hands the host a code. ``/internal/redeem`` takes the rig's shared secret; as
    ``ssc_control.identity.sessions.redeem_code`` does, a code is used up by its first
    redemption whatever the outcome, and signs in only on the host it was issued for, with the
    nonce whose hash it carries.

    For the nightly sign-in (SSC-056) it also stands in for WorkOS and Okta: a person with no
    ``iso-person`` cookie is sent to ``/idp``, the identity provider's form in steps (the name,
    the security method, then the password, with Okta Identity Engine's field names), which sets
    ``iso-idp`` as Okta's own session would. ``/device/authorize``, ``/device`` and ``/token``
    run the device flow through it, and ``/callback`` approves the grant, as the real host's
    does."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    codes: dict[str, Issued] = {}
    grants: dict[str, DeviceGrant] = {}
    refresh_tokens: dict[str, str] = {}
    logins = {NIGHT_USERNAME: ADA, NIGHT_SECOND_FACTOR_USERNAME: ADA}

    def signed_in(request: Request) -> str:
        return request.cookies.get(PERSON_COOKIE) or request.cookies.get(IDP_COOKIE) or ""

    def tokens(person: str) -> JSONResponse:
        refresh = secrets.token_urlsafe(32)
        refresh_tokens[refresh] = person
        return JSONResponse(
            {
                "access_token": unsigned_jwt({"sub": person, "org": ORG}),
                "token_type": "Bearer",
                "expires_in": ACCESS_SECONDS,
                "refresh_token": refresh,
            },
            headers={"cache-control": "no-store"},
        )

    @app.post("/device/authorize")
    async def device_authorize(request: Request) -> JSONResponse:
        form = parse_qs((await request.body()).decode())
        if form.get("org") != [ORG]:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        device_code = secrets.token_urlsafe(32)
        user_code = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
        grants[device_code] = DeviceGrant(user_code)
        verify = f"{AUTH_URL}/device?{urlencode({'org': ORG})}"
        return JSONResponse(
            {
                "device_code": device_code,
                "user_code": user_code,
                "verification_uri": verify,
                "verification_uri_complete": f"{verify}&{urlencode({'user_code': user_code})}",
                "expires_in": DEVICE_SECONDS,
                "interval": 1,
            },
            headers={"cache-control": "no-store"},
        )

    @app.get("/device")
    def device_page(request: Request) -> Response:
        code = escape(request.query_params.get("user_code", ""))
        return page(
            "Sign in to the ssc command line",
            "<form method=post action='/device'>"
            f"<input type=hidden name=org value='{ORG}'>"
            f"<label>Code <input name=user_code value='{code}' autocomplete=off required></label> "
            "<button>Continue</button></form>",
        )

    @app.post("/device")
    async def device_continue(request: Request) -> Response:
        if request.headers.get("sec-fetch-site", "same-origin") not in {"same-origin", "none"}:
            return Response(status_code=400)
        form = parse_qs((await request.body()).decode())
        code = form.get("user_code", [""])[0]
        if form.get("org") != [ORG] or not any(g.user_code == code for g in grants.values()):
            return Response(status_code=400)
        back = "/callback?" + urlencode({"flow": "device", "user_code": code})
        return RedirectResponse("/idp?" + urlencode({"next": back}), status_code=302)

    @app.get("/callback")
    def callback(request: Request) -> Response:
        code = request.query_params.get("user_code", "")
        person = request.cookies.get(IDP_COOKIE, "")
        grant = next((g for g in grants.values() if g.user_code == code), None)
        if request.query_params.get("flow") != "device" or grant is None or person not in PEOPLE:
            return page("Sign-in refused", status=400)
        grant.person = person
        return page("You are signed in", "<p>Return to your terminal.</p>")

    @app.get("/idp")
    def idp_page(request: Request) -> Response:
        back = local_path(request.query_params.get("next", "/"))
        if request.cookies.get(IDP_COOKIE) in PEOPLE:
            return RedirectResponse(back, status_code=302)
        return page(
            "Sign in",
            "<form method=post action='/idp'>"
            f"<input type=hidden name=next value='{escape(back)}'>"
            "<input name=identifier autocomplete=off> <input type=submit value=Next></form>",
        )

    def methods(back: str, name: str) -> str:
        """Okta Identity Engine's list of security methods, as org 2's Okta shows it to a person
        with Okta Verify and a password: one Select per method."""
        rows = []
        for se, label in (("okta_verify-totp", "Okta Verify"), ("okta_password", "Password")):
            method = "password" if se == "okta_password" else "okta_verify"
            rows.append(
                "<div class='authenticator-row'><form method=post action='/idp'>"
                f"<input type=hidden name=next value='{escape(back)}'>"
                f"<input type=hidden name=identifier value='{escape(name)}'>"
                f"<input type=hidden name=method value={method}>{label} "
                f"<div class=authenticator-button data-se={se}>"
                "<button data-se=button>Select</button></div></form></div>"
            )
        return f"<div class='authenticator-verify-list authenticator-list'>{''.join(rows)}</div>"

    @app.post("/idp")
    async def idp_answer(request: Request) -> Response:
        form = parse_qs((await request.body()).decode())
        back, name = local_path(form.get("next", ["/"])[0]), form.get("identifier", [""])[0]
        if name not in logins:
            return page("Sign-in refused", status=403)
        typed = form.get("credentials.passcode")
        if typed is None and form.get("method") != ["password"]:
            return page("Verify it's you with a security method", methods(back, name))

        def password_form(refusal: str = "") -> Response:
            return page(
                "Verify",
                "<form method=post action='/idp'>"
                f"{refusal}"
                f"<input type=hidden name=next value='{escape(back)}'>"
                f"<input type=hidden name=identifier value='{escape(name)}'>"
                "<input type=password name=credentials.passcode> <input type=submit value=Verify>"
                "</form>",
            )

        if typed is None:
            return password_form()
        if not hmac.compare_digest(typed[0], password):
            # Okta puts its refusal in an error container above the form, and answers 200.
            return password_form("<div class=o-form-error-container>Authentication failed</div>")
        if name == NIGHT_SECOND_FACTOR_USERNAME:
            # A policy that wants Okta Verify after the password: a page with nothing to type.
            return page(
                "Verify with Okta Verify",
                "<h2>Get a push notification</h2><p>Okta Verify sent a push to your phone.</p>",
            )
        answer = RedirectResponse(back, status_code=303)
        answer.set_cookie(IDP_COOKIE, logins[name], secure=True, httponly=True, samesite="lax")
        return answer

    @app.post("/token")
    async def token(request: Request) -> JSONResponse:
        form = parse_qs((await request.body()).decode())
        kind = form.get("grant_type", [""])[0]
        if kind == "refresh_token":
            person = refresh_tokens.pop(form.get("refresh_token", [""])[0], None)
            if person is None:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return tokens(person)
        device_code = form.get("device_code", [""])[0]
        grant = grants.get(device_code)
        if kind != "urn:ietf:params:oauth:grant-type:device_code" or grant is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if grant.person is None:
            return JSONResponse({"error": "authorization_pending"}, status_code=400)
        del grants[device_code]
        return tokens(grant.person)

    @app.get("/login")
    def login(request: Request) -> Response:
        q = request.query_params
        back = urlsplit(q.get("return_to", ""))
        host, binding = back.hostname or "", q.get("binding", "")
        person = signed_in(request)
        if (
            q.get("org") != ORG
            or back.scheme != "https"
            or not host.endswith("." + BASE)
            or (person and person not in PEOPLE)
            or len(binding) != 43
        ):
            return HTMLResponse("<h1>Sign-in refused</h1>", status_code=403)
        if not person:
            return RedirectResponse(
                "/idp?" + urlencode({"next": str(request.url.path) + "?" + request.url.query}),
                status_code=302,
            )
        code = secrets.token_urlsafe(32)
        codes[_digest(code)] = Issued(host, binding, person)
        path = (back.path or "/") + (f"?{back.query}" if back.query else "")
        query = urlencode({"code": code, "next": path})
        return RedirectResponse(f"https://{host}/.ssc/callback?{query}", status_code=302)

    @app.post("/internal/redeem")
    async def redeem(request: Request) -> JSONResponse:
        if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {secret}"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        issued = codes.pop(_digest(str(body.get("code", ""))), None)
        if (
            issued is None
            or body.get("org") != ORG
            or not hmac.compare_digest(issued.host, str(body.get("host", "")))
            or not hmac.compare_digest(issued.binding, binding_of(str(body.get("nonce", ""))))
        ):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        now, name = int(time.time()), PEOPLE[issued.person]
        answer = {
            "sub": issued.person,
            "org": ORG,
            "name": name,
            "email": f"{name.lower()}@example.test",
            "iat": now,
            "exp": now + SESSION_SECONDS,
        }
        return JSONResponse(answer, headers={"cache-control": "no-store"})

    @app.get("/logout")
    def logout() -> HTMLResponse:
        return HTMLResponse("<h1>Signed out</h1>")

    return app


def control_app(
    seal: Callable[[str, str], str], switches: Mapping[str, Callable[[bool], None]]
) -> FastAPI:
    """The suite's handle on the rig, on loopback only: sealed sessions and the switches."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/seal")
    async def seal_one(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse({"value": seal(str(body["host"]), str(body["user"]))})

    @app.post("/{name}")
    async def switch(name: str, request: Request) -> Response:
        flip = switches.get(name)
        if flip is None:
            return Response(status_code=404)
        body = await request.json()
        flip(bool(body["on"]))
        return Response(status_code=204)

    return app


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError):
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()


async def splice(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int) -> None:
    """Copy bytes between a client and ``127.0.0.1:port`` until either side closes."""
    try:
        up_reader, up_writer = await asyncio.open_connection(LOOPBACK, port)
    except OSError:
        writer.close()
        return
    copies = [
        asyncio.create_task(_copy(reader, up_writer)),
        asyncio.create_task(_copy(up_reader, writer)),
    ]
    try:
        await asyncio.wait(copies, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for copy in copies:
            copy.cancel()
        for w in (writer, up_writer):
            w.close()


@dataclass
class Switch:
    """A forwarder to the authorisation service that can be turned off, which Envoy sees as the
    service stopping: a new connection is closed at once and open ones are cut."""

    port: int
    on: bool = True
    open: set[asyncio.StreamWriter] = field(default_factory=set[asyncio.StreamWriter])

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self.on:
            writer.close()
            return
        self.open.add(writer)
        try:
            await splice(reader, writer, self.port)
        finally:
            self.open.discard(writer)

    def set(self, on: bool) -> None:
        self.on = on
        if not on:
            for writer in list(self.open):
                writer.close()


async def proxy(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, route: Callable[[str], int | None]
) -> None:
    """One browser connection to the CONNECT proxy: ``route`` names the port for a host, or
    None, and anything but ``CONNECT host:443`` to a routed host is refused."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    except asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError:
        writer.close()
        return
    method, _, rest = head.decode("latin-1").partition(" ")
    host, _, port = rest.partition(" ")[0].rpartition(":")
    dest = route(host.lower()) if method == "CONNECT" and port == "443" else None
    if dest is None:
        writer.write(b"HTTP/1.1 403 Forbidden\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")
        writer.close()
        return
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await splice(reader, writer, dest)


def certificate(directory: Path) -> ssl.SSLContext:
    """A server context with a throwaway self-signed certificate for the cell's hosts and the
    auth host, made for this run; the browsers are told to accept it."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"*.{BASE}")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(f"*.{BASE}"), x509.DNSName(AUTH_HOST)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "front.crt", directory / "front.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_path, key_path)
    return context


class _Server(uvicorn.Server):
    """A server sharing the rig's event loop, which handles the signals itself."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


def free_port() -> int:
    with socket.socket() as s:
        s.bind((LOOPBACK, 0))
        return int(s.getsockname()[1])


def docker(*args: str) -> str:
    """Run one docker command; its standard output, or an error naming the command."""
    found = shutil.which("docker")
    if found is None:
        raise RuntimeError("the fast target needs Docker")
    out = subprocess.run([found, *args], capture_output=True, text=True, check=False)  # noqa: S603
    if out.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {out.stderr[-2000:]}")
    return out.stdout.strip()


def published(name: str, port: str) -> int:
    return int(docker("port", name, f"{port}/tcp").splitlines()[0].rsplit(":", 1)[1])


async def reachable(port: int) -> None:
    """Wait until something listens on ``127.0.0.1:port``."""
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            _, writer = await asyncio.open_connection(LOOPBACK, port)
        except OSError:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.1)
            continue
        writer.close()
        return


async def until_stopped() -> None:
    """Until standard input closes, or SIGINT or SIGTERM arrives."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    def watch() -> None:
        sys.stdin.read()
        loop.call_soon_threadsafe(stop.set)

    threading.Thread(target=watch, daemon=True).start()
    await stop.wait()


def announce(settings: Mapping[str, str]) -> None:
    sys.stdout.write(json.dumps(dict(settings)) + "\n")
    sys.stdout.flush()


@dataclass
class Rig:
    """Everything ``fast`` started, for stopping it in reverse."""

    servers: list[tuple[uvicorn.Server, asyncio.Task[None]]] = field(
        default_factory=list[tuple[uvicorn.Server, asyncio.Task[None]]]
    )
    listeners: list[asyncio.Server] = field(default_factory=list[asyncio.Server])
    processes: list[asyncio.subprocess.Process] = field(
        default_factory=list[asyncio.subprocess.Process]
    )
    containers: list[str] = field(default_factory=list[str])
    network: str | None = None

    async def serve(self, app: FastAPI, host: str, port: int) -> None:
        server = _Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
        )
        self.servers.append((server, asyncio.create_task(server.serve())))
        await reachable(port)

    async def listen(
        self,
        handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
        host: str,
        tls: ssl.SSLContext | None = None,
    ) -> int:
        server = await asyncio.start_server(handler, host, 0, ssl=tls)
        self.listeners.append(server)
        return int(server.sockets[0].getsockname()[1])

    async def app(self, port: int) -> None:
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("the fast target needs Node")
        env = {**os.environ, "PORT": str(port), "HOST": ANY}
        self.processes.append(
            await asyncio.create_subprocess_exec(node, str(HERE / "apps" / "server.mjs"), env=env)
        )
        await reachable(port)

    async def container(self, name: str, *args: str) -> None:
        self.containers.append(name)
        await asyncio.to_thread(docker, "run", "-d", "--rm", "--name", name, *args)

    async def stop(self) -> None:
        for listener in self.listeners:
            listener.close()
        if self.containers:
            await asyncio.to_thread(docker, "rm", "-f", *self.containers)
        if self.network is not None:
            with contextlib.suppress(RuntimeError):
                await asyncio.to_thread(docker, "network", "rm", self.network)
        for server, _ in self.servers:
            server.should_exit = True
        await asyncio.gather(*(task for _, task in self.servers), return_exceptions=True)
        for process in self.processes:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            await process.wait()


async def run_fast(limit: int) -> None:  # noqa: PLR0915  (one stack, started in order)
    """Start the fast target, announce it, and run until stopped."""
    keyring = parse_keyring(new_keyring())
    view = AccessView.from_document(snapshot(limit))
    fresh = {"on": True}
    secret = secrets.token_urlsafe(32)
    night_password = secrets.token_urlsafe(18)
    auth = auth_app(secret, night_password)
    ports = {name: free_port() for name in ("alpha", "bravo", "authz", "relay", "auth", "control")}
    apps = {
        upstream(ALPHA_PROD): ports["alpha"],
        upstream(BRAVO_PROD): ports["bravo"],
        upstream(BRAVO_PREVIEW): ports["bravo"],
    }
    limits = {upstream(BRAVO_PREVIEW): limit}

    async def bearer() -> str:
        return secret

    async def dial(host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_connection(LOOPBACK, apps[host])
        cut = limits.get(host, REQUEST_TIMEOUT_SECONDS)
        asyncio.get_running_loop().call_later(cut, writer.close)
        return reader, writer

    def flip_snapshot(on: bool) -> None:
        fresh["on"] = on

    gate = gate_for(
        GateConfig(
            org_id=ORG,
            cell_label=LABEL,
            apps_domain=DOMAIN,
            auth_url=AUTH_URL,
            issuer=f"https://keys.example.test/{LABEL}",
            project_number=PROJECT_NUMBER,
            region=REGION,
            max_body_bytes=MAX_BODY_BYTES,
        ),
        keyring,
        view=lambda: view if fresh["on"] else None,
        redeemer=HttpRedeemer(
            auth_url=AUTH_URL,
            org_id=ORG,
            bearer=bearer,
            transport=httpx2.ASGITransport(app=auth),
        ),
    )
    streams = Streams(lambda: gate, dial=dial)
    switch = Switch(ports["authz"])
    rig = Rig()
    work = Path(tempfile.mkdtemp(prefix="ssc-isolation-"))
    mount = work / "docker"
    mount.mkdir()
    tag = "ssc029-" + secrets.token_hex(4)
    try:
        for name in ("alpha", "bravo"):
            await rig.app(ports[name])
        await rig.serve(create_app(lambda: gate, streams=streams), LOOPBACK, ports["authz"])
        await rig.serve(auth, LOOPBACK, ports["auth"])
        seal = sealer_for(keyring, ORG)
        switches = {"authoriser": switch.set, "snapshot": flip_snapshot}
        await rig.serve(control_app(seal, switches), LOOPBACK, ports["control"])
        rig.listeners.append(await streams.serve(ANY, ports["relay"]))
        switch_port = await rig.listen(switch.handle, ANY)

        (mount / "forward.py").write_text(FORWARDER)
        cfg = EnvoyConfig(
            authz_host="host.docker.internal",
            authz_port=switch_port,
            stream_host="host.docker.internal",
            stream_port=ports["relay"],
            upstream_tls=False,
        )
        (mount / "envoy.json").write_text(json.dumps(render(cfg)))
        await asyncio.to_thread(docker, "network", "create", tag)
        rig.network = tag
        gateway = "host.docker.internal:host-gateway"
        for name, envs in (("alpha", (ALPHA_PROD,)), ("bravo", (BRAVO_PROD, BRAVO_PREVIEW))):
            aliases = [arg for env in envs for arg in ("--network-alias", upstream(env))]
            await rig.container(
                f"{tag}-{name}", "--network", tag, *aliases, "--add-host", gateway,
                "-v", f"{mount}:/c:ro", PIPE_IMAGE, "python", "/c/forward.py", str(ports[name]),
            )  # fmt: skip
        await rig.container(
            f"{tag}-envoy", "--network", tag, "--add-host", gateway, "-p", f"{LOOPBACK}::8080",
            "-v", f"{mount}:/c:ro", ENVOY_IMAGE, "-c", "/c/envoy.json", "--log-level", "warn",
        )  # fmt: skip
        envoy_port = await asyncio.to_thread(published, f"{tag}-envoy", "8080")
        await asyncio.sleep(0.5)
        for name in ("alpha", "bravo"):
            running = await asyncio.to_thread(
                docker, "inspect", "-f", "{{.State.Running}}", f"{tag}-{name}"
            )
            if running != "true":
                raise RuntimeError(f"the {name} forwarder stopped; Envoy would resolve run.app")
        deadline = time.monotonic() + WAIT_SECONDS
        async with httpx2.AsyncClient(timeout=1) as client:
            while True:
                try:
                    await client.get(f"http://{LOOPBACK}:{envoy_port}", headers={"host": BASE})
                    break
                except httpx2.HTTPError:
                    if time.monotonic() > deadline:
                        raise
                    await asyncio.sleep(0.2)

        tls = certificate(work)

        async def to_envoy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await splice(reader, writer, envoy_port)

        async def to_auth(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await splice(reader, writer, ports["auth"])

        front = await rig.listen(to_envoy, LOOPBACK, tls)
        auth_front = await rig.listen(to_auth, LOOPBACK, tls)

        def route(host: str) -> int | None:
            if host == AUTH_HOST:
                return auth_front
            return front if host.endswith("." + BASE) else None

        async def to_front(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await proxy(reader, writer, route)

        proxy_port = await rig.listen(to_front, LOOPBACK)
        announce(
            {
                "SSC_ISO_TARGET": "fast",
                "SSC_ISO_CELL1_BASE": BASE,
                "SSC_ISO_AUTH_URL": AUTH_URL,
                "SSC_ISO_PROXY": f"http://{LOOPBACK}:{proxy_port}",
                "SSC_ISO_CONTROL": f"http://{LOOPBACK}:{ports['control']}",
                "SSC_ISO_LIMIT_SECONDS": str(limit),
                "SSC_ISO_USER": ADA,
                "SSC_ISO_OTHER_USER": BEN,
                "SSC_ISO_OUTSIDER": CY,
                "SSC_NIGHT_AUTH_URL": AUTH_URL,
                "SSC_NIGHT_ORG": ORG,
                "SSC_NIGHT_USERNAME": NIGHT_USERNAME,
                "SSC_NIGHT_PASSWORD": night_password,
            }
        )
        await until_stopped()
    finally:
        await streams.aclose()
        await rig.stop()
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    modes = parser.add_subparsers(dest="mode", required=True)
    fast = modes.add_parser("fast", help="the cell in Docker, for the pre-merge run")
    fast.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="bravo preview's limit")
    args = parser.parse_args()
    asyncio.run(run_fast(args.limit))


if __name__ == "__main__":
    main()
