"""SSC-065: what delimitus.com answers, and the abuse limits on the pilot request form."""

import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from ssc_landing.__main__ import Settings, SettingsError, production_app, settings_from_env
from ssc_landing.app import MAX_BODY, create_app
from ssc_landing.page import PLAIN_CSP, SECURITY_HEADERS, USER_DATA_POLICY, PageError, load_page
from ssc_landing.pilot import PilotRequest
from ssc_landing.store import MemoryPilotStore, StoreError

PAGE_PATH = Path(__file__).resolve().parents[3] / "landing" / "index.html"
PAGE = load_page(PAGE_PATH)
PRIVACY_PATH = PAGE_PATH.with_name("privacy.html")
PRIVACY = load_page(PRIVACY_PATH, (USER_DATA_POLICY,))
ORIGIN = "https://delimitus.com"
GOOD = {
    "firstName": "Dana",
    "lastName": "Ortiz",
    "email": "dana@example.com",
    "company": "Example Logistics",
    "tools": "",
    "website": "",
}


class World:
    def __init__(self, store: MemoryPilotStore | None = None) -> None:
        self.store = store or MemoryPilotStore()
        self.logs: list[Mapping[str, object]] = []
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        app = create_app(
            PAGE,
            self.store,
            privacy=PRIVACY,
            origin=ORIGIN,
            redirect_hosts=frozenset({"www.delimitus.com"}),
            trusted_hops=2,
            now=lambda: self.now,
            log=self.logs.append,
        )
        self.client = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)

    def post(
        self,
        body: Mapping[str, str] | None = None,
        *,
        origin: str | None = ORIGIN,
        client_ip: str = "203.0.113.7",
    ) -> httpx2.Response:
        headers = {"x-forwarded-for": f"{client_ip}, 35.191.0.1"}
        if origin is not None:
            headers["origin"] = origin
        return self.client.post("/pilot-request", json=dict(body or GOOD), headers=headers)


@pytest.fixture
def world() -> World:
    return World()


def test_the_page_is_served_with_its_policy_and_no_cookie(world: World) -> None:
    res = world.client.get("/")
    assert res.status_code == 200
    assert res.headers["content-type"] == "text/html; charset=utf-8"
    assert res.headers["content-security-policy"] == PAGE.csp
    assert res.headers["cache-control"] == "public, max-age=300"
    for name, value in SECURITY_HEADERS.items():
        assert res.headers[name] == value
    assert "preload" not in res.headers["strict-transport-security"]
    assert "set-cookie" not in res.headers
    assert "server" not in res.headers
    assert res.content == PAGE.body


def test_head_and_a_matching_etag(world: World) -> None:
    assert world.client.head("/").status_code == 200
    res = world.client.get("/", headers={"if-none-match": PAGE.etag})
    assert res.status_code == 304
    assert res.content == b""


def test_the_privacy_page_is_served_with_its_policy_and_no_cookie(world: World) -> None:
    res = world.client.get("/privacy")
    assert res.status_code == 200
    assert res.headers["content-type"] == "text/html; charset=utf-8"
    assert res.headers["content-security-policy"] == PRIVACY.csp
    assert "script-src 'none'" in PRIVACY.csp
    assert res.headers["cache-control"] == "public, max-age=300"
    for name, value in SECURITY_HEADERS.items():
        assert res.headers[name] == value
    assert "set-cookie" not in res.headers
    assert res.content == PRIVACY.body


def test_the_privacy_page_answers_head_and_a_matching_etag(world: World) -> None:
    res = world.client.head("/privacy")
    assert res.status_code == 200
    assert res.headers["content-security-policy"] == PRIVACY.csp
    assert res.content == b""
    assert PRIVACY.etag != PAGE.etag
    assert world.client.get("/privacy", headers={"if-none-match": PRIVACY.etag}).status_code == 304
    assert world.client.get("/privacy", headers={"if-none-match": PAGE.etag}).status_code == 200


def test_www_moves_permanently_to_the_apex(world: World) -> None:
    res = world.client.get(
        "https://www.delimitus.com/?ref=a",
        headers={"host": "www.delimitus.com"},
        follow_redirects=False,
    )
    assert res.status_code == 301
    assert res.headers["location"] == "https://delimitus.com/?ref=a"
    assert res.headers["strict-transport-security"] == SECURITY_HEADERS["strict-transport-security"]


@pytest.mark.parametrize(
    "path",
    ["/docs", "/openapi.json", "/healthz", "/sign-in", "/index.html", "/privacy.html", "/privacy/"],
)
def test_nothing_else_answers(world: World, path: str) -> None:
    res = world.client.get(path)
    assert res.status_code == 404
    assert res.headers["content-security-policy"] == PLAIN_CSP
    assert "set-cookie" not in res.headers


def test_a_get_to_the_form_path_is_not_found(world: World) -> None:
    assert world.client.get("/pilot-request").status_code == 404


def test_a_request_is_stored_and_logged_without_personal_data(world: World) -> None:
    res = world.post()
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert "set-cookie" not in res.headers
    [stored] = world.store.requests
    assert stored.email == "dana@example.com"
    assert stored.asked_at == world.now
    assert world.logs == [{"severity": "NOTICE", "event": "pilot_request_stored"}]
    assert "dana" not in repr(world.logs).lower()


def test_the_page_lets_its_own_form_name_its_origin(world: World) -> None:
    """Under no-referrer a browser with JavaScript off posts ``Origin: null``, which is refused."""
    assert world.client.get("/").headers["referrer-policy"] == "same-origin"


@pytest.mark.parametrize(
    "origin", [None, "https://evil.example", "https://www.delimitus.com", "null"]
)
def test_a_post_from_another_origin_is_refused(world: World, origin: str | None) -> None:
    res = world.post(origin=origin)
    assert res.status_code == 403
    assert res.json()["error"] == "WRONG_ORIGIN"
    assert world.store.requests == []


def test_a_body_over_4_kb_is_refused(world: World) -> None:
    res = world.post({**GOOD, "tools": "x" * MAX_BODY})
    assert res.status_code == 413
    assert world.store.requests == []


def test_a_streamed_body_over_4_kb_is_refused_without_a_length(world: World) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"tools": "'
        yield b"x" * MAX_BODY
        yield b'"}'

    res = world.client.post(
        "/pilot-request",
        content=chunks(),
        headers={"origin": ORIGIN, "content-type": "application/json"},
    )
    assert res.status_code == 413


def test_a_sixth_request_in_a_minute_is_refused(world: World) -> None:
    codes = [world.post().status_code for _ in range(6)]
    assert codes == [200, 200, 200, 200, 200, 429]
    sixth = world.post()
    assert sixth.headers["retry-after"] == "60"
    assert world.post(client_ip="198.51.100.9").status_code == 200


def test_a_spoofed_forwarded_for_does_not_reset_the_limit(world: World) -> None:
    for i in range(5):
        headers = {"origin": ORIGIN, "x-forwarded-for": f"10.0.0.{i}, 203.0.113.7, 35.191.0.1"}
        assert world.client.post("/pilot-request", json=GOOD, headers=headers).status_code == 200
    headers = {"origin": ORIGIN, "x-forwarded-for": "10.9.9.9, 203.0.113.7, 35.191.0.1"}
    assert world.client.post("/pilot-request", json=GOOD, headers=headers).status_code == 429


def test_the_honeypot_answers_ok_and_stores_nothing(world: World) -> None:
    res = world.post({**GOOD, "website": "https://spam.example"})
    assert res.json() == {"ok": True}
    assert world.store.requests == []
    assert world.logs == [{"severity": "INFO", "event": "pilot_request_trapped"}]


def test_bad_fields_are_named_for_the_page(world: World) -> None:
    res = world.post({**GOOD, "email": "nope", "company": ""})
    assert res.status_code == 422
    body = res.json()
    assert body["ok"] is False
    assert body["error"] == "FIELDS_INVALID"
    assert body["fields"] == ["company", "email"]
    assert body["message"]


def test_a_plain_form_post_gets_a_page_back(world: World) -> None:
    res = world.client.post(
        "/pilot-request",
        data=GOOD,
        headers={"origin": ORIGIN, "x-forwarded-for": "203.0.113.7, 35.191.0.1"},
    )
    assert res.status_code == 200
    assert res.headers["content-type"] == "text/html; charset=utf-8"
    assert res.headers["content-security-policy"] == PLAIN_CSP
    assert "We have your request" in res.text
    assert len(world.store.requests) == 1


def test_a_plain_form_refusal_is_a_page_without_script(world: World) -> None:
    res = world.client.post(
        "/pilot-request",
        data={**GOOD, "company": ""},
        headers={"origin": ORIGIN},
    )
    assert res.status_code == 422
    assert "<script" not in res.text
    assert "Check the highlighted fields" in res.text


class _BrokenStore:
    async def add(self, request: PilotRequest) -> None:
        raise StoreError("Forbidden")


def test_a_store_failure_is_a_503_and_an_error_log() -> None:
    logs: list[Mapping[str, object]] = []
    app = create_app(PAGE, _BrokenStore(), privacy=PRIVACY, origin=ORIGIN, log=logs.append)
    client = TestClient(app, base_url=ORIGIN)
    res = client.post("/pilot-request", json=GOOD, headers={"origin": ORIGIN})
    assert res.status_code == 503
    assert res.json()["error"] == "NOT_STORED"
    assert logs == [
        {"severity": "ERROR", "event": "pilot_request_not_stored", "cause": "Forbidden"}
    ]


def test_settings_require_a_bucket_outside_dev() -> None:
    with pytest.raises(SettingsError, match="SSC_LANDING_BUCKET"):
        settings_from_env({"SSC_LANDING_PAGE": str(PAGE_PATH)})
    dev = settings_from_env({"SSC_LANDING_ENV": "dev", "SSC_LANDING_PAGE": str(PAGE_PATH)})
    assert dev.bucket is None


def test_settings_defaults_for_production() -> None:
    settings = settings_from_env(
        {"SSC_LANDING_PAGE": "/app/landing/index.html", "SSC_LANDING_BUCKET": "ssc-pilot-requests"}
    )
    assert settings.privacy == Path("/app/landing/privacy.html")
    assert settings.origin == "https://delimitus.com"
    assert settings.redirect_hosts == frozenset({"www.delimitus.com"})
    assert settings.trusted_hops == 2
    assert settings.port == 8080


def test_settings_refuse_a_plain_http_origin_in_production() -> None:
    with pytest.raises(SettingsError, match="https"):
        settings_from_env(
            {
                "SSC_LANDING_PAGE": "p",
                "SSC_LANDING_BUCKET": "b",
                "SSC_LANDING_ORIGIN": "http://delimitus.com",
            }
        )


def test_settings_take_the_privacy_page_from_the_env() -> None:
    settings = settings_from_env(
        {"SSC_LANDING_ENV": "dev", "SSC_LANDING_PAGE": "p", "SSC_LANDING_PRIVACY_PAGE": "/x/q.html"}
    )
    assert settings.privacy == Path("/x/q.html")


def test_the_service_refuses_a_privacy_page_that_breaks_the_policy(tmp_path: Path) -> None:
    privacy = tmp_path / "privacy.html"
    privacy.write_text('<p style="color:red">x</p>', encoding="utf-8")
    settings = settings_from_env(
        {
            "SSC_LANDING_ENV": "dev",
            "SSC_LANDING_PAGE": str(PAGE_PATH),
            "SSC_LANDING_PRIVACY_PAGE": str(privacy),
        }
    )
    with pytest.raises(PageError, match="style="):
        production_app(settings)
    privacy.write_text(f'<a href="{USER_DATA_POLICY}">x</a>', encoding="utf-8")
    production_app(settings)


def test_the_service_refuses_a_missing_privacy_page(tmp_path: Path) -> None:
    settings = Settings(
        page=PAGE_PATH,
        privacy=tmp_path / "missing.html",
        origin=ORIGIN,
        redirect_hosts=frozenset(),
        bucket=None,
        trusted_hops=0,
        port=8080,
    )
    with pytest.raises(FileNotFoundError):
        production_app(settings)


def test_the_image_ships_only_the_venv_and_the_pages() -> None:
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    last = re.split(r"^FROM ", dockerfile, flags=re.M)[-1]
    copies = [" ".join(ln.split()[1:]) for ln in last.splitlines() if ln.startswith("COPY ")]
    assert copies == [
        "--from=builder /app/.venv /app/.venv",
        "landing/index.html /app/landing/index.html",
        "landing/privacy.html /app/landing/privacy.html",
    ]
    ignore = (Path(__file__).resolve().parents[1] / "Dockerfile.dockerignore").read_text(
        encoding="utf-8"
    )
    assert "!landing/privacy.html" in ignore.splitlines()
    assert all("@sha256:" in line for line in re.findall(r"^FROM .*", dockerfile, flags=re.M))
