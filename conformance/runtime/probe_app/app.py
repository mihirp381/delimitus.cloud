"""The runtime probe app: what a platform container must allow and refuse, seen from inside.

Standard library only, so the image needs no package install. Listens on ``$PORT`` (no default:
an app that ignores ``PORT`` is the failure this probes). Routes:

- ``/`` and ``/health``: 200. (Cloud Run keeps some paths ending in ``z``, ``/healthz`` among them.)
- ``/probe/uid``: the process's uid and gid.
- ``/probe/env``: the environment's variable names, never their values.
- ``/probe/write``: which of ``/``, ``/app`` and ``$HOME`` accept a new file.
- ``/probe/mounts``: every mount's point and type, and whether ``$HOME`` accepts a file.
- ``/probe/egress``: direct connections to the internet, each blocked or not.
- ``/probe/dns``: what public names resolve to, and whether a public resolver answers.
- ``/probe/identity``: the metadata server's identity and what it may do. The token stays here.
- ``/probe/peer?url=``: whether another app answers this one, by name and by Google's VIP.
- ``/probe/peer-cell?app=&gateway=&range=``: the same for another cell's probe app (on
  ``/health``) and gateway (on its own ``/.ssc/logout``), keeping the headers and body start that
  show who answered, and direct connections into that cell's address range unless the range
  holds this app's own address.
- ``/probe/headers``: the request's headers, as received.
- ``/probe/sse``: three server-sent events a second apart.
"""

import ipaddress
import itertools
import json
import os
import secrets
import socket
import ssl
import struct
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TIMEOUT = 3.0
METADATA = "http://metadata.google.internal/computeMetadata/v1"
GOOGLE_VIP = "199.36.153.8"
PUBLIC_NAME = "example.com"
GOOGLE_API_NAME = "storage.googleapis.com"
PROBE_SECRET = "ssc-a-probe"  # noqa: S105  (a secret name, not a value)
PEER_CELL_HOSTS = 2
PEER_CELL_PORTS = (443, 8080)
PEER_CELL_PATHS = {"app": "/health", "gateway": "/.ssc/logout"}
PEER_CELL_HEADERS = (
    "server",
    "content-type",
    "cache-control",
    "x-content-type-options",
    "referrer-policy",
    "via",
)
PEER_CELL_BODY = 256
VIP_READ = 4096
SENSITIVE = (
    "iam.serviceAccounts.actAs",
    "iam.serviceAccounts.getAccessToken",
    "resourcemanager.projects.get",
    "run.services.get",
    "run.services.update",
    "secretmanager.secrets.list",
    "secretmanager.versions.access",
    "storage.buckets.list",
    "storage.objects.get",
    "cloudsql.instances.connect",
)


def writable(directory: str) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".probe-"):
            return True
    except OSError:
        return False


def _blocked(run: Callable[[], str]) -> dict[str, object]:
    try:
        return {"blocked": False, "detail": run()}
    except OSError as exc:
        return {"blocked": True, "detail": type(exc).__name__}


def _tcp(host: str, port: int, family: int = socket.AF_INET) -> dict[str, object]:
    def run() -> str:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect((host, port))
            return "connected"

    return _blocked(run)


def _dns_query(name: str) -> bytes:
    header = secrets.token_bytes(2) + struct.pack(">HHHHH", 0x0100, 1, 0, 0, 0)
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    return header + labels + struct.pack(">HH", 1, 1)


def _udp(host: str, port: int, payload: bytes) -> dict[str, object]:
    def run() -> str:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(TIMEOUT)
            s.sendto(payload, (host, port))
            data, _ = s.recvfrom(4096)
            return f"reply of {len(data)} bytes"

    return _blocked(run)


def egress() -> dict[str, object]:
    return {
        "tcp 1.1.1.1:443": _tcp("1.1.1.1", 443),
        "tcp 1.1.1.1:80": _tcp("1.1.1.1", 80),
        "udp 8.8.8.8:53": _udp("8.8.8.8", 53, _dns_query(PUBLIC_NAME)),
        "udp 1.1.1.1:443": _udp("1.1.1.1", 443, secrets.token_bytes(1200)),
        "tcp [2606:4700:4700::1111]:443": _tcp("2606:4700:4700::1111", 443, socket.AF_INET6),
    }


def dns() -> dict[str, object]:
    names = [PUBLIC_NAME, f"ssc-probe-{secrets.token_hex(6)}.{PUBLIC_NAME}"]
    answers: dict[str, list[str]] = {}
    for name in names:
        answers[name] = _addresses(name)
    return {
        "answers": answers,
        "direct": _udp("8.8.8.8", 53, _dns_query(names[1])),
        "resolvers": _resolvers(),
        "google_api": _addresses(GOOGLE_API_NAME),
    }


def _addresses(name: str) -> list[str]:
    try:
        return sorted({str(i[4][0]) for i in socket.getaddrinfo(name, 443)})
    except OSError:
        return []


def _resolvers() -> list[str]:
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as f:
            return [line.split()[1] for line in f if line.startswith("nameserver ")]
    except OSError, IndexError:
        return []


def _exchange(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return response.status, _lower(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, _lower(exc.headers.items() if exc.headers else []), exc.read()


def _lower(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {k.lower(): v.strip() for k, v in headers}


def _http(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes]:
    status, _, data = _exchange(url, method=method, headers=headers, body=body)
    return status, data


def _metadata(path: str) -> str:
    status, body = _http(f"{METADATA}/{path}", headers={"Metadata-Flavor": "Google"})
    if status != 200:  # noqa: PLR2004
        raise OSError(f"metadata {path}: HTTP {status}")
    return body.decode()


def identity() -> dict[str, object]:
    out: dict[str, object] = {"k_service": os.environ.get("K_SERVICE", "")}
    try:
        out["email"] = _metadata("instance/service-accounts/default/email")
        project = _metadata("project/project-id")
        out["project"] = project
        token = json.loads(_metadata("instance/service-accounts/default/token"))["access_token"]
    except (OSError, ValueError, KeyError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    crm = f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:testIamPermissions"
    try:
        status, body = _http(
            crm,
            method="POST",
            headers=auth,
            body=json.dumps({"permissions": list(SENSITIVE)}).encode(),
        )
        out["granted_status"] = status
        decoded = json.loads(body)
        out["granted"] = decoded.get("permissions", []) if status == 200 else None  # noqa: PLR2004
        if status != 200:  # noqa: PLR2004
            out["granted_reason"] = str(decoded.get("error", {}).get("message", ""))[:200]
    except (OSError, ValueError) as exc:
        out["error"] = f"testIamPermissions: {type(exc).__name__}"
    secrets_api = f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets"
    for key, url in (
        ("secret_access_status", f"{secrets_api}/{PROBE_SECRET}/versions/latest:access"),
        ("secret_list_status", secrets_api),
    ):
        try:
            out[key] = _http(url, headers=auth)[0]
        except OSError as exc:
            out[key] = type(exc).__name__
    return out


def _vip_get(host: str, token: str, path: str = "/", *, full: bool = False) -> dict[str, object]:
    """GET https://<host><path> through Google's private VIP, where internal ingress may answer."""
    try:
        context = ssl.create_default_context()
        with (
            socket.create_connection((GOOGLE_VIP, 443), timeout=TIMEOUT) as raw,
            context.wrap_socket(raw, server_hostname=host) as tls,
        ):
            tls.sendall(
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\nAuthorization: Bearer {token}\r\n"
                "Connection: close\r\n\r\n".encode()
            )
            data = b""
            while len(data) < VIP_READ and (chunk := tls.recv(VIP_READ)):
                data += chunk
        return _answer(*_parse_response(data), full=full)
    except (OSError, ValueError, IndexError) as exc:
        return {"error": type(exc).__name__}


def _parse_response(data: bytes) -> tuple[int, dict[str, str], bytes]:
    """Status, lower-cased headers and body of a raw HTTP/1.1 response."""
    head, _, body = data.partition(b"\r\n\r\n")
    status_line, *lines = head.decode(errors="replace").split("\r\n")
    headers = _lower((k, v) for k, _, v in (line.partition(":") for line in lines))
    return int(status_line.split()[1]), headers, body


def _answer(status: int, headers: dict[str, str], body: bytes, *, full: bool) -> dict[str, object]:
    """The status, and with ``full`` the headers and body start that show who answered."""
    if not full:
        return {"status": status}
    return {
        "status": status,
        "headers": {k: v for k, v in headers.items() if k in PEER_CELL_HEADERS},
        "body": body[:PEER_CELL_BODY].decode(errors="replace"),
    }


def peer(url: str, path: str = "/", *, full: bool = False) -> dict[str, object]:
    host = urllib.parse.urlsplit(url).hostname or ""
    if not host:
        return {"url": url, "error": "no url"}
    try:
        token = _metadata(f"instance/service-accounts/default/identity?audience={url}")
    except OSError:
        token = ""
    target = urllib.parse.urljoin(url, path)
    attempts: dict[str, object] = {}
    for name, headers in (
        ("by name", {}),
        ("by name with ID token", {"Authorization": f"Bearer {token}"}),
    ):
        try:
            attempts[name] = _answer(*_exchange(target, headers=headers), full=full)
        except OSError as exc:
            attempts[name] = {"error": type(exc).__name__}
    attempts["by Google VIP with ID token"] = _vip_get(host, token, path, full=full)
    return {"url": url, "attempts": attempts}


def _own_address(toward: str) -> str | None:
    """The address this app sends from toward ``toward``. A UDP connect sends no packet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((toward, 9))
            return str(s.getsockname()[0])
    except OSError:
        return None


def peer_cell(app_url: str, gateway_url: str, cidr: str) -> dict[str, object]:
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return {"range": cidr, "error": f"not an address range: {cidr!r}"}
    hosts = [str(a) for a in itertools.islice(network.hosts(), 1, 1 + PEER_CELL_HOSTS)]
    hosts = hosts or [str(network.network_address)]
    own = _own_address(hosts[0])
    own_in_range = None if own is None else ipaddress.ip_address(own) in network
    attempts: dict[str, object] = {}
    for name, url in (("app", app_url), ("gateway", gateway_url)):
        found = peer(url, PEER_CELL_PATHS[name], full=True)
        tried = found.get("attempts")
        if not isinstance(tried, dict):
            return {"range": cidr, "error": f"{name}: {found.get('error')}"}
        attempts.update({f"{name} {k}": v for k, v in tried.items()})
    if own_in_range is not True:
        for host in hosts:
            for port in PEER_CELL_PORTS:
                attempts[f"tcp {host}:{port}"] = _tcp(host, port)
    return {"range": cidr, "own": own, "own_in_range": own_in_range, "attempts": attempts}


def mounts() -> dict[str, object]:
    found: list[list[str]] = []
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            found = [line.split()[1:3] for line in f if line.strip()]
    except OSError:
        pass
    home = os.environ.get("HOME", "")
    return {"mounts": found, "home": home, "home_writable": bool(home) and writable(home)}


def probe(path: str, query: dict[str, list[str]], headers: dict[str, str]) -> object | None:
    home = os.environ.get("HOME", "")
    routes: dict[str, Callable[[], object]] = {
        "/": lambda: "ok",
        "/health": lambda: "ok",
        "/probe/uid": lambda: {"uid": os.getuid(), "gid": os.getgid()},
        "/probe/env": lambda: {"names": sorted(os.environ)},
        "/probe/write": lambda: {
            "home": home,
            "writable": {d: writable(d) for d in ("/", "/app", home) if d},
        },
        "/probe/mounts": mounts,
        "/probe/egress": egress,
        "/probe/dns": dns,
        "/probe/identity": identity,
        "/probe/peer": lambda: peer(query.get("url", [""])[0]),
        "/probe/peer-cell": lambda: peer_cell(
            query.get("app", [""])[0], query.get("gateway", [""])[0], query.get("range", [""])[0]
        ),
        "/probe/headers": lambda: headers,
    }
    route = routes.get(path)
    return None if route is None else route()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/probe/sse":
            self._sse()
            return
        headers = {k.lower(): v for k, v in self.headers.items()}
        body = probe(parts.path, urllib.parse.parse_qs(parts.query), headers)
        data = json.dumps(body).encode()
        self.send_response(404 if body is None else 200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        for n in range(3):
            self.wfile.write(f"data: {n}\n\n".encode())
            self.wfile.flush()
            time.sleep(1.0)
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), Handler).serve_forever()  # noqa: S104
