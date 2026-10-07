"""SSC gap 1: what console.delimitus.com answers, and with which headers.

Checks:
  * exact files from dist                           -> test_a_file_of_dist_is_served_as_is
  * /assets/* immutable for a year, a miss is a 404 -> test_assets_are_kept_a_year,
                                                       test_a_missing_asset_is_not_the_page
  * any other path is index.html, no-store          -> test_app_routes_get_the_page_uncached
  * .. refused                                      -> test_paths_leaving_dist_are_refused
  * /v1 and /mcp never served here                  -> test_api_paths_are_never_served_here
  * /healthz                                        -> test_health
  * security headers on every response              -> test_every_answer_carries_the_headers
  * SSC_CONSOLE_AUTH_ORIGIN required                -> test_settings
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ssc_console_host.__main__ import SettingsError, settings_from_env
from ssc_console_host.app import (
    ASSET_CACHE,
    create_app,
    is_unsafe_path,
    security_headers,
)
from ssc_console_host.site import SiteError, load_site

AUTH = "https://auth.delimitus.com"
INDEX = b'<!doctype html><script type="module" src="/assets/index-abc.js"></script>'
SCRIPT = b"console.log(1)"
STYLE = b"body{color:red}"
ICON = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    f"connect-src 'self' {AUTH}; frame-ancestors 'none'; base-uri 'none'; "
    f"form-action 'self' {AUTH}; object-src 'none'"
)


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_bytes(INDEX)
    (root / "assets" / "index-abc.js").write_bytes(SCRIPT)
    (root / "assets" / "index-def.css").write_bytes(STYLE)
    (root / "favicon.svg").write_bytes(ICON)
    secret = tmp_path / "secret.txt"
    secret.write_text("not for the web")
    (root / "linked.txt").symlink_to(secret)
    return root


@pytest.fixture
def client(dist: Path) -> TestClient:
    app = create_app(load_site(dist), auth_origin=AUTH)
    return TestClient(app, base_url="https://console.delimitus.com")


def test_a_file_of_dist_is_served_as_is(client: TestClient) -> None:
    res = client.get("/favicon.svg")
    assert res.status_code == 200
    assert res.content == ICON
    assert res.headers["content-type"] == "image/svg+xml; charset=utf-8"
    assert res.headers["cache-control"] == "no-cache"
    again = client.get("/favicon.svg", headers={"if-none-match": res.headers["etag"]})
    assert again.status_code == 304


def test_assets_are_kept_a_year(client: TestClient) -> None:
    script = client.get("/assets/index-abc.js")
    assert script.status_code == 200
    assert script.content == SCRIPT
    assert script.headers["content-type"] == "text/javascript; charset=utf-8"
    assert script.headers["cache-control"] == ASSET_CACHE
    assert ASSET_CACHE == "public, max-age=31536000, immutable"
    style = client.get("/assets/index-def.css")
    assert style.headers["content-type"] == "text/css; charset=utf-8"
    head = client.head("/assets/index-abc.js")
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(SCRIPT))
    assert head.content == b""


def test_a_missing_asset_is_not_the_page(client: TestClient) -> None:
    res = client.get("/assets/index-old.js")
    assert res.status_code == 404
    assert res.content != INDEX


@pytest.mark.parametrize(
    "path", ["/", "/index.html", "/apps/x", "/auth/callback?code=c&state=s", "/approvals/1/"]
)
def test_app_routes_get_the_page_uncached(client: TestClient, path: str) -> None:
    res = client.get(path)
    assert res.status_code == 200
    assert res.content == INDEX
    assert res.headers["content-type"] == "text/html; charset=utf-8"
    assert res.headers["cache-control"] == "no-store"
    # no-store is never answered from a validator.
    again = client.get(path, headers={"if-none-match": res.headers["etag"]})
    assert again.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/%2e%2e/secret.txt",
        "/assets/%2e%2e/index.html",
        "/apps/%2E%2E/%2e%2e/etc/passwd",
        "/%2e/index.html",
        "/assets/..%5cindex-abc.js",
        "/linked.txt",
    ],
)
def test_paths_leaving_dist_are_refused(client: TestClient, path: str) -> None:
    res = client.get(path)
    assert res.status_code in {404, 200}
    assert b"not for the web" not in res.content
    if path != "/linked.txt":
        assert res.status_code == 404


def test_unsafe_paths() -> None:
    assert is_unsafe_path("/../x")
    assert is_unsafe_path("/a/..")
    assert is_unsafe_path("/a/./b")
    assert is_unsafe_path("/a\\b")
    assert is_unsafe_path("/a\x00b")
    assert not is_unsafe_path("/apps/x..y")
    assert not is_unsafe_path("/assets/index-abc.js")


def test_a_symlink_in_dist_is_not_a_file(dist: Path) -> None:
    assert "/linked.txt" not in load_site(dist).files


@pytest.mark.parametrize(
    "path", ["/v1", "/v1/", "/v1/apps", "/v1/whoami?x=1", "/mcp", "/mcp/", "/mcp/sse"]
)
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_api_paths_are_never_served_here(client: TestClient, path: str, method: str) -> None:
    res = client.request(method, path)
    assert res.status_code == 404
    assert res.content != INDEX


def test_lookalike_paths_are_app_routes(client: TestClient) -> None:
    assert client.get("/v10").content == INDEX
    assert client.get("/mcpx").content == INDEX


def test_health(client: TestClient) -> None:
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.text == "ok\n"


def test_only_reads_are_answered(client: TestClient) -> None:
    res = client.post("/apps/x")
    assert res.status_code == 405
    assert res.headers["allow"] == "GET, HEAD"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/apps/x"),
        ("GET", "/assets/index-abc.js"),
        ("GET", "/assets/missing.js"),
        ("GET", "/favicon.svg"),
        ("GET", "/healthz"),
        ("GET", "/v1/apps"),
        ("GET", "/%2e%2e/x"),
        ("PUT", "/"),
    ],
)
def test_every_answer_carries_the_headers(client: TestClient, method: str, path: str) -> None:
    res = client.request(method, path)
    for name, value in security_headers(AUTH).items():
        assert res.headers[name] == value
    assert res.headers["content-security-policy"] == EXPECTED_CSP
    assert res.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "no-referrer"
    assert "unsafe-inline" not in res.headers["content-security-policy"]
    assert "set-cookie" not in res.headers
    assert "server" not in res.headers


def test_a_dist_without_an_index_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SiteError, match="not a directory"):
        load_site(tmp_path / "missing")
    (tmp_path / "assets").mkdir()
    with pytest.raises(SiteError, match="no index.html"):
        load_site(tmp_path)


def test_settings() -> None:
    good = {"SSC_CONSOLE_AUTH_ORIGIN": AUTH, "SSC_CONSOLE_DIST": "/app/dist"}
    settings = settings_from_env(good)
    assert (settings.auth_origin, str(settings.dist), settings.port) == (AUTH, "/app/dist", 8080)
    assert settings_from_env({**good, "PORT": "9000"}).port == 9000
    with pytest.raises(SettingsError, match="SSC_CONSOLE_AUTH_ORIGIN is required"):
        settings_from_env({"SSC_CONSOLE_DIST": "/app/dist"})
    with pytest.raises(SettingsError, match="SSC_CONSOLE_DIST is required"):
        settings_from_env({"SSC_CONSOLE_AUTH_ORIGIN": AUTH})
    for bad in (f"{AUTH}/", f"{AUTH}/x", f"{AUTH}; script-src *", "auth.delimitus.com"):
        with pytest.raises(SettingsError, match="is an origin"):
            settings_from_env({**good, "SSC_CONSOLE_AUTH_ORIGIN": bad})
    local = "http://127.0.0.1:8001"
    with pytest.raises(SettingsError, match="must be https"):
        settings_from_env({**good, "SSC_CONSOLE_AUTH_ORIGIN": local})
    dev = settings_from_env({**good, "SSC_CONSOLE_AUTH_ORIGIN": local, "SSC_CONSOLE_ENV": "dev"})
    assert dev.auth_origin == local
    with pytest.raises(SettingsError, match="PORT"):
        settings_from_env({**good, "PORT": "x"})
