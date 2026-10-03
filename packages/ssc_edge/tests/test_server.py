"""The ext_authz service (SSC-018): what Envoy sends in, what goes back, and failing closed."""

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from edge_world import (
    HOST,
    LABEL,
    NOW,
    NOWHERE_HOST,
    ORG,
    PAY,
    PAY_HOST,
    PROD,
    FakeRedeemer,
    World,
    config,
    session,
    snapshot,
)
from fastapi.testclient import TestClient

from ssc_app.identity import verify
from ssc_edge import pages, server
from ssc_edge.gate import IDENTITY_HEADER, UPSTREAM_HEADER, WAKE_HEADER, Allow, Deny, Facts, Gate
from ssc_edge.keys import KeyringError, new_keyring, parse_keyring, public_jwks
from ssc_edge.server import (
    FRESH_WAIT,
    RECHECK_SECONDS,
    SERVERLESS_AUTH,
    SETTLED_SECONDS,
    OnDemandView,
    SettingsError,
    create_app,
    gate_for,
    load_keyring,
    production_app,
    settings_from_env,
)
from ssc_edge.session import wake_cookie
from ssc_edge.tokens import MetadataTokens
from ssc_shared.access import ViewHolder
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.canonical import canonical_bytes
from ssc_shared.clock import SystemClock
from ssc_shared.runtime import service_name
from ssc_shared.snapshot_feed import SnapshotFeed, latest_key, object_key


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


def test_the_id_token_is_for_the_environment_s_run_app_url_and_the_note_for_the_app(
    world: World,
) -> None:
    """The audience Cloud Run must accept (SSC-086 T3 checks it live)."""
    tokens = Tokens()
    r = client(world.gate(), tokens).get(
        "/authz/", headers={"host": HOST, "cookie": world.cookie()}
    )
    assert tokens.audiences == [f"https://{service_name(PROD)}-123456789012.us-central1.run.app"]
    keys = public_jwks(world.keyring)
    note = verify(
        r.headers[IDENTITY_HEADER],
        audience=f"https://{HOST}",
        keys=keys,
        issuer=f"https://keys.example.test/{LABEL}",
        now=NOW,
    )
    assert note.aud == f"https://{HOST}"


def test_a_page_load_s_wake_cookie_goes_back_beside_the_upstream_headers(world: World) -> None:
    page_load = {
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
        "accept": "text/html",
    }
    r = client(world.gate()).get(
        "/authz/", headers={"host": HOST, "cookie": world.cookie(), **page_load}
    )
    assert r.status_code == 200
    assert r.headers[WAKE_HEADER] == "1"
    assert r.headers.get_list("set-cookie") == [wake_cookie()]


def test_the_published_jwks_is_read() -> None:
    assert settings_from_env(env()).published_jwks is None
    assert settings_from_env(env(SSC_IDENTITY_JWKS='{"keys":[]}')).published_jwks == '{"keys":[]}'


async def test_the_gateway_starts_only_with_the_published_identity_keys() -> None:
    raw = new_keyring()
    mine = json.dumps(public_jwks(parse_keyring(raw)))
    other = json.dumps(public_jwks(parse_keyring(new_keyring())))
    tokens = MetadataTokens()
    try:
        for published in (None, mine):
            changes = {"SSC_IDENTITY_JWKS": published} if published else {}
            settings = settings_from_env(dev_env(raw, **changes))
            assert (await load_keyring(settings, tokens)).identity_kid == "i1"
        for published in (other, "not json", '{"keys":[{"kid":"i1"}]}'):
            settings = settings_from_env(dev_env(raw, SSC_IDENTITY_JWKS=published))
            with pytest.raises(KeyringError):
                await load_keyring(settings, tokens)
    finally:
        await tokens.aclose()


def test_the_public_jwks_holds_no_private_material() -> None:
    doc = public_jwks(parse_keyring(new_keyring()))
    (key,) = doc["keys"]
    assert set(key) == {"kty", "crv", "x", "y", "kid", "use", "alg"}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class CountingStore(FsBlobStore):
    def __init__(self, root: Path) -> None:
        signer = UrlSigner({"k1": b"edge-test-signing-key-" * 2}, active="k1", clock=SystemClock())
        super().__init__(root, signer=signer, base_url="http://blobs.test")
        self.reads: list[str] = []

    async def get(self, key: str) -> AsyncIterator[bytes]:
        self.reads.append(key)
        async for chunk in super().get(key):
            yield chunk


async def publish(store: FsBlobStore, version: int, **changes: Any) -> None:
    raw = canonical_bytes(snapshot(version, **changes))
    sha = hashlib.sha256(raw).hexdigest()
    key = object_key(ORG, version, sha)
    await store.put(key, raw)
    pointer = {"version": version, "key": key, "digest": f"sha256:{sha}"}
    await store.put(latest_key(ORG), canonical_bytes(pointer))


def on_demand(store: FsBlobStore, clock: Clock) -> OnDemandView:
    holder = ViewHolder(ORG)
    return OnDemandView(SnapshotFeed(store, holder, monotonic=clock), holder, max_stale=300)


async def test_a_check_reads_the_snapshot_again_only_after_two_seconds(tmp_path: Path) -> None:
    store, clock = CountingStore(tmp_path), Clock()
    await publish(store, 1)
    snap = on_demand(store, clock)
    assert await snap.first_read()
    read = len(store.reads)
    await publish(store, 2)
    clock.t += RECHECK_SECONDS - 0.1
    await snap.refresh()
    assert len(store.reads) == read
    view = snap.view()
    assert view is not None and view.version == 1
    clock.t += 0.2
    await snap.refresh()
    view = snap.view()
    assert view is not None and view.version == 2


async def test_checks_at_once_share_one_read(tmp_path: Path) -> None:
    store, clock = CountingStore(tmp_path), Clock()
    await publish(store, 1)
    snap = on_demand(store, clock)
    await snap.first_read()
    read = len(store.reads)
    clock.t += RECHECK_SECONDS + 1
    await asyncio.gather(*(snap.refresh() for _ in range(5)))
    assert store.reads[read:] == [latest_key(ORG)]


async def test_no_confirmed_read_for_five_minutes_fails_closed_until_one_succeeds(
    tmp_path: Path,
) -> None:
    store, clock = CountingStore(tmp_path), Clock()
    snap = on_demand(store, clock)
    assert await snap.first_read() is False
    assert snap.view() is None
    await publish(store, 1)
    await snap.refresh()
    assert snap.view() is not None
    await store.delete(latest_key(ORG))
    clock.t += 299
    await snap.refresh()
    assert snap.view() is not None
    clock.t += 2
    await snap.refresh()
    assert snap.view() is None
    await publish(store, 2)
    await snap.refresh()
    view = snap.view()
    assert view is not None and view.version == 2


class HangingStore(CountingStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.answer = asyncio.Event()

    async def get(self, key: str) -> AsyncIterator[bytes]:
        await self.answer.wait()
        async for chunk in super().get(key):
            yield chunk


async def test_a_bucket_that_hangs_at_start_delays_the_gateway_only_briefly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "FIRST_READ_WAIT", 0.2)
    store, clock = HangingStore(tmp_path), Clock()
    await publish(store, 1)
    snap = on_demand(store, clock)
    started_at = time.monotonic()
    assert await snap.first_read() is False
    assert time.monotonic() - started_at < 1
    assert snap.view() is None
    store.answer.set()
    await snap.refresh()
    view = snap.view()
    assert view is not None and view.version == 1
    assert store.reads.count(latest_key(ORG)) == 1
    await snap.aclose()


class SlowStore(CountingStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.delay = 0.0

    async def get(self, key: str) -> AsyncIterator[bytes]:
        await asyncio.sleep(self.delay)
        async for chunk in super().get(key):
            yield chunk


async def test_the_first_check_after_idle_waits_for_the_new_snapshot(
    tmp_path: Path, world: World
) -> None:
    store, clock = SlowStore(tmp_path), Clock()
    await publish(store, 1)
    snap = on_demand(store, clock)
    assert await snap.first_read()
    gate = gate_for(
        config(),
        world.keyring,
        view=snap.view,
        clock=lambda: world.now,
        refresh=snap.refresh,
    )
    cookie = world.cookie(session(), PAY_HOST)
    paying = Facts(method="GET", host=PAY_HOST, path="/", headers={"cookie": cookie})
    assert isinstance(await gate.check(paying), Allow)
    clock.t += SETTLED_SECONDS + 1
    await publish(store, 2, grants={**snapshot()["grants"], "env_" + "y" * 20: []})
    store.delay = FRESH_WAIT * 3
    refused = await gate.check(paying)
    assert isinstance(refused, Deny) and refused.reason == "not_granted"
    await snap.aclose()


async def test_steady_traffic_waits_at_most_the_short_wait(tmp_path: Path) -> None:
    store, clock = SlowStore(tmp_path), Clock()
    await publish(store, 1)
    snap = on_demand(store, clock)
    assert await snap.first_read()
    clock.t += RECHECK_SECONDS + 1
    await publish(store, 2)
    store.delay = 2.0
    started_at = time.monotonic()
    await snap.refresh()
    assert time.monotonic() - started_at < FRESH_WAIT + 0.2
    view = snap.view()
    assert view is not None and view.version == 1
    await snap.aclose()


@pytest.mark.parametrize(
    ("gap", "read_takes"),
    [(0.25, 0.0), (1.0, 0.0), (1.75, 0.0), (1.0, FRESH_WAIT * 2), (1.75, FRESH_WAIT * 2)],
)
async def test_a_removed_grant_is_refused_within_the_recheck_and_one_gap(
    tmp_path: Path, world: World, gap: float, read_takes: float
) -> None:
    """SSC-021: an awake gateway, a request every ``gap`` seconds, the grant removed just after a
    read confirmed the view. Reads slower than ``FRESH_WAIT`` cost at most one more gap."""
    store, clock = SlowStore(tmp_path), Clock()
    await publish(store, 1)
    snap = on_demand(store, clock)
    assert await snap.first_read()
    gate = gate_for(
        config(), world.keyring, view=snap.view, clock=lambda: world.now, refresh=snap.refresh
    )
    cookie = world.cookie(session(), PAY_HOST)
    paying = Facts(method="GET", host=PAY_HOST, path="/", headers={"cookie": cookie})
    assert isinstance(await gate.check(paying), Allow)
    await publish(store, 2, grants={**snapshot()["grants"], PAY: []})
    store.delay = read_takes
    removed_at = clock.t
    while isinstance(outcome := await gate.check(paying), Allow):
        await asyncio.sleep(read_takes)
        clock.t += gap
    assert isinstance(outcome, Deny) and outcome.reason == "not_granted"
    took = clock.t - removed_at
    assert took <= RECHECK_SECONDS + (2 if read_takes > FRESH_WAIT else 1) * gap
    assert took + read_takes < 5
    await snap.aclose()


def dev_env(raw: bytes, **changes: str) -> dict[str, str]:
    return env(
        SSC_ENV="test",
        SSC_GATEWAY_KEYRING_PLAIN=raw.decode(),
        SSC_GATEWAY_KEYRING="",
        SSC_GATEWAY_KMS_KEY="",
        SSC_APPS_DOMAIN="apps.test",
        SSC_IDENTITY_ISSUER=f"https://keys.example.test/{LABEL}",
        SSC_STREAM_PORT="0",
        **changes,
    )


def started(tmp_path: Path, *, published: bool) -> tuple[World, CountingStore, TestClient]:
    raw = new_keyring()
    store = CountingStore(tmp_path)
    if published:
        asyncio.run(publish(store, 1))
    app = production_app(dev_env(raw), store=store)
    world = World(keyring=parse_keyring(raw), view=None, redeemer=FakeRedeemer())
    world.now = int(time.time())
    return world, store, TestClient(app, raise_server_exceptions=False)


def test_the_gateway_reads_the_snapshot_before_its_first_request(tmp_path: Path) -> None:
    world, store, test_client = started(tmp_path, published=True)
    assert store.reads == []
    with test_client as c:
        assert store.reads[0] == latest_key(ORG) and len(store.reads) == 2
        time.sleep(0.3)
        assert len(store.reads) == 2
        r = c.get("/authz/", headers={"host": HOST, "cookie": world.cookie()})
        assert r.status_code == 200, r.text
        assert r.headers[UPSTREAM_HEADER].startswith(service_name(PROD))
        assert len(store.reads) == 2


def test_a_gateway_started_after_a_grant_went_refuses_its_first_request(tmp_path: Path) -> None:
    world, store, test_client = started(tmp_path, published=True)
    asyncio.run(publish(store, 2, grants={**snapshot()["grants"], PAY: []}))
    with test_client as c:
        r = c.get("/authz/", headers={"host": PAY_HOST, "cookie": world.cookie(host=PAY_HOST)})
        assert r.status_code == 404, r.text
        assert UPSTREAM_HEADER not in r.headers


def test_a_gateway_that_cannot_read_the_snapshot_at_start_refuses_everything(
    tmp_path: Path,
) -> None:
    world, store, test_client = started(tmp_path, published=False)
    with test_client as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        r = c.get("/authz/", headers={"host": HOST, "cookie": world.cookie()})
        assert r.status_code == 503 and r.content == pages.UNAVAILABLE
        assert IDENTITY_HEADER not in r.headers
    assert store.reads[0] == latest_key(ORG)
