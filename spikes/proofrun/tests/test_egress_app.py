"""The egress probe app's tunnel (``apps/egress/tunnel.py``) against a fake proxy on 127.0.0.1.
The app's FastAPI route is a thin wrapper over it and is not run here."""

import base64
import importlib.util
import json
import socket
import threading
from types import ModuleType
from typing import Any

import pytest

from proofrun.common import KIT

USER = "env_aaaaaaaaaaaaaaaaaaaa.credaaaaaaaa"
PASSWORD = "not-a-real-token-for-tests"


def load_tunnel() -> ModuleType:
    path = KIT / "apps" / "egress" / "tunnel.py"
    spec = importlib.util.spec_from_file_location("egress_tunnel", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tunnel = load_tunnel()


class FakeProxy:
    """Answers each connection with one fixed reply, records the request head, then closes."""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.requests: list[bytes] = []
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    head += chunk
                self.requests.append(head)
                conn.sendall(self.reply)

    def url(self) -> str:
        return f"http://{USER}:{PASSWORD}@127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def proxy_407():
    proxy = FakeProxy(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
    yield proxy
    proxy.close()


@pytest.fixture
def proxy_200():
    proxy = FakeProxy(b"HTTP/1.1 200 Connection established\r\n\r\n")
    yield proxy
    proxy.close()


def show(answer: dict[str, Any]) -> str:
    return json.dumps(answer)


@pytest.mark.parametrize(
    ("host", "valid"),
    [
        ("www.cloudflare.com", True),
        ("example.com", True),
        ("a-b.example.co", True),
        ("WWW.example.com", False),
        ("exa mple.com", False),
        ("localhost", False),
        ("1.1.1.1", False),
        ("", False),
        ("example..com", False),
        ("-a.example.com", False),
        ("a." * 127 + "com", False),
        ("a" * 64 + ".com", False),
    ],
)
def test_valid_host(host: str, valid: bool) -> None:
    assert tunnel.valid_host(host) is valid


def test_valid_host_stops_at_253_characters() -> None:
    longest = ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 57, "com"])
    assert len(longest) == 253
    assert tunnel.valid_host(longest) is True
    assert tunnel.valid_host("b" + longest) is False


def test_the_proxy_status_is_read_and_a_407_goes_no_further(proxy_407: FakeProxy) -> None:
    answer = tunnel.probe("www.cloudflare.com", True, proxy_407.url())
    assert answer == {
        "host": "www.cloudflare.com",
        "credentials": True,
        "proxy_status": 407,
        "status": None,
        "ip": None,
        "error": None,
    }
    (request,) = proxy_407.requests
    assert request.startswith(b"CONNECT www.cloudflare.com:443 HTTP/1.1\r\n")
    assert b"Host: www.cloudflare.com:443\r\n" in request


def test_the_credential_is_sent_only_with_credentials_yes(proxy_407: FakeProxy) -> None:
    tunnel.probe("www.cloudflare.com", True, proxy_407.url())
    tunnel.probe("www.cloudflare.com", False, proxy_407.url())
    with_credential, without = proxy_407.requests
    expected = base64.b64encode(f"{USER}:{PASSWORD}".encode())
    assert b"Proxy-Authorization: Basic " + expected + b"\r\n" in with_credential
    assert b"Proxy-Authorization" not in without


def test_a_tunnel_whose_tls_is_refused_reports_the_class_name(proxy_200: FakeProxy) -> None:
    answer = tunnel.probe("www.cloudflare.com", True, proxy_200.url(), timeout=5.0)
    assert answer["proxy_status"] == 200
    assert answer["status"] is None
    assert answer["ip"] is None
    assert isinstance(answer["error"], str)
    assert answer["error"].isidentifier()


def test_a_missing_proxy_url_is_answered_with_the_error_and_nulls() -> None:
    assert tunnel.probe("www.cloudflare.com", True, None) == {
        "host": "www.cloudflare.com",
        "credentials": True,
        "proxy_status": None,
        "status": None,
        "ip": None,
        "error": "no HTTPS_PROXY",
    }


def test_an_unreachable_proxy_reports_the_class_name_only() -> None:
    free = socket.create_server(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    answer = tunnel.probe("www.cloudflare.com", True, f"http://{USER}:{PASSWORD}@127.0.0.1:{port}")
    assert answer["proxy_status"] is None
    assert answer["error"] == "ConnectionRefusedError"


def test_no_answer_holds_the_password_or_the_user(
    proxy_407: FakeProxy, proxy_200: FakeProxy
) -> None:
    answers = [
        tunnel.probe("www.cloudflare.com", True, proxy_407.url()),
        tunnel.probe("www.cloudflare.com", False, proxy_407.url()),
        tunnel.probe("www.cloudflare.com", True, proxy_200.url(), timeout=5.0),
        tunnel.probe("www.cloudflare.com", True, "http://" + USER + ":" + PASSWORD + "@[bad"),
    ]
    text = "".join(show(a) for a in answers)
    assert PASSWORD not in text
    assert USER not in text
    assert "127.0.0.1" not in text


def test_parse_response_reads_the_status_and_the_ip_line() -> None:
    plain = b"HTTP/1.1 200 OK\r\nContent-Length: 30\r\n\r\nfl=1\nip=34.1.2.3\nts=1.5\n"
    assert tunnel.parse_response(plain) == (200, "34.1.2.3")
    chunked = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"1d\r\nfl=1\nip=34.1.2.3\nts=1.5\n\r\n0\r\n\r\n"
    )
    assert tunnel.parse_response(chunked) == (200, "34.1.2.3")
    assert tunnel.parse_response(b"HTTP/1.1 503 Service Unavailable\r\n\r\nbusy") == (503, None)
    assert tunnel.parse_response(b"") == (None, None)
