"""The egress proxy's Envoy config, rendered and run in real Envoy 1.39 (SSC-053).

Envoy and a TCP echo server, answering on port 443 under several host names, run in Docker; the
tests speak raw ``CONNECT`` to the proxy, as Node's ``fetch`` does with ``HTTPS_PROXY``. Without
Docker these tests skip locally and fail in CI.
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from egress_world import (
    NEXT_TOKEN,
    PREVIEW,
    PREVIEW_TOKEN,
    PROD,
    STOPPED,
    STOPPED_TOKEN,
    TOKEN,
    snapshot,
)

from ssc_contracts.egress import (
    BUILD_HOSTS,
    BUILD_PLAIN_HOSTS,
    BUILD_PROXY_PORT,
    BUILD_USER,
    DRAIN_SECONDS,
    MAX_HOSTS,
    authority_regex,
    plain_authority_regex,
    token_digest,
)
from ssc_egress.envoy import (
    BUILD_LISTENER,
    ENVOY_VERSION,
    LDS_FILE,
    PUBLIC_RESOLVERS,
    REALM,
    USER_HEADER,
    BuildListener,
    EgressConfig,
    Policy,
    bootstrap,
    build_listener,
    envoy_args,
    lds_document,
    listener,
    policy_of,
)
from ssc_shared.access import AccessView

# pytest makes its folders 0700 for the CI user; Linux Docker keeps that in a mount, and Envoy
# (uid 101) then fails with `Invalid path`. Docker Desktop hides it.
READABLE = 0o755
ENVOY_IMAGE = (
    f"envoyproxy/envoy:v{ENVOY_VERSION}"
    "@sha256:d59f7f5fa10cff6d5892b6c5e7df5c9297ddfb2c3683e33fbfb82da24de4fa66"
)
ECHO_IMAGE = (
    "python:3.11.15-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff"
)
ECHO = """
import socketserver
import threading

class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while data := self.request.recv(4096):
            self.request.sendall(data)

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

class Plain(socketserver.BaseRequestHandler):
    def handle(self):
        head = b""
        while b"\\r\\n\\r\\n" not in head and (data := self.request.recv(4096)):
            head += data
        body = head.lower()
        self.request.sendall(
            b"HTTP/1.1 200 OK\\r\\ncontent-length: %d\\r\\n\\r\\n" % len(body) + body
        )

threading.Thread(target=Server(("0.0.0.0", 80), Plain).serve_forever, daemon=True).start()
Server(("0.0.0.0", 443), Echo).serve_forever()
"""
ALIASES = (
    "api.allowed.test", "b.wild.test", "x.y.wild.test", "wild.test", "other.test", "plain.test"
)  # fmt: skip
PROD_USER = f"{PROD}.prod00000001"
BUILD_TOKEN = "build-token-" + "b" * 32  # noqa: S105  (a test credential)
BUILD = BuildListener(
    user=BUILD_USER,
    sha1=token_digest(BUILD_TOKEN),
    hosts=("api.allowed.test",),
    plain_hosts=("plain.test",),
)


def view(**changes: Any) -> AccessView:
    return AccessView.from_document(snapshot(**changes))


def hcm_of(resource: dict[str, Any]) -> dict[str, Any]:
    return resource["filter_chains"][0]["filters"][0]["typed_config"]


def test_only_active_environments_credentials_are_listed_in_order() -> None:
    policy = policy_of(view())
    assert policy.hosts == ("*.wild.test", "api.allowed.test")
    assert [u for u, _ in policy.users] == [
        PROD_USER,
        f"{PROD}.prod00000002",
        f"{PREVIEW}.prev00000001",
    ]
    assert all(STOPPED not in u for u, _ in policy.users)
    assert dict(policy.users)[PROD_USER] == token_digest(TOKEN)


def test_the_listener_authenticates_first_and_refuses_what_no_route_allows() -> None:
    hcm = hcm_of(listener(EgressConfig(), policy_of(view())))
    assert [f["name"] for f in hcm["http_filters"]] == [
        "envoy.filters.http.basic_auth",
        "envoy.filters.http.dynamic_forward_proxy",
        "envoy.filters.http.router",
    ]
    auth = hcm["http_filters"][0]["typed_config"]
    assert auth["authentication_header"] == "proxy-authorization"
    assert auth["forward_username_header"] == USER_HEADER
    assert f"{PROD_USER}:{{SHA}}{token_digest(TOKEN)}\n" in auth["users"]["inline_string"]
    assert TOKEN not in json.dumps(hcm)
    assert hcm["codec_type"] == "HTTP1"
    assert hcm["upgrade_configs"] == [{"upgrade_type": "CONNECT"}]
    routes = hcm["route_config"]["virtual_hosts"][0]["routes"]
    assert [r["name"] for r in routes] == ["allow-0", "allow-1", "refuse", "not-connect"]
    for allow in routes[:2]:
        assert "connect_matcher" in allow["match"]
        assert allow["route"]["upgrade_configs"] == [
            {"upgrade_type": "CONNECT", "connect_config": {}}
        ]
    assert routes[2]["direct_response"] == {"status": 403}
    assert routes[3]["direct_response"] == {"status": 405}


def test_with_no_credential_every_request_needs_one() -> None:
    hcm = hcm_of(listener(EgressConfig(), Policy(("api.allowed.test",), ())))
    assert [f["name"] for f in hcm["http_filters"]][0] != "envoy.filters.http.basic_auth"
    connect, plain = hcm["route_config"]["virtual_hosts"][0]["routes"]
    assert connect["match"] == {"connect_matcher": {}}
    assert connect["direct_response"] == plain["direct_response"] == {"status": 401}


def test_the_bootstrap_resolves_through_public_dns_over_tcp_and_ipv4_only() -> None:
    (cluster,) = bootstrap(EgressConfig())["static_resources"]["clusters"]
    cache = cluster["cluster_type"]["typed_config"]["dns_cache_config"]
    assert cache["dns_lookup_family"] == "V4_ONLY"
    resolver = cache["typed_dns_resolver_config"]["typed_config"]
    assert [r["socket_address"]["address"] for r in resolver["resolvers"]] == list(PUBLIC_RESOLVERS)
    assert resolver["dns_resolver_options"]["use_tcp_for_dns_lookups"] is True
    assert resolver["use_resolvers_as_fallback"] is False
    assert "admin" not in bootstrap(EgressConfig())
    assert envoy_args("/c/envoy.json")[-4:] == [
        "--drain-time-s",
        str(DRAIN_SECONDS),
        "--drain-strategy",
        "immediate",
    ]


def test_the_listener_version_follows_its_content() -> None:
    cfg = EgressConfig()
    one = lds_document(cfg, policy_of(view()))
    assert one == lds_document(cfg, policy_of(view(version=2)))
    assert (
        one["version_info"] != lds_document(cfg, policy_of(view(hosts=("a.test",))))["version_info"]
    )


def test_without_build_settings_the_bootstrap_has_no_static_listener() -> None:
    plain = bootstrap(EgressConfig())
    assert "listeners" not in plain["static_resources"]
    with_build = bootstrap(EgressConfig(build=BUILD))
    (resource,) = with_build["static_resources"].pop("listeners")
    assert json.dumps(with_build) == json.dumps(plain)
    assert resource["name"] == BUILD_LISTENER
    assert resource["address"]["socket_address"]["port_value"] == BUILD_PROXY_PORT


def test_the_build_listener_routes_are_the_build_hosts_and_nothing_else() -> None:
    build = BuildListener(user=BUILD_USER, sha1=token_digest(BUILD_TOKEN))
    hcm = hcm_of(build_listener(EgressConfig(), build))
    routes = hcm["route_config"]["virtual_hosts"][0]["routes"]
    connect = [r for r in routes if r["name"].startswith("allow-")]
    plain = [r for r in routes if r["name"].startswith("plain-")]
    assert [r["match"]["headers"][0]["string_match"]["safe_regex"]["regex"] for r in connect] == [
        authority_regex(h) for h in BUILD_HOSTS
    ]
    assert all("connect_matcher" in r["match"] for r in connect)
    assert [r["match"]["headers"][0]["string_match"]["safe_regex"]["regex"] for r in plain] == [
        plain_authority_regex(h) for h in BUILD_PLAIN_HOSTS
    ]
    for route in plain:
        assert route["match"]["headers"][1]["name"] == ":method"
        assert "proxy-authorization" in route["request_headers_to_remove"]
        assert "upgrade_configs" not in route["route"]
    assert [r["name"] for r in routes[-2:]] == ["refuse", "refuse-plain"]
    assert routes[-2]["direct_response"] == routes[-1]["direct_response"] == {"status": 403}
    auth = hcm["http_filters"][0]["typed_config"]
    assert auth["users"]["inline_string"] == f"{BUILD_USER}:{{SHA}}{token_digest(BUILD_TOKEN)}\n"
    assert BUILD_TOKEN not in json.dumps(hcm)
    assert hcm["http_protocol_options"] == {"allow_absolute_url": True}


def test_the_org_listener_never_lists_the_build_user() -> None:
    cfg = EgressConfig(build=BUILD)
    org = json.dumps(lds_document(cfg, policy_of(view())))
    assert token_digest(BUILD_TOKEN) not in org
    assert f"{BUILD_USER}:" not in org
    assert lds_document(cfg, policy_of(view())) == lds_document(EgressConfig(), policy_of(view()))


def docker() -> str:
    found = shutil.which("docker")
    if (
        found is None
        or subprocess.run([found, "info"], capture_output=True, check=False).returncode
    ):
        if os.environ.get("CI"):
            pytest.fail("Docker is required in CI for the Envoy tests")
        pytest.skip("Docker is not available")
    return found


def run(*args: str, stdin: bytes | None = None) -> str:
    out = subprocess.run([docker(), *args], capture_output=True, input=stdin, check=False)
    if out.returncode:
        raise AssertionError(f"docker {args[0]} failed: {out.stderr.decode()[-2000:]}")
    return out.stdout.decode().strip()


def static(cfg: EgressConfig, policy: Policy) -> dict[str, Any]:
    """The bootstrap with the listener inline, which ``--mode validate`` checks fully."""
    config = bootstrap(cfg)
    del config["dynamic_resources"]
    resource = {k: v for k, v in listener(cfg, policy).items() if k != "@type"}
    static = config["static_resources"]
    static["listeners"] = [resource, *static.get("listeners", [])]
    return config


LONGEST = "*." + ".".join(["a" * 61] * 4)


@pytest.mark.parametrize(
    "policy",
    [
        Policy(),
        Policy(("api.allowed.test",), ()),
        policy_of(view()),
        Policy((LONGEST,), ((PROD_USER, token_digest(TOKEN)),)),
        Policy(
            tuple(f"*.h{i}.example.com" for i in range(MAX_HOSTS)),
            ((PROD_USER, token_digest(TOKEN)),),
        ),
    ],
    ids=["nothing", "no-credentials", "snapshot", "longest-host", "most-hosts"],
)
@pytest.mark.parametrize(
    "build",
    [None, BuildListener(user=BUILD_USER, sha1=token_digest(BUILD_TOKEN))],
    ids=["org", "with-build"],
)
def test_the_rendered_config_validates(
    tmp_path: Path, policy: Policy, build: BuildListener | None
) -> None:
    assert len(LONGEST) <= 253  # noqa: PLR2004
    (tmp_path / "envoy.json").write_text(json.dumps(static(EgressConfig(build=build), policy)))
    tmp_path.chmod(READABLE)
    out = subprocess.run(
        [docker(), "run", "--rm", "-v", f"{tmp_path}:/c:ro", ENVOY_IMAGE]
        + ["--mode", "validate", "-c", "/c/envoy.json"],
        capture_output=True,
        text=True,
        check=False,
    )
    text = out.stdout + out.stderr
    assert "configuration '/c/envoy.json' OK" in text, text[-2000:]
    assert "deprecated" not in text.lower()


@dataclass
class Proxy:
    port: int
    name: str
    cfg: EgressConfig
    build_port: int

    def apply(self, policy: Policy) -> None:
        """Moves a new listener into place inside the container, as the runner does."""
        body = json.dumps(lds_document(self.cfg, policy)).encode()
        lds = self.cfg.lds_dir
        script = f"cat > {lds}/.next && mv {lds}/.next {lds}/{LDS_FILE}"
        run("exec", "-i", self.name, "sh", "-c", script, stdin=body)

    def connect(
        self,
        authority: str,
        user: str | None = PROD_USER,
        token: str = TOKEN,
        port: int | None = None,
    ) -> tuple[socket.socket, int, str]:
        """A ``CONNECT``; the open socket, the status and the response body (empty on 200)."""
        sock = socket.create_connection(("127.0.0.1", port or self.port), timeout=10)
        head = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
        if user is not None:
            basic = base64.b64encode(f"{user}:{token}".encode()).decode()
            head += f"Proxy-Authorization: Basic {basic}\r\n"
        sock.sendall((head + "\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data and (chunk := sock.recv(4096)):
            data += chunk
        status_line, _, rest = data.partition(b"\r\n")
        status = int(status_line.split()[1])
        headers, _, body = rest.partition(b"\r\n\r\n")
        if status != 200:  # noqa: PLR2004
            sock.settimeout(2)
            while chunk := sock.recv(4096):
                body += chunk
            if f"proxy-authenticate: {REALM}".lower().encode() in headers.lower():
                body = b"[proxy-authenticate] " + body
        return sock, status, body.decode()

    def status(self, authority: str, **kw: Any) -> int:
        sock, status, _ = self.connect(authority, **kw)
        sock.close()
        return status

    def echoes(self, authority: str, **kw: Any) -> bool:
        sock, status, _ = self.connect(authority, **kw)
        with sock:
            if status != 200:  # noqa: PLR2004
                return False
            sock.sendall(b"ping")
            return sock.recv(16) == b"ping"


def wait_until(check: Any, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.1)


@pytest.fixture(scope="module")
def proxy(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Proxy]:
    docker()
    tmp = tmp_path_factory.mktemp("egress")
    tmp.chmod(READABLE)
    (tmp / "lds").mkdir()
    (tmp / "echo.py").write_text(ECHO)
    cfg = EgressConfig(lds_dir="/lds", resolvers=(), build=BUILD)
    (tmp / "envoy.json").write_text(json.dumps(bootstrap(cfg)))
    (tmp / "lds" / LDS_FILE).write_text(json.dumps(lds_document(cfg, policy_of(view()))))
    tag = f"ssc053-{os.getpid()}"
    run("network", "create", tag)
    try:
        aliases = [arg for a in ALIASES for arg in ("--network-alias", a)]
        run(
            "run", "-d", "--rm", "--name", f"{tag}-echo", "--network", tag, *aliases,
            "-v", f"{tmp}:/c:ro", ECHO_IMAGE, "python", "/c/echo.py",
        )  # fmt: skip
        run(
            "run", "-d", "--rm", "--name", f"{tag}-envoy", "--network", tag,
            "-p", "127.0.0.1::3128", "-p", f"127.0.0.1::{BUILD_PROXY_PORT}",
            "-v", f"{tmp}:/c:ro", "-v", f"{tmp / 'lds'}:/lds",
            ENVOY_IMAGE, *envoy_args("/c/envoy.json"),
        )  # fmt: skip
        port, build_port = (
            int(run("port", f"{tag}-envoy", f"{p}/tcp").splitlines()[0].rsplit(":", 1)[1])
            for p in (3128, BUILD_PROXY_PORT)
        )
        found = Proxy(port=port, name=f"{tag}-envoy", cfg=cfg, build_port=build_port)

        def answers() -> bool:
            try:
                return found.status("api.allowed.test:443") == 200  # noqa: PLR2004
            except OSError, IndexError, ValueError:
                return False

        wait_until(answers, 30)
        yield found
    finally:
        for name in (f"{tag}-envoy", f"{tag}-echo"):
            subprocess.run([docker(), "rm", "-f", name], capture_output=True, check=False)
        subprocess.run([docker(), "network", "rm", tag], capture_output=True, check=False)


def test_a_listed_host_opens_a_tunnel_in_any_case(proxy: Proxy) -> None:
    assert proxy.echoes("api.allowed.test:443")
    assert proxy.echoes("API.Allowed.TEST:443")


def test_a_wildcard_stands_for_exactly_one_label(proxy: Proxy) -> None:
    assert proxy.echoes("b.wild.test:443")
    assert proxy.status("x.y.wild.test:443") == 403  # noqa: PLR2004
    assert proxy.status("wild.test:443") == 403  # noqa: PLR2004


def test_an_unlisted_host_is_refused_with_a_message_naming_it(proxy: Proxy) -> None:
    sock, status, body = proxy.connect("other.test:443")
    sock.close()
    assert status == 403  # noqa: PLR2004
    assert "refused other.test:443" in body
    assert "[egress] hosts" in body


@pytest.mark.parametrize(
    "authority", ["172.17.0.2:443", "8.8.8.8:443", "[::1]:443", "api.allowed.test:80"]
)
def test_a_raw_ip_address_or_another_port_is_refused(proxy: Proxy, authority: str) -> None:
    sock, status, body = proxy.connect(authority)
    sock.close()
    assert status == 403  # noqa: PLR2004
    assert authority in body


@pytest.mark.parametrize(
    "credential",
    [
        {"user": None},
        {"token": "wrong"},
        {"user": f"{STOPPED}.stop00000001", "token": STOPPED_TOKEN},
        {"user": f"{PREVIEW}.prev00000001", "token": TOKEN},
    ],
    ids=["none", "wrong-token", "stopped-app", "another-environment-s-token"],
)
def test_a_missing_wrong_or_stopped_credential_is_407(
    proxy: Proxy, credential: dict[str, Any]
) -> None:
    sock, status, body = proxy.connect("api.allowed.test:443", **credential)
    sock.close()
    assert status == 407  # noqa: PLR2004
    assert body.startswith("[proxy-authenticate] ")
    assert "credential" in body


def test_both_credentials_of_a_rotation_and_each_environment_s_own_work(proxy: Proxy) -> None:
    assert proxy.echoes("api.allowed.test:443", user=f"{PROD}.prod00000002", token=NEXT_TOKEN)
    assert proxy.echoes("api.allowed.test:443", user=f"{PREVIEW}.prev00000001", token=PREVIEW_TOKEN)


def test_plain_http_is_never_forwarded(proxy: Proxy) -> None:
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=10) as sock:
        basic = base64.b64encode(f"{PROD_USER}:{TOKEN}".encode()).decode()
        sock.sendall(
            b"GET http://api.allowed.test/ HTTP/1.1\r\nHost: api.allowed.test\r\n"
            + f"Proxy-Authorization: Basic {basic}\r\n\r\n".encode()
        )
        assert sock.recv(64).startswith(b"HTTP/1.1 405")


def test_removing_a_host_cuts_its_open_tunnel_within_the_drain_time(proxy: Proxy) -> None:
    sock, status, _ = proxy.connect("b.wild.test:443")
    assert status == 200  # noqa: PLR2004
    proxy.apply(policy_of(view(hosts=("api.allowed.test",))))
    # The clock starts once the new listener is in place: ``docker exec`` alone takes seconds
    # on a loaded machine, and that time is the test's, not the proxy's.
    started = time.monotonic()
    try:
        sock.settimeout(DRAIN_SECONDS + 10)
        while sock.recv(4096):
            pass
        cut = time.monotonic() - started
    finally:
        sock.close()
    assert cut <= DRAIN_SECONDS + 2, cut
    assert proxy.status("b.wild.test:443") == 403  # noqa: PLR2004
    assert proxy.echoes("api.allowed.test:443")
    proxy.apply(policy_of(view()))
    wait_until(lambda: proxy.status("b.wild.test:443") == 200)  # noqa: PLR2004


def test_revoking_a_credential_cuts_its_open_tunnel_within_the_drain_time(proxy: Proxy) -> None:
    sock, status, _ = proxy.connect("api.allowed.test:443")
    assert status == 200  # noqa: PLR2004
    doc = snapshot()
    doc["environments"][PROD]["status"] = "quarantined"
    proxy.apply(policy_of(AccessView.from_document(doc)))
    started = time.monotonic()
    try:
        sock.settimeout(DRAIN_SECONDS + 10)
        while sock.recv(4096):
            pass
        cut = time.monotonic() - started
    finally:
        sock.close()
    assert cut <= DRAIN_SECONDS + 2, cut
    assert proxy.status("api.allowed.test:443") == 407  # noqa: PLR2004
    proxy.apply(policy_of(view()))
    wait_until(lambda: proxy.status("api.allowed.test:443") == 200)  # noqa: PLR2004


def test_the_build_listener_tunnels_to_a_build_host_only(proxy: Proxy) -> None:
    build = {"user": BUILD_USER, "token": BUILD_TOKEN, "port": proxy.build_port}
    assert proxy.echoes("api.allowed.test:443", **build)
    sock, status, body = proxy.connect("b.wild.test:443", **build)
    sock.close()
    assert status == 403  # noqa: PLR2004
    assert "build network refused b.wild.test:443" in body
    for authority in ("8.8.8.8:443", "api.allowed.test:80", "plain.test:443"):
        assert proxy.status(authority, **build) == 403, authority  # noqa: PLR2004


def test_the_build_listener_takes_only_the_build_credential(proxy: Proxy) -> None:
    assert proxy.status("api.allowed.test:443", port=proxy.build_port) == 407  # noqa: PLR2004
    assert proxy.status("api.allowed.test:443", user=BUILD_USER, token=BUILD_TOKEN) == 407  # noqa: PLR2004


def plain_request(port: int, url: str, host: str, method: str = "GET") -> bytes:
    basic = base64.b64encode(f"{BUILD_USER}:{BUILD_TOKEN}".encode()).decode()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(
            f"{method} {url} HTTP/1.1\r\nHost: {host}\r\n"
            f"Proxy-Authorization: Basic {basic}\r\nConnection: close\r\n\r\n".encode()
        )
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
    return data


def test_plain_http_reaches_a_plain_host_without_the_credential(proxy: Proxy) -> None:
    answer = plain_request(proxy.build_port, "http://plain.test/dists/Release", "plain.test")
    assert answer.startswith(b"HTTP/1.1 200"), answer[:200]
    head = answer.partition(b"\r\n\r\n")[2]
    assert head.startswith(b"get /dists/release http/1.1")
    assert b"proxy-authorization" not in head
    assert USER_HEADER.encode() not in head


@pytest.mark.parametrize(
    ("method", "url", "host"),
    [
        ("GET", "http://api.allowed.test/", "api.allowed.test"),
        ("GET", "http://other.test/", "other.test"),
        ("POST", "http://plain.test/upload", "plain.test"),
        ("GET", "http://plain.test:8080/", "plain.test:8080"),
    ],
    ids=["connect-only-host", "unlisted-host", "post", "other-port"],
)
def test_any_other_plain_request_is_refused(proxy: Proxy, method: str, url: str, host: str) -> None:
    answer = plain_request(proxy.build_port, url, host, method)
    assert answer.startswith(b"HTTP/1.1 403"), answer[:200]
    assert b"build network refused" in answer
