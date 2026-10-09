"""The egress probe's tunnel, standard library only: ``CONNECT`` through the proxy, then a TLS
request to the host inside it (``probe``), or a tunnel held open with a request a second until
it ends (``hold``, GA-6.1). Nothing here keeps or returns the proxy's address, user or password;
a failure is reported as the exception's class name."""

import base64
import re
import socket
import ssl
import time
import urllib.parse
from collections.abc import Callable, Iterator

PROXY_PORT = 3128
TUNNEL_PORT = 443
TIMEOUT_S = 15.0
TUNNELLED = 200
HEAD_LIMIT = 8192
BODY_LIMIT = 65536
TICK_S = 1.0
Wrap = Callable[[socket.socket, str], socket.socket]
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


def _connect(
    host: str, credentials: bool, proxy_url: str, deadline: float, timeout: float
) -> tuple[int | None, socket.socket]:
    """``CONNECT host:443`` through the proxy; the proxy's status and the open socket, which the
    caller closes."""
    proxy = urllib.parse.urlsplit(proxy_url)
    request = f"CONNECT {host}:{TUNNEL_PORT} HTTP/1.1\r\nHost: {host}:{TUNNEL_PORT}\r\n"
    if credentials:
        request += f"Proxy-Authorization: Basic {_authorization(proxy)}\r\n"
    address = (proxy.hostname or "", proxy.port or PROXY_PORT)
    sock = socket.create_connection(address, timeout=timeout)
    try:
        sock.sendall(f"{request}\r\n".encode())
        status, _ = parse_response(_read(sock, deadline, until_head=True))
    except BaseException:
        sock.close()
        raise
    return status, sock


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
        out["proxy_status"], sock = _connect(host, credentials, proxy_url, deadline, timeout)
        with sock:
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


def tls_wrap(sock: socket.socket, host: str) -> socket.socket:
    """TLS to ``host`` over the tunnel, verified against the system's roots."""
    return ssl.create_default_context().wrap_socket(sock, server_hostname=host)


def _keepalive(host: str) -> bytes:
    return (
        f"GET /cdn-cgi/trace HTTP/1.1\r\nHost: {host}\r\nUser-Agent: ssc-proofrun-hold\r\n\r\n"
    ).encode()


def hold(  # noqa: PLR0913  (keyword-only)
    host: str,
    proxy_url: str | None,
    *,
    seconds: float,
    tick: float = TICK_S,
    timeout: float = TIMEOUT_S,
    wrap: Wrap = tls_wrap,
    clock: Callable[[], float] = time.time,
) -> Iterator[dict[str, object]]:
    """Open a tunnel to ``host:443`` with the app's credential and hold it for up to ``seconds``,
    sending a keep-alive ``GET /cdn-cgi/trace`` every ``tick`` and reading the answers until the
    next. Yields ``open`` (the proxy said 200 and TLS is up), one ``alive`` per tick the tunnel
    survived (``bytes`` read in it), and always one ``end`` last: ``reason`` is ``closed`` (the
    tunnel was closed under it), an exception's class name, ``proxy_<status>`` (refused),
    ``no HTTPS_PROXY`` or ``max`` (held to the end). ``at`` is seconds since the epoch."""
    alive = 0
    opened_at: float | None = None

    def end(reason: str) -> dict[str, object]:
        return {
            "event": "end",
            "reason": reason,
            "alive": alive,
            "opened_at": opened_at,
            "at": clock(),
        }

    if proxy_url is None:
        yield end("no HTTPS_PROXY")
        return
    try:
        status, sock = _connect(host, True, proxy_url, time.monotonic() + timeout, timeout)
    except Exception as exc:
        yield end(type(exc).__name__)
        return
    if status != TUNNELLED:
        sock.close()
        yield end(f"proxy_{status}")
        return
    try:
        sock.settimeout(timeout)
        tunnel = wrap(sock, host)
    except Exception as exc:
        sock.close()
        yield end(type(exc).__name__)
        return
    with tunnel:
        opened_at = clock()
        yield {"event": "open", "proxy_status": status, "at": opened_at}
        reason = "max"
        stop = time.monotonic() + seconds
        while reason == "max" and time.monotonic() < stop:
            tick_end = min(time.monotonic() + tick, stop)
            got = 0
            try:
                tunnel.sendall(_keepalive(host))
                while (left := tick_end - time.monotonic()) > 0:
                    tunnel.settimeout(left)
                    try:
                        chunk = tunnel.recv(65536)
                    except TimeoutError:
                        break
                    if not chunk:
                        reason = "closed"
                        break
                    got += len(chunk)
            except OSError as exc:
                reason = type(exc).__name__
            if reason == "max":
                alive += 1
                yield {"event": "alive", "n": alive, "bytes": got, "at": clock()}
        yield end(reason)
