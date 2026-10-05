"""The egress probe's tunnel, standard library only: ``CONNECT`` through the proxy, then a TLS
request to the host inside it. Nothing here keeps or returns the proxy's address, user or
password; a failure is reported as the exception's class name."""

import base64
import re
import socket
import ssl
import time
import urllib.parse

PROXY_PORT = 3128
TUNNEL_PORT = 443
TIMEOUT_S = 15.0
TUNNELLED = 200
HEAD_LIMIT = 8192
BODY_LIMIT = 65536
LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
IP_LINE = re.compile(r"^ip=(\S+)\s*$", re.MULTILINE)


def valid_host(host: str) -> bool:
    """A lower-case DNS name of at most 253 characters, two or more labels, the last one
    starting with a letter (so never an IP address)."""
    labels = host.split(".")
    return (
        0 < len(host) <= 253
        and len(labels) >= 2
        and all(LABEL.fullmatch(label) for label in labels)
        and labels[-1][0].isalpha()
    )


def parse_response(raw: bytes) -> tuple[int | None, str | None]:
    """The status code and the ``ip=`` value of a ``/cdn-cgi/trace`` answer, None for each
    that is missing."""
    head, _, body = raw.partition(b"\r\n\r\n")
    parts = head.split(b" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    found = IP_LINE.search(body.decode(errors="replace"))
    return status, found.group(1) if found else None


def _read(sock: socket.socket, deadline: float, *, until_head: bool) -> bytes:
    data = b""
    limit = HEAD_LIMIT if until_head else BODY_LIMIT
    while len(data) < limit and not (until_head and b"\r\n\r\n" in data):
        sock.settimeout(max(deadline - time.monotonic(), 0.001))
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _authorization(proxy: urllib.parse.SplitResult) -> str:
    user = urllib.parse.unquote(proxy.username or "")
    password = urllib.parse.unquote(proxy.password or "")
    return base64.b64encode(f"{user}:{password}".encode()).decode()


def probe(
    host: str,
    credentials: bool,
    proxy_url: str | None,
    *,
    timeout: float = TIMEOUT_S,
    context: ssl.SSLContext | None = None,
) -> dict[str, object]:
    """``CONNECT host:443`` through the proxy, with the credential from the proxy URL when
    ``credentials`` is set. When the proxy says 200, ask the host for ``/cdn-cgi/trace`` over
    TLS and report its status and the address it saw. One deadline covers it all."""
    out: dict[str, object] = {
        "host": host,
        "credentials": credentials,
        "proxy_status": None,
        "status": None,
        "ip": None,
        "error": None,
    }
    if proxy_url is None:
        out["error"] = "no HTTPS_PROXY"
        return out
    deadline = time.monotonic() + timeout
    try:
        proxy = urllib.parse.urlsplit(proxy_url)
        request = f"CONNECT {host}:{TUNNEL_PORT} HTTP/1.1\r\nHost: {host}:{TUNNEL_PORT}\r\n"
        if credentials:
            request += f"Proxy-Authorization: Basic {_authorization(proxy)}\r\n"
        with socket.create_connection(
            (proxy.hostname or "", proxy.port or PROXY_PORT), timeout=timeout
        ) as sock:
            sock.sendall(f"{request}\r\n".encode())
            out["proxy_status"], _ = parse_response(_read(sock, deadline, until_head=True))
            if out["proxy_status"] != TUNNELLED:
                return out
            sock.settimeout(max(deadline - time.monotonic(), 0.001))
            tls = (context or ssl.create_default_context()).wrap_socket(
                sock, server_hostname=host
            )
            tls.sendall(
                f"GET /cdn-cgi/trace HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
            )
            out["status"], out["ip"] = parse_response(_read(tls, deadline, until_head=False))
    except Exception as exc:
        out["error"] = type(exc).__name__
    return out
