"""The app's ID token for the data gateway (SSC-051), against a local stand-in for Cloud Run's
metadata server that answers like Google's: ``email`` only when ``format=full`` is asked."""

import base64
import http.server
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest

from ssc_app.workload import IDENTITY_PATH, WorkloadToken, WorkloadTokenError, identity_url

AUDIENCE = "https://ssc-datagw-123456789012.us-central1.run.app"
NOW = 1_800_000_000.0


def _token(claims: dict[str, object]) -> str:
    def part(doc: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(doc).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.c2ln"


@dataclass
class Metadata:
    """What the stand-in was asked, and what it answers next."""

    url: str = ""
    asked: list[dict[str, list[str]]] = field(default_factory=list[dict[str, list[str]]])
    status: int = 200
    body: str | None = None


@pytest.fixture
def metadata() -> Iterator[Metadata]:
    state = Metadata()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            state.asked.append(query)
            if url.path != IDENTITY_PATH or self.headers.get("Metadata-Flavor") != "Google":
                self.send_response(403)
                self.end_headers()
                return
            claims: dict[str, object] = {"aud": query["audience"][0], "exp": NOW + 3600}
            if query.get("format") == ["full"]:
                claims |= {"email": "ssc-a-x@p.iam.gserviceaccount.com", "email_verified": True}
            body = (state.body if state.body is not None else _token(claims)).encode()
            self.send_response(state.status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def _claims(token: str) -> dict[str, object]:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def test_done_when_the_token_is_asked_for_with_format_full_and_carries_the_email(
    metadata: Metadata,
) -> None:
    token = WorkloadToken(audience=AUDIENCE, metadata=metadata.url, clock=lambda: NOW).get()
    assert metadata.asked == [{"audience": [AUDIENCE], "format": ["full"]}]
    claims = _claims(token)
    assert claims["aud"] == AUDIENCE
    assert claims["email_verified"] is True
    assert "format=full" in identity_url(AUDIENCE)


def test_a_token_is_reused_until_five_minutes_before_it_expires(metadata: Metadata) -> None:
    now = [NOW]
    tokens = WorkloadToken(audience=AUDIENCE, metadata=metadata.url, clock=lambda: now[0])
    first = tokens.get()
    now[0] = NOW + 3600 - 301
    assert tokens.get() == first
    assert len(metadata.asked) == 1
    now[0] = NOW + 3600 - 300
    tokens.get()
    assert len(metadata.asked) == 2


@pytest.mark.parametrize(("status", "body"), [(500, None), (200, "not-a-token")])
def test_a_metadata_server_that_fails_or_answers_garbage_is_an_error(
    metadata: Metadata, status: int, body: str | None
) -> None:
    metadata.status, metadata.body = status, body
    with pytest.raises(WorkloadTokenError):
        WorkloadToken(audience=AUDIENCE, metadata=metadata.url, clock=lambda: NOW).get()


def test_no_metadata_server_is_an_error() -> None:
    with pytest.raises(WorkloadTokenError, match="unreachable"):
        WorkloadToken(audience=AUDIENCE, metadata="http://127.0.0.1:9").get()
