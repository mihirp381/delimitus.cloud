"""T6's stand-in, run as a Cloud Run job. Standard library only. Prints one JSON line,
``{"proofrun_egress": {...}}``, which Cloud Logging keeps as the entry's ``jsonPayload``.

- ``nat [<host>@<ip>[/<path>] ...]``: for each target, connect to ``<ip>:443`` (an address, so no
  name is resolved through the cell's sinkhole), speak TLS as ``<host>``, GET ``<path>`` (``/`` if
  none) as ``curl`` and report the source address the target saw, or the stage that failed
  (``connect``, ``tls`` or ``http``) and how long each took. With no target it asks Cloudflare's
  ``one.one.one.one@1.1.1.1/cdn-cgi/trace``.
- ``proxy <ip:port> allow=<host> ... deny=<host> ...``: for each host, ``CONNECT <host>:443``
  through the proxy. For an allowed host that tunnels, GET ``/`` over TLS inside the tunnel as
  ``curl`` (both default hosts answer with the caller's address) and report it.
"""

import ipaddress
import json
import socket
import ssl
import sys
import time

TIMEOUT = 10.0
TRACE = "one.one.one.one@1.1.1.1/cdn-cgi/trace"


def target(text: str) -> tuple[str, str, str]:
    """``<host>@<ip>[/<path>]`` as (host, ip, path)."""
    host, _, rest = text.partition("@")
    ip, slash, path = rest.partition("/")
    if not host or not ip:
        raise ValueError(f"target {text!r} is not <host>@<ip>[/<path>]")
    return host, str(ipaddress.IPv4Address(ip)), f"/{path}" if slash else "/"


def seen_address(body: bytes) -> str | None:
    """The caller's address in an answer: Cloudflare's ``ip=`` line, or a bare first line."""
    for line in body.decode(errors="replace").splitlines():
        if line.startswith("ip="):
            return _address(line.removeprefix("ip=").encode())
    return _address(body)


def ask(text: str, timeout: float = TIMEOUT, port: int = 443) -> dict[str, object]:
    try:
        host, ip, path = target(text)
    except ValueError as exc:
        return {"stage": "target", "error": str(exc)[:200]}
    report: dict[str, object] = {"stage": "connect"}
    started = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            report["connect_s"] = round(time.monotonic() - started, 3)
            report["stage"] = "tls"
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as tls:
                report["tls_s"] = round(time.monotonic() - started, 3)
                report["stage"] = "http"
                tls.sendall(
                    f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: curl/8.10.1\r\n"
                    "Accept: */*\r\nConnection: close\r\n\r\n".encode()
                )
                raw = b""
                while chunk := tls.recv(4096):
                    raw += chunk
        head, _, body = raw.partition(b"\r\n\r\n")
        parts = head.split(b" ", 2)
        report["status"] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        report["ip"] = seen_address(body)
        report["stage"] = "done"
    except OSError as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"[:200]
    report["total_s"] = round(time.monotonic() - started, 3)
    return report


def nat(targets: list[str]) -> dict[str, object]:
    asked = {t: ask(t) for t in targets or [TRACE]}
    return {"mode": "nat", "targets": asked}


def _status(sock: socket.socket) -> int | None:
    head = b""
    while b"\r\n\r\n" not in head and len(head) < 8192:
        chunk = sock.recv(1024)
        if not chunk:
            break
        head += chunk
    parts = head.split(b" ", 2)
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None


def _address(body: bytes) -> str | None:
    text = body.decode(errors="replace").strip().splitlines()
    try:
        return str(ipaddress.IPv4Address(text[0].strip())) if text else None
    except ValueError:
        return None


def through(proxy: tuple[str, int], host: str, fetch: bool) -> dict[str, object]:
    """CONNECT to ``host:443``; if it tunnels and ``fetch``, ask the host for our address."""
    try:
        with socket.create_connection(proxy, timeout=TIMEOUT) as sock:
            sock.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
            status = _status(sock)
            if status != 200 or not fetch:
                return {"status": status}
            context = ssl.create_default_context()
            with context.wrap_socket(sock, server_hostname=host) as tls:
                tls.sendall(
                    f"GET / HTTP/1.1\r\nHost: {host}\r\nUser-Agent: curl/8.10.1\r\n"
                    "Accept: */*\r\nConnection: close\r\n\r\n".encode()
                )
                raw = b""
                while chunk := tls.recv(4096):
                    raw += chunk
            body = raw.split(b"\r\n\r\n", 1)[-1]
            return {"status": status, "ip": _address(body)}
    except OSError as exc:
        return {"status": None, "error": f"{type(exc).__name__}: {exc}"[:200]}


def proxy(args: list[str]) -> dict[str, object]:
    ip, _, port = args[0].partition(":")
    target = (ip, int(port))
    allowed = [a.removeprefix("allow=") for a in args[1:] if a.startswith("allow=")]
    denied = [a.removeprefix("deny=") for a in args[1:] if a.startswith("deny=")]
    return {
        "mode": "proxy",
        "proxy": args[0],
        "allowed": {h: through(target, h, fetch=True) for h in allowed},
        "unlisted": {h: through(target, h, fetch=False) for h in denied},
    }


def main(argv: list[str]) -> int:
    report = nat(argv[1:]) if argv[:1] == ["nat"] else proxy(argv[1:]) if argv[:1] == ["proxy"] else None
    if report is None:
        print("usage: probe.py nat [<host>@<ip>[/<path>]...] | proxy <ip:port> allow=<host>... deny=<host>...")
        return 2
    print(json.dumps({"proofrun_egress": report}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
