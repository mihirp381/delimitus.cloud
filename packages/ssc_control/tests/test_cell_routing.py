"""Decision 029: each org reaches its own cell, and only its own.

Ticket "done when" checks:
  * two orgs on two cells through one API and one worker, each call to its org's agent with
        its org in ``X-SSC-Org`` -> test_the_api_reaches_each_org_s_cell_alone,
        test_the_worker_reaches_each_org_s_cell_alone
  * an org whose cell is not configured: the API answers ``CELL_UNAVAILABLE``, the tick skips
        it and serves the others (its deployments and builds fail, test_deploy)
        -> test_the_api_reaches_each_org_s_cell_alone, test_the_worker_reaches_each_org_s_cell_alone
  * ``SSC_CELLS`` and the deprecated one-cell variables -> test_ssc_cells_*, test_the_legacy_*
  * a one-cell identity is the identity before placement, byte for byte
        -> test_a_cell_s_identity_is_the_one_cell_identity_byte_for_byte
"""

import asyncio
import base64
import json
import logging
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from ssc_testkit import ISSUER, Dsns, SigningKey, auth, mint, new_key
from test_worker import FAST, LiveEnv, fresh_db, live_env, queue_count, until

import ssc_control.db
from ssc_contracts.app_env import APP_ORIGIN, IDENTITY_KEYS_URL
from ssc_contracts.manifest import default_manifest
from ssc_control.api import Settings, create_app
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER
from ssc_control.db import bind_org_sync
from ssc_control.metrics.collect import collect_all
from ssc_control.runtime.cells import (
    LABEL_SECONDS,
    CellConfig,
    CellRouter,
    CellUnavailableError,
    cells_from_env,
    legacy_cells,
    parse_cells,
)
from ssc_control.runtime.driver import AppIdentity, identity_env
from ssc_control.runtime.specs import ReleaseSpec, StaticReleaseSpecs
from ssc_control.worker import Ports, app_identity_from_env, build_app, run_worker
from ssc_shared import hosts
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.clock import SystemClock
from ssc_shared.runtime import ORG_HEADER, spec_from_wire

DOMAIN = "delimitusapps.com"
MASTER = bytes(range(32))
KEY = {"kty": "EC", "crv": "P-256", "kid": "gw-1", "x": "AA", "y": "BB", "alg": "ES256"}
JWKS = {"keys": [KEY]}
STACK_JWKS = json.dumps(JWKS, indent=2)
"""The cell stack's ``identity_jwks`` output as an operator pastes it: whitespace and member
order are not the compact form."""
OUTBOUND = "203.0.113.{}"

# ── the agents ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Call:
    host: str
    org: str | None
    path: str
    body: Mapping[str, Any]


@dataclass
class Agents:
    """Every cell's agent behind one transport, each serving one org as the real agent does
    (``WRONG_CELL`` for another). Runtime calls succeed; anything else answers 503."""

    orgs: Mapping[str, str]  # host -> the org its agent serves
    calls: list[Call] = field(default_factory=list[Call])
    refused: list[Call] = field(default_factory=list[Call])

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body: object = json.loads(request.content or b"{}")
        call = Call(
            request.url.host,
            request.headers.get(ORG_HEADER),
            request.url.path,
            body if isinstance(body, dict) else {},
        )
        if call.org != self.orgs.get(call.host):
            self.refused.append(call)
            return httpx2.Response(403, json={"code": "WRONG_CELL", "message": "another org"})
        self.calls.append(call)
        match call.path:
            case "/v1/runtime/observe":
                return httpx2.Response(200, json={"observation": None})
            case "/v1/runtime/apply":
                return httpx2.Response(200, json={"revision": "rev-1"})
            case "/v1/runtime/set_traffic" | "/v1/runtime/scale_to_zero":
                return httpx2.Response(200, json={})
            case "/v1/egress/info":
                index = sorted(self.orgs).index(call.host)
                return httpx2.Response(
                    200, json={"proxy_address": "10.20.4.10", "outbound_ip": OUTBOUND.format(index)}
                )
            case _:
                return httpx2.Response(503, json={"code": "TEST_AGENT", "message": "not here"})

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))

    def of(self, org: str) -> list[Call]:
        return [c for c in self.calls if c.org == org]


async def id_token(audience: str) -> str:
    return "id-token-for-" + audience


def label_of(dsn: str, org: str) -> str:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        row = conn.execute("select cell_label from ssc.org where id = %s", (org,)).fetchone()
    assert row is not None
    return str(row[0])


def owner_of(dsn: str, env: LiveEnv) -> str:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, env.org)
        row = conn.execute("select owner_user_id from ssc.app where id = %s", (env.app,)).fetchone()
    assert row is not None
    return str(row[0])


def configured(dsn: str, *served: LiveEnv) -> tuple[dict[str, CellConfig], Agents]:
    """The cells of ``served``'s orgs, and their agents."""
    labels = {e.org: label_of(dsn, e.org) for e in served}
    cells = {label: CellConfig(label=label, identity_jwks=JWKS) for label in labels.values()}
    agents = Agents({hosts.agent_host(label, DOMAIN): org for org, label in labels.items()})
    return cells, agents


def assert_each_org_reached_its_own_agent(dsn: str, agents: Agents, *served: LiveEnv) -> None:
    assert agents.refused == []
    for e in served:
        reached = {c.host for c in agents.of(e.org)}
        assert reached == {hosts.agent_host(label_of(dsn, e.org), DOMAIN)}


# ── the API ──────────────────────────────────────────────────────────────────


def test_the_api_reaches_each_org_s_cell_alone(dsns: Dsns, signing_key: SigningKey) -> None:
    a, b, lost = (live_env(dsns.app, name) for name in ("Route Api A", "Route Api B", "Lost Api"))
    for e in (a, b, lost):
        with psycopg.connect(dsns.app) as conn:
            bind_org_sync(conn, e.org)
            conn.execute(
                "insert into ssc.app_database (org_id, environment_id, host, port, "
                "connection_limit) values (%s, %s, '10.0.0.5', 5432, 10)",
                (e.org, e.env),
            )
    cells, agents = configured(dsns.app, a, b)
    settings = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
        metrics_key=MASTER,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        rt = app.state.runtime
        router = CellRouter(
            rt.engine,
            cells,
            apps_domain=DOMAIN,
            id_tokens=id_token,
            grant_tokens=id_token,
            client=agents.client(),
        )
        app.state.runtime = replace(rt, cells=router)

        def calls(e: LiveEnv) -> dict[str, tuple[int, Any]]:
            token = mint(signing_key, org=e.org, sub=owner_of(dsns.app, e))
            base = f"/v1/apps/{e.app}/environments/{e.env}"
            once = auth(token, **{IDEMPOTENCY_HEADER: new_key()})
            again = auth(token, **{IDEMPOTENCY_HEADER: new_key()})
            out = {
                "egress": client.get("/v1/egress", headers=auth(token)),
                "health": client.get(f"{base}/health", headers=auth(token)),
                "grant": client.post(f"{base}/secrets/API_KEY/grants", headers=once),
                "rotate": client.post(f"{base}/database/rotate", headers=again),
            }
            return {k: (r.status_code, r.json()) for k, r in out.items()}

        served = {e.org: calls(e) for e in (a, b)}
        refused = calls(lost)

    for e in (a, b):
        paths = {c.path for c in agents.of(e.org)}
        assert {"/v1/egress/info", "/v1/logs/health", "/v1/databases/rotate"} <= paths
        assert any(p.startswith("/v1/secrets/") for p in paths)
        for status, body in served[e.org].values():
            assert body.get("code") != "CELL_UNAVAILABLE", (status, body)
        ip = served[e.org]["egress"][1]["outbound_ip"]
        host = hosts.agent_host(label_of(dsns.app, e.org), DOMAIN)
        assert ip == OUTBOUND.format(sorted(agents.orgs).index(host))
    assert_each_org_reached_its_own_agent(dsns.app, agents, a, b)
    assert agents.of(lost.org) == []
    assert refused["egress"][0] == 200  # the allowlist, without the cell's address
    assert refused["egress"][1]["outbound_ip"] is None
    for name in ("health", "grant", "rotate"):
        status, body = refused[name]
        assert (status, body["code"]) == (503, "CELL_UNAVAILABLE"), name


# ── the worker ───────────────────────────────────────────────────────────────


async def test_the_worker_reaches_each_org_s_cell_alone(
    dsns: Dsns, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = fresh_db(dsns)
    a, b, lost = [
        await asyncio.to_thread(live_env, db.app, name)
        for name in ("Route Worker A", "Route Worker B", "Lost Worker")
    ]
    cells, agents = configured(db.app, a, b)
    signer = UrlSigner({"k1": MASTER}, active="k1", clock=SystemClock())
    store = FsBlobStore(tmp_path, signer=signer, base_url="https://blobs.test")
    engine = ssc_control.db.make_engine(db.app)
    client = agents.client()
    router = CellRouter(
        engine,
        cells,
        apps_domain=DOMAIN,
        id_tokens=id_token,
        grant_tokens=id_token,
        build_store=lambda _label: store,
        client=client,
    )
    specs = StaticReleaseSpecs(
        {e.release: ReleaseSpec(manifest=default_manifest()) for e in (a, b, lost)}
    )
    ports = Ports(engine=engine, cells=router, release_specs=specs)
    kept = replace(FAST, delete_jobs="never")
    caplog.set_level(logging.WARNING, logger="ssc_control.runtime.jobs")
    task = asyncio.create_task(
        run_worker(
            build_app(db.app, settings=kept), ports, settings=kept, install_signal_handlers=False
        )
    )
    try:
        applied = lambda e: any(  # noqa: E731
            c.path == "/v1/runtime/apply" and c.body["spec"]["service"] == e.service
            for c in agents.of(e.org)
        )
        await until(lambda: applied(a) and applied(b), 30)
        ticks = "task_name = 'runtime:reconcile_tick' and status = 'succeeded'"
        await until(lambda: queue_count(db, ticks) >= 2, 30)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    # The usage job, a kill switch's stop and a build's poll go the same way.
    assert await collect_all(engine, router, datetime.now(UTC)) == 0
    for e in (a, b):
        cell = await router.for_org(e.org)
        assert cell.runtime is not None and cell.build is not None
        await cell.runtime.scale_to_zero(e.service)
        with suppress(Exception):
            await cell.build.poll("ref-1")
    with pytest.raises(CellUnavailableError):
        await router.for_org(lost.org)
    await client.aclose()
    await engine.dispose()

    for e in (a, b):
        paths = {c.path for c in agents.of(e.org)}
        assert {
            "/v1/runtime/observe",
            "/v1/runtime/apply",
            "/v1/runtime/scale_to_zero",
            "/v1/usage/read",
            "/v1/build/poll",
        } <= paths
        (spec, *_) = [
            spec_from_wire(c.body["spec"]) for c in agents.of(e.org) if c.path.endswith("/apply")
        ]
        assert spec.service == e.service
        assert spec.labels["ssc-org"] == e.org
        label = label_of(db.app, e.org)
        assert spec.env[IDENTITY_KEYS_URL] == cells[label].keys_url
        assert f".{label}.{DOMAIN}" in spec.env[APP_ORIGIN]
    assert_each_org_reached_its_own_agent(db.app, agents, a, b)
    assert agents.of(lost.org) == []
    assert not [c for c in agents.calls if lost.service in json.dumps(c.body)]
    skipped = [r for r in caplog.records if r.getMessage().startswith("reconcile tick skipped an")]
    assert {getattr(r, "org_id", None) for r in skipped} == {lost.org}


async def test_an_org_s_label_is_read_once_per_period(dsns: Dsns) -> None:
    e = await asyncio.to_thread(live_env, dsns.app, "Route Cache")
    cells, agents = configured(dsns.app, e)
    engine = ssc_control.db.make_engine(dsns.app)
    now = [0.0]
    queries: list[str] = []
    router = CellRouter(
        engine,
        cells,
        apps_domain=DOMAIN,
        id_tokens=id_token,
        grant_tokens=id_token,
        client=agents.client(),
        clock=lambda: now[0],
    )

    def seen(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        if "cell_label" in statement:
            queries.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", seen)
    try:
        first = await router.for_org(e.org)
        assert await router.for_org(e.org) is first
        now[0] = LABEL_SECONDS - 1
        await router.for_org(e.org)
        assert len(queries) == 1
        now[0] = LABEL_SECONDS + 1
        assert await router.for_org(e.org) is first
        assert len(queries) == 2
        with pytest.raises(CellUnavailableError):
            await router.for_org("org_" + "z" * 20)
    finally:
        await engine.dispose()


# ── SSC_CELLS ────────────────────────────────────────────────────────────────


def test_ssc_cells_names_each_cell_and_its_public_keys() -> None:
    raw = json.dumps({"cellabcd01": {"identity_jwks": JWKS}, "cellwxyz02": {"identity_jwks": JWKS}})
    cells = parse_cells(raw)
    assert set(cells) == {"cellabcd01", "cellwxyz02"}
    assert cells["cellabcd01"] == CellConfig(label="cellabcd01", identity_jwks=JWKS)
    assert parse_cells("{}") == {}


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ("{", "must be JSON"),
        ("[]", "an object of cell label"),
        (json.dumps({"Cell-1": {"identity_jwks": JWKS}}), "is not a cell label"),
        (json.dumps({"cellabcd01": {}}), "identity_jwks"),
        (json.dumps({"cellabcd01": {"identity_jwks": JWKS, "url": "x"}}), "identity_jwks"),
        (json.dumps({"cellabcd01": {"identity_jwks": {"keys": []}}}), "non-empty"),
        (json.dumps({"cellabcd01": {"identity_jwks": {"keys": [{"kty": "EC"}]}}}), "kid"),
    ],
)
def test_ssc_cells_refuses_anything_else(raw: str, problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        parse_cells(raw)


def test_ssc_cells_never_echoes_a_private_key() -> None:
    private = {"keys": [{**KEY, "d": "SECRET-PART"}]}
    with pytest.raises(ValueError, match="private key") as caught:
        parse_cells(json.dumps({"cellabcd01": {"identity_jwks": private}}))
    assert "SECRET-PART" not in str(caught.value)


def legacy_env(label: str = "cellabcd01") -> dict[str, str]:
    return {
        "SSC_CELL_AGENT_URL": hosts.agent_url(label, DOMAIN),
        "SSC_IDENTITY_JWKS": STACK_JWKS,
        "SSC_IDENTITY_ISSUER": hosts.identity_issuer(label),
    }


def test_the_legacy_variables_are_one_cell(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    cells = cells_from_env(legacy_env(), DOMAIN)
    assert cells == {"cellabcd01": CellConfig(label="cellabcd01", identity_jwks=JWKS)}
    assert "deprecated" in caplog.text
    both = {**legacy_env(), "SSC_CELLS": json.dumps({"cellwxyz02": {"identity_jwks": JWKS}})}
    assert set(cells_from_env(both, DOMAIN)) == {"cellwxyz02"}
    assert cells_from_env({}, DOMAIN) == {}


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"SSC_IDENTITY_JWKS": ""}, "set SSC_CELLS"),
        ({"SSC_IDENTITY_ISSUER": "https://elsewhere.test/cellabcd01"}, "SSC_IDENTITY_ISSUER"),
        ({"SSC_CELL_AGENT_URL": hosts.agent_url("cellwxyz02", DOMAIN)}, "SSC_CELL_AGENT_URL"),
        ({"SSC_IDENTITY_JWKS": "{"}, "SSC_IDENTITY_JWKS"),
    ],
)
def test_the_legacy_variables_must_name_one_cell(changes: dict[str, str], problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        legacy_cells({**legacy_env(), **changes}, DOMAIN)


# ── the identity, unchanged ──────────────────────────────────────────────────


def identity_before_placement(env: Mapping[str, str]) -> AppIdentity:
    """``worker.app_identity_from_env`` as it was before decision 029, kept here verbatim (its
    error paths aside) so the identity every app's spec carries is pinned against it."""
    jwks, issuer = env["SSC_IDENTITY_JWKS"], env["SSC_IDENTITY_ISSUER"]
    parsed = json.loads(jwks)
    compact = json.dumps(parsed, separators=(",", ":"), sort_keys=True).encode()
    keys_url = "data:application/json;base64," + base64.b64encode(compact).decode()
    label = hosts.check_cell_label(issuer.removeprefix("https://keys.delimitus.com/"))
    return AppIdentity(
        keys_url=keys_url, cell_label=label, apps_domain=env.get("SSC_APPS_DOMAIN", DOMAIN)
    )


@pytest.mark.parametrize("label", ["cellabcd01", "bcdfghjkmnpq"])
def test_a_cell_s_identity_is_the_one_cell_identity_byte_for_byte(label: str) -> None:
    before = identity_before_placement(legacy_env(label))
    from_legacy = cells_from_env(legacy_env(label), DOMAIN)[label].identity(DOMAIN)
    # SSC_CELLS as the platform stack writes it: compact, keys sorted (control.cells_env).
    raw = json.dumps(
        {label: {"identity_jwks": json.loads(STACK_JWKS)}}, separators=(",", ":"), sort_keys=True
    )
    from_cells = parse_cells(raw)[label].identity(DOMAIN)
    fake = app_identity_from_env(legacy_env(label))
    assert before == from_legacy == from_cells == fake
    for env_name in ("prod", "preview"):
        pinned = identity_env(before, "ledger", env_name)
        assert {IDENTITY_KEYS_URL, APP_ORIGIN} <= set(pinned)
        assert identity_env(from_cells, "ledger", env_name) == pinned
        assert identity_env(from_legacy, "ledger", env_name) == pinned
