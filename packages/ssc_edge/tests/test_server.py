"""The ext_authz service (SSC-018): what Envoy sends in, what goes back, and failing closed."""

import json
from typing import Any

import pytest
from edge_world import HOST, LABEL, NOWHERE_HOST, ORG, World
from fastapi.testclient import TestClient

from ssc_edge import pages
from ssc_edge.gate import IDENTITY_HEADER, UPSTREAM_HEADER, Gate
from ssc_edge.keys import new_keyring
from ssc_edge.server import SERVERLESS_AUTH, SettingsError, create_app, settings_from_env


class Tokens:
    def __init__(self, fail: bool = False) -> None:
        self.audiences: list[str] = []
        self.fail = fail

    async def identity(self, audience: str) -> str:
        if self.fail:
            raise RuntimeError("metadata server down")
        self.audiences.append(audience)
        return "google.id.token"


def client(gate: Gate | None, tokens: Tokens | None = None) -> TestClient:
    return TestClient(create_app(lambda: gate, tokens=tokens), raise_server_exceptions=False)


def test_an_allowed_check_returns_the_headers_envoy_forwards(world: World) -> None:
    tokens = Tokens()
    r = client(world.gate(), tokens).get(
        "/authz/books?y=1", headers={"host": HOST, "cookie": world.cookie()}
    )
    assert r.status_code == 200 and r.content == b""
    upstream = r.headers[UPSTREAM_HEADER]
    assert r.headers[SERVERLESS_AUTH] == "Bearer google.id.token"
    assert tokens.audiences == [f"https://{upstream}"]
    assert r.headers[IDENTITY_HEADER].count(".") == 2


def test_the_original_path_query_and_declared_length_reach_the_gate(world: World) -> None:
    seen: list[Any] = []
    gate = world.gate()
    real = gate.check

    async def spy(f):  # noqa: ANN001, ANN202
        seen.append(f)
        return await real(f)

    gate.check = spy  # type: ignore[method-assign]
    client(gate).post(
        "/authz/a%2Fb/c?x=1&y=%20",
        headers={"host": HOST, "x-ssc-content-length": "12", "content-length": "0"},
    )
    (f,) = seen
    assert (f.method, f.host, f.path) == ("POST", HOST, "/a%2Fb/c?x=1&y=%20")
    assert f.headers["content-length"] == "12" and "x-ssc-content-length" not in f.headers


def test_a_refusal_goes_back_as_it_is(world: World) -> None:
    r = client(world.gate()).get(
        "/authz/", headers={"host": NOWHERE_HOST, "cookie": world.cookie(host=NOWHERE_HOST)}
    )
    assert r.status_code == 404 and r.content == pages.NOT_FOUND
    assert r.headers["cache-control"] == "no-store"
    login = client(world.gate()).get(
        "/authz/",
        headers={"host": HOST, "cookie": "__Host-ssc-session=bad"},
        follow_redirects=False,
    )
    assert login.status_code == 302 and "Max-Age=0" in login.headers["set-cookie"]


@pytest.mark.parametrize("case", ["not_started", "gate_raises", "token_fails"])
def test_every_failure_is_unavailable(world: World, case: str) -> None:
    gate: Gate | None = world.gate()
    tokens = Tokens(fail=case == "token_fails")
    if case == "not_started":
        gate = None
    elif case == "gate_raises" and gate is not None:

        async def boom(_):  # noqa: ANN001, ANN202
            raise RuntimeError("bug")

        gate.check = boom  # type: ignore[method-assign]
    r = client(gate, tokens).get("/authz/", headers={"host": HOST, "cookie": world.cookie()})
    assert r.status_code == 503 and r.content == pages.UNAVAILABLE
    assert IDENTITY_HEADER not in r.headers


def test_healthz(world: World) -> None:
    assert client(world.gate()).get("/healthz").json() == {"status": "ok"}


def env(**changes: str) -> dict[str, str]:
    base = {
        "SSC_ORG_ID": ORG,
        "SSC_CELL_LABEL": LABEL,
        "SSC_PROJECT_NUMBER": "123456789012",
        "SSC_CELL_BUCKET": f"ssc-c-{LABEL}-cell",
        "SSC_GATEWAY_KEYRING": "Y2lwaGVy",
        "SSC_GATEWAY_KMS_KEY": "projects/p/locations/l/keyRings/r/cryptoKeys/k",
    }
    base.update(changes)
    return {k: v for k, v in base.items() if v}


def test_settings_defaults() -> None:
    s = settings_from_env(env())
    assert s.environment == "prod" and s.max_stale == 300.0
    assert s.gate.issuer == f"https://keys.delimitus.com/{LABEL}"
    assert s.gate.auth_url == "https://auth.delimitus.com"
    assert s.gate.apps_domain == "delimitusapps.com"
    assert s.gate.max_body_bytes == 32 * 1024 * 1024


def test_a_plain_keyring_only_in_dev_and_test() -> None:
    plain = new_keyring().decode()
    for environment in ("dev", "test"):
        assert settings_from_env(
            env(SSC_ENV=environment, SSC_GATEWAY_KEYRING_PLAIN=plain)
        ).keyring_plain
    for environment in ("prod", "staging", ""):
        with pytest.raises(SettingsError, match="dev or test"):
            settings_from_env(env(SSC_ENV=environment, SSC_GATEWAY_KEYRING_PLAIN=plain))


@pytest.mark.parametrize(
    "missing",
    [
        "SSC_ORG_ID",
        "SSC_CELL_LABEL",
        "SSC_PROJECT_NUMBER",
        "SSC_CELL_BUCKET",
        "SSC_GATEWAY_KMS_KEY",
    ],
)
def test_required_settings(missing: str) -> None:
    with pytest.raises(SettingsError, match=missing):
        settings_from_env(env(**{missing: ""}))


def test_numbers_are_checked() -> None:
    with pytest.raises(SettingsError):
        settings_from_env(env(SSC_GATEWAY_MAX_BODY="lots"))
    assert json.dumps(settings_from_env(env(SSC_SNAPSHOT_MAX_AGE="5")).max_stale) == "5.0"
