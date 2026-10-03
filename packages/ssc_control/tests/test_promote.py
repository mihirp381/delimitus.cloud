"""SSC-042: promote builds for prod what preview runs, and is the only way to a prod release.

Uses test_deploy's bench: postgres:18, the fake build and runtime drivers; the cell build path
(the cell agent, ``CloudBuildDriver`` and the Cloud Build emulator); and test_app_databases' cell
agent on a postgres:18 set up the way Cloud SQL is.

  * same source as preview                -> test_promote_builds_preview_source_for_prod,
                                              test_promote_rebuilds_preview_source_on_the_cell
  * a failing gate starts no prod instance -> test_a_failing_gate_starts_no_prod_instance
                                              (scan, approval, production gate, DB_TIER_FULL,
                                              missing prod secret)
  * prod's own database and place          -> test_prod_takes_its_own_database_and_place
  * secrets never copied from preview      -> test_prod_runs_its_own_secrets_never_previews
  * pushes reach preview only              -> test_a_preview_token_builds_preview_on_the_cell_
                                              and_never_prod
  * prod only through promote              -> test_a_direct_prod_build_is_refused,
                                              test_a_prod_rollback_to_a_preview_build_is_refused
  * preview-only credentials               -> test_a_preview_scoped_token_cannot_promote
  * preconditions                          -> test_nothing_live_in_preview,
                                              test_a_stale_preview_release_id,
                                              test_a_stopped_app_cannot_promote,
                                              test_prod_in_flight_refuses_promote
  * replay                                 -> test_a_replay_returns_the_same_build
  * a failing gate starts no prod instance -> test_a_failing_prod_gate_starts_no_prod_instance
"""

from __future__ import annotations

import base64
import hashlib
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx2
import psycopg
import pytest
import test_app_databases
import test_deploy
from httpx import Response
from ssc_testkit import CloudSqlLike, Dsns, SigningKey, assert_problem, auth, mint, new_key
from test_app_databases import STATEFUL, Cell, database_ready, latest, pinned, run_through_the_cell
from test_deploy import (
    AGENT,
    CELL_BUILD,
    FIXTURES,
    MASTER,
    NIGHTLY,
    Bench,
    SpyGate,
    access_token,
    agent_token,
    audit_of,
    build_release,
    deploy,
    execute,
    get,
    manifest_of,
    operation,
    pointer,
    post,
    rows_of,
    run,
    seed_prod_build,
    set_prod_gate,
    start_build,
    start_deploy,
    stored_fixture,
    take_job,
)

from ssc_agent.app import create_app as create_agent
from ssc_agent.cloud_build import CloudBuildDriver
from ssc_conformance.cloud_build_emulator import CloudBuildEmulator
from ssc_contracts import app_database
from ssc_contracts.app_env import DATABASE_URL
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.idempotency import IDEMPOTENCY_HEADER, REPLAYED_HEADER
from ssc_control.deploy.build_driver import fake_image_digest
from ssc_control.deploy.builds import run_build
from ssc_control.deploy.cell_build import CellAgentBuildDriver
from ssc_control.deploy.deployments import APPROVAL_REQUIRED
from ssc_control.runtime.app_databases import FakeAppDatabases
from ssc_control.runtime.driver import service_name
from ssc_control.worker import Ports
from ssc_shared.blobstore_fs import UrlSigner
from ssc_shared.clock import SystemClock
from ssc_shared.runtime import database_name

world = test_deploy.world
tokens = test_deploy.tokens
b = test_deploy.b
instance = test_app_databases.instance
cell = test_app_databases.cell

PUBLIC_ENV = {
    "preview": {"VITE_API": "https://preview.example"},
    "prod": {"VITE_API": "https://prod.example"},
}


def promote(b: Bench, body: dict[str, Any] | None = None, token: str | None = None) -> Response:
    return post(b, f"/v1/apps/{b.w.app}/promote", body or {}, token)


async def live_in_preview(b: Bench, **tables: Any) -> str:
    """A release built for preview and healthy there."""
    release = await build_release(b, b.w.preview, manifest_of(**tables))
    _, state = await deploy(b, b.w.preview, release)
    assert state == "healthy"
    return release


def release_row(b: Bench, release: str) -> dict[str, Any]:
    (row,) = rows_of(b.dsn, b.w.org, "select * from ssc.release where id = %s", release)
    return row


def prod_builds(b: Bench) -> list[dict[str, Any]]:
    return rows_of(b.dsn, b.w.org, "select id from ssc.build where environment_id = %s", b.w.prod)


def set_secret_ref(b: Bench, env: str, name: str, version: str) -> None:
    """A secret as ``ssc secret set`` leaves it: a reference and a version, never a value."""
    execute(
        b.dsn,
        b.w.org,
        "insert into ssc.secret_ref (id, org_id, environment_id, name, secret_version) "
        "values (%s, %s, %s, %s, %s)",
        new_id("sec"),
        b.w.org,
        env,
        name,
        version,
    )


def prod_never_started(b: Bench) -> None:
    assert ("apply", service_name(b.w.prod)) not in b.runtime.calls
    assert service_name(b.w.prod) not in b.runtime.services
    assert pointer(b, b.w.prod) is None


# ── the cell build path ──────────────────────────────────────────────────────

PUBLIC_TOML = """
[build.public_env.preview]
VITE_API = "https://preview.example"

[build.public_env.prod]
VITE_API = "https://prod.example"
"""
FINANCE_TOML = """
[connections]
names = ["finance"]
"""
STATEFUL_TOML = """
[state]
postgres = true
"""


@dataclass
class OnTheCell:
    """Builds through the cell agent's ``CloudBuildDriver`` against the Cloud Build emulator."""

    ports: Ports
    emulator: CloudBuildEmulator
    bundle: str
    sha256: str

    async def finish(self, b: Bench, build: str) -> dict[str, Any]:
        for _ in range(5):
            take_job(b.dsn, f"bld:{build}")
            if await run_build(self.ports, org_id=b.w.org, build_id=build) != "running":
                break
        out: dict[str, Any] = get(b, f"/v1/builds/{build}").json()
        return out

    async def preview_release(self, b: Bench, token: str | None = None) -> str:
        r = start_build(b, b.w.preview, self.bundle, token)
        assert r.status_code == 202, r.text
        out = await self.finish(b, r.json()["build_id"])
        assert out["state"] == "succeeded", out
        return str(out["release_id"])

    def step_env(self, nth: int, step: str) -> dict[str, str]:
        build = list(self.emulator.builds.values())[nth]
        (found,) = [s for s in build["steps"] if s["id"] == step]
        return dict(e.replace("$$", "$").split("=", 1) for e in found["env"])


@asynccontextmanager
async def on_the_cell(b: Bench, root: Path, toml: str) -> AsyncIterator[OnTheCell]:
    """cs-fastapi-hello with ``toml`` added to its ``ssc.toml``, stored, built on the cell."""
    source = root / "source"
    shutil.copytree(FIXTURES / "cs-fastapi-hello", source)
    (source / "ssc.toml").write_text('schema = "ssc/v1"\n' + toml)
    ports, bundle = await stored_fixture(b, str(source), root)
    data = (root / "bundle.tar.gz").read_bytes()
    signer = UrlSigner({"k1": MASTER}, active="k1", clock=SystemClock())

    def fetch(url: str) -> bytes:
        parts = urlsplit(url)
        signer.verify("GET", parts.path.removeprefix("/v1/blobs/"), dict(parse_qsl(parts.query)))
        return data

    emulator = CloudBuildEmulator(fetch, polls=0)
    cloud_build = CloudBuildDriver(
        CELL_BUILD,
        access_token,
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(emulator.handler)),
    )
    agent = httpx2.ASGITransport(app=create_agent(b.runtime, cloud_build))
    builder = CellAgentBuildDriver(
        AGENT, agent_token, ports.blob_store, client=httpx2.AsyncClient(transport=agent)
    )
    try:
        yield OnTheCell(
            replace(ports, build_driver=builder),
            emulator,
            bundle,
            hashlib.sha256(data).hexdigest(),
        )
    finally:
        await builder.aclose()
        await cloud_build.aclose()


def public_env_of(step: dict[str, str]) -> dict[str, str]:
    raw = base64.b64decode(step["SSC_PUBLIC_ENV"]).decode()
    return dict(pair.split("=", 1) for pair in raw.split("\0") if pair)


@pytest.mark.parametrize("toml", [PUBLIC_TOML, ""], ids=["public-env", "identical-input"])
async def test_promote_rebuilds_preview_source_on_the_cell(
    b: Bench, tmp_path: Path, toml: str
) -> None:
    async with on_the_cell(b, tmp_path, toml) as cell:
        r1 = await cell.preview_release(b)
        assert (await deploy(b, b.w.preview, r1))[1] == "healthy"
        build = promote(b, {"preview_release_id": r1}).json()["build_id"]
        out = await cell.finish(b, build)
        assert out["state"] == "succeeded", out
        prod_release = str(out["release_id"])

        preview_row, prod_row = release_row(b, r1), release_row(b, prod_release)
        assert prod_row["source_digest"] == preview_row["source_digest"]
        assert prod_row["image_digest"] != preview_row["image_digest"]
        assert len(cell.emulator.builds) == 2
        fetched = [cell.step_env(i, "fetch")["SSC_BUNDLE_SHA256"] for i in (0, 1)]
        assert fetched == [cell.sha256, cell.sha256]
        planned = [public_env_of(cell.step_env(i, "plan")) for i in (0, 1)]
        if toml:
            assert planned == [PUBLIC_ENV["preview"], PUBLIC_ENV["prod"]]
        else:
            assert planned == [{}, {}]
        images = [b_["images"] for b_ in cell.emulator.builds.values()]
        assert images[0] != images[1]

        op, state = await deploy(b, b.w.prod, prod_release)
        assert state == "healthy"
        assert ("apply", service_name(b.w.prod)) in b.runtime.calls
        assert operation(b, op)["release_id"] == prod_release


FAILS_WITH = {
    "scan": "SECRET_IN_BUNDLE",
    "approval": APPROVAL_REQUIRED,
    "production-gate": APPROVAL_REQUIRED,
    "db-tier-full": "DB_TIER_FULL",
    "prod-secret": ErrorCode.PROD_SECRET_MISSING.value,
}


@pytest.mark.parametrize("gate", list(FAILS_WITH))
async def test_a_failing_gate_starts_no_prod_instance(
    b: Bench, tmp_path: Path, dsns: Dsns, gate: str
) -> None:
    toml = {"approval": FINANCE_TOML, "db-tier-full": STATEFUL_TOML}.get(gate, "")
    databases = FakeAppDatabases(ceiling=1)
    database_ready(dsns, b.w.org)
    async with on_the_cell(b, tmp_path, toml) as cell:
        ports = replace(cell.ports, app_databases=databases)
        r1 = await cell.preview_release(b)
        op = start_deploy(b, b.w.preview, r1).json()["operation_id"]
        assert await run(b, op, ports) == "healthy"

        if gate == "prod-secret":
            set_secret_ref(b, b.w.preview, "STRIPE_KEY", "1")
            assert_problem(promote(b), ErrorCode.PROD_SECRET_MISSING)
            assert prod_builds(b) == []
            assert len(cell.emulator.builds) == 1
            prod_never_started(b)
            return
        if gate == "scan":
            cell.emulator.fail(cell.sha256, 10)
        if gate == "production-gate":
            spy = SpyGate("refused")
            set_prod_gate(b, spy)
            ports = replace(ports, prod_gate=spy)
        out = await cell.finish(b, promote(b).json()["build_id"])
        if gate == "scan":
            assert (out["state"], out["failure_code"]) == ("failed", FAILS_WITH[gate])
            assert out["release_id"] is None
            prod_never_started(b)
            return
        assert out["state"] == "succeeded", out
        op = start_deploy(b, b.w.prod, out["release_id"]).json()["operation_id"]
        assert await run(b, op, ports) == "failed"
        assert operation(b, op)["failure_code"] == FAILS_WITH[gate]
        prod_never_started(b)
        if gate == "db-tier-full":
            assert databases.calls == [
                ("ensure", service_name(b.w.preview)),
                ("ensure", service_name(b.w.prod)),
            ]


async def test_a_preview_token_builds_preview_on_the_cell_and_never_prod(
    b: Bench, tmp_path: Path, signing_key: SigningKey
) -> None:
    """What the ssc-deploy Action does with its preview-scoped token, on the real build path."""
    scoped = mint(
        signing_key, org=b.w.org, sub=b.w.admin, jti=f"cred_{new_key()[:16]}", scope="preview"
    )
    async with on_the_cell(b, tmp_path, PUBLIC_TOML) as cell:
        r1 = await cell.preview_release(b, scoped)
        op = start_deploy(b, b.w.preview, r1, token=scoped).json()["operation_id"]
        assert await run(b, op, cell.ports) == "healthy"
        assert public_env_of(cell.step_env(0, "plan")) == PUBLIC_ENV["preview"]

        assert_problem(promote(b, {"preview_release_id": r1}, scoped), ErrorCode.FORBIDDEN)
        assert_problem(start_build(b, b.w.prod, cell.bundle, scoped), ErrorCode.FORBIDDEN)
        assert prod_builds(b) == []
        assert len(cell.emulator.builds) == 1

        out = await cell.finish(b, promote(b).json()["build_id"])
        for kind in ("deploy", "rollback"):
            r = start_deploy(b, b.w.prod, out["release_id"], kind, token=scoped)
            assert_problem(r, ErrorCode.FORBIDDEN)
        prod_never_started(b)


# ── stateful apps and secrets ────────────────────────────────────────────────


async def prod_release_of(b: Bench) -> str:
    build = promote(b).json()["build_id"]
    assert await run_build(b.ports, org_id=b.w.org, build_id=build) == "succeeded"
    return str(get(b, f"/v1/builds/{build}").json()["release_id"])


async def test_prod_takes_its_own_database_and_place(
    b: Bench, cell: Cell, dsns: Dsns, instance: CloudSqlLike
) -> None:
    database_ready(dsns, b.w.org)
    r1 = await build_release(b, b.w.preview, manifest_of(**STATEFUL))
    op = start_deploy(b, b.w.preview, r1).json()["operation_id"]
    assert await run_through_the_cell(b, cell, op) == "healthy"
    prod_service = service_name(b.w.prod)
    assert (await cell.agent.usage(prod_service)).present is False

    prod_release = await prod_release_of(b)
    for _ in range(2):
        op = start_deploy(b, b.w.prod, prod_release).json()["operation_id"]
        assert await run_through_the_cell(b, cell, op) == "healthy"
        used = await cell.agent.usage(prod_service)
        assert (used.present, used.environments, used.ceiling) == (True, 2, 10)

    prod_db = database_name(prod_service)
    prod_url = urlsplit(latest(cell, b.w.prod, DATABASE_URL))
    preview_url = urlsplit(latest(cell, b.w.preview, DATABASE_URL))
    assert prod_url.path == f"/{prod_db}" != preview_url.path
    for url, reaches in ((prod_url, True), (preview_url, False)):
        assert url.username is not None and url.password is not None
        dsn = instance.dsn(url.username, unquote(url.password), prod_db)
        if reaches:
            psycopg.connect(dsn).close()
            continue
        with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
            psycopg.connect(dsn)
    envs = rows_of(b.dsn, b.w.org, "select environment_id from ssc.app_database")
    assert {r["environment_id"] for r in envs} == {b.w.preview, b.w.prod}


async def test_prod_runs_its_own_secrets_never_previews(b: Bench, dsns: Dsns) -> None:
    database_ready(dsns, b.w.org)
    databases = FakeAppDatabases()
    ports = replace(b.ports, app_databases=databases)
    set_secret_ref(b, b.w.preview, "STRIPE_KEY", "4")
    r1 = await build_release(b, b.w.preview, manifest_of(**STATEFUL))
    op = start_deploy(b, b.w.preview, r1).json()["operation_id"]
    assert await run(b, op, ports) == "healthy"
    database_secrets = dict.fromkeys(app_database.SECRETS, "1")
    assert pinned(b, op) == {"STRIPE_KEY": "4", **database_secrets}

    assert_problem(promote(b), ErrorCode.PROD_SECRET_MISSING)
    assert prod_builds(b) == []
    set_secret_ref(b, b.w.prod, "STRIPE_KEY", "1")
    prod_release = await prod_release_of(b)
    op = start_deploy(b, b.w.prod, prod_release).json()["operation_id"]
    assert await run(b, op, ports) == "healthy"
    assert pinned(b, op) == {"STRIPE_KEY": "1", **database_secrets}
    assert databases.calls == [
        ("ensure", service_name(b.w.preview)),
        ("ensure", service_name(b.w.prod)),
        ("recovery_point", service_name(b.w.prod)),
    ]
    names = rows_of(
        b.dsn,
        b.w.org,
        "select name, secret_version from ssc.secret_ref where environment_id = %s order by name",
        b.w.prod,
    )
    assert names == sorted(
        [{"name": "STRIPE_KEY", "secret_version": "1"}]
        + [{"name": n, "secret_version": "1"} for n in app_database.SECRETS],
        key=lambda r: r["name"],
    )


async def test_promote_builds_preview_source_for_prod(b: Bench) -> None:
    r1 = await live_in_preview(b, build={"public_env": PUBLIC_ENV}, **NIGHTLY)
    r = promote(b, {"preview_release_id": r1})
    assert r.status_code == 202, r.text
    build = r.json()["build_id"]
    assert r.headers["Location"] == f"/v1/builds/{build}"
    assert r.json()["state"] == "queued"
    started = audit_of(b, build)[0]
    assert started["action"] == "build.started"
    assert started["after"]["via"] == "promote"
    assert started["after"]["source_release_id"] == r1
    assert started["after"]["environment_id"] == b.w.prod

    assert await run_build(b.ports, org_id=b.w.org, build_id=build) == "succeeded"
    out = get(b, f"/v1/builds/{build}").json()
    assert out["environment_id"] == b.w.prod
    prod_release = str(out["release_id"])
    source = release_row(b, r1)["source_digest"]
    released = get(b, f"/v1/apps/{b.w.app}/releases/{prod_release}").json()
    assert released["source_digest"] == source
    assert released["built_for_environment_id"] == b.w.prod
    assert released["image_digest"] == fake_image_digest(source, "prod", PUBLIC_ENV["prod"])
    assert released["image_digest"] != release_row(b, r1)["image_digest"]
    assert b.builds.requests[-1].public_env == PUBLIC_ENV["prod"]

    # The second step is the ordinary forward deploy, which syncs prod's timers.
    op, state = await deploy(b, b.w.prod, prod_release)
    assert state == "healthy"
    assert pointer(b, b.w.prod) == op
    assert operation(b, op)["release_id"] == prod_release
    assert (b.w.prod, ("nightly",), b.w.builder) in b.timers.calls


async def test_a_direct_prod_build_is_refused(b: Bench) -> None:
    await live_in_preview(b)
    (bundle,) = rows_of(b.dsn, b.w.org, "select id from ssc.bundle")
    r = start_build(b, b.w.prod, bundle["id"])
    assert_problem(r, ErrorCode.PROD_REQUIRES_PROMOTE)
    assert_problem(
        start_build(b, b.w.prod, bundle["id"], b.t.admin), ErrorCode.PROD_REQUIRES_PROMOTE
    )
    assert (
        rows_of(b.dsn, b.w.org, "select id from ssc.build where environment_id = %s", b.w.prod)
        == []
    )


async def test_a_prod_rollback_to_a_preview_build_is_refused(b: Bench) -> None:
    r1 = await live_in_preview(b)
    for kind in ("deploy", "rollback"):
        r = start_deploy(b, b.w.prod, r1, kind)
        assert_problem(r, ErrorCode.RELEASE_ENVIRONMENT_MISMATCH)


async def test_a_preview_scoped_token_cannot_promote(b: Bench, signing_key: SigningKey) -> None:
    r1 = await live_in_preview(b)
    scoped = mint(
        signing_key, org=b.w.org, sub=b.w.admin, jti=f"cred_{new_key()[:16]}", scope="preview"
    )
    assert_problem(promote(b, {"preview_release_id": r1}, scoped), ErrorCode.FORBIDDEN)
    assert (
        rows_of(b.dsn, b.w.org, "select id from ssc.build where environment_id = %s", b.w.prod)
        == []
    )


async def test_only_a_builder_on_prod_may_promote(b: Bench) -> None:
    await live_in_preview(b)
    assert_problem(promote(b, token=b.t.member), ErrorCode.FORBIDDEN)
    r = post(b, f"/v1/apps/{new_id('app')}/promote", {}, None)
    assert_problem(r, ErrorCode.NOT_FOUND)
    r = b.client.post(f"/v1/apps/{b.w.app}/promote", json={}, headers=auth(b.t.builder))
    assert_problem(r, ErrorCode.IDEMPOTENCY_KEY_REQUIRED)


async def test_nothing_live_in_preview(b: Bench) -> None:
    assert_problem(promote(b), ErrorCode.NOTHING_TO_PROMOTE)
    release = await build_release(b, b.w.preview)
    op = start_deploy(b, b.w.preview, release).json()["operation_id"]
    # Accepted but not yet healthy: still nothing live.
    assert_problem(promote(b), ErrorCode.NOTHING_TO_PROMOTE)
    assert await run(b, op) == "healthy"
    assert promote(b).status_code == 202


async def test_a_stale_preview_release_id(b: Bench) -> None:
    r1 = await live_in_preview(b)
    r2 = await live_in_preview(b)
    assert_problem(promote(b, {"preview_release_id": r1}), ErrorCode.PRECONDITION_STALE)
    assert promote(b, {"preview_release_id": r2}).status_code == 202


async def test_a_replay_returns_the_same_build(b: Bench) -> None:
    await live_in_preview(b)
    key = new_key()
    path = f"/v1/apps/{b.w.app}/promote"
    headers = auth(b.t.builder, **{IDEMPOTENCY_HEADER: key})
    first = b.client.post(path, json={}, headers=headers)
    again = b.client.post(path, json={}, headers=headers)
    assert (first.status_code, again.status_code) == (202, 202)
    assert again.json() == first.json()
    assert again.headers[REPLAYED_HEADER] == "true"
    builds = rows_of(b.dsn, b.w.org, "select id from ssc.build where environment_id = %s", b.w.prod)
    assert builds == [{"id": first.json()["build_id"]}]


async def test_a_stopped_app_cannot_promote(b: Bench) -> None:
    r1 = await live_in_preview(b)
    for status in ("disabled", "quarantined"):
        execute(b.dsn, b.w.org, "update ssc.app set status = %s where id = %s", status, b.w.app)
        assert_problem(promote(b, {"preview_release_id": r1}), ErrorCode.APP_NOT_ACTIVE)


async def test_prod_in_flight_refuses_promote(b: Bench) -> None:
    await live_in_preview(b)
    first = promote(b)
    assert first.status_code == 202
    # A second promote while the first build is queued.
    assert_problem(promote(b), ErrorCode.BUILD_IN_FLIGHT)
    build = first.json()["build_id"]
    assert await run_build(b.ports, org_id=b.w.org, build_id=build) == "succeeded"
    prod_release = get(b, f"/v1/builds/{build}").json()["release_id"]
    op = start_deploy(b, b.w.prod, prod_release).json()["operation_id"]
    assert_problem(promote(b), ErrorCode.DEPLOYMENT_IN_FLIGHT)
    assert await run(b, op) == "healthy"
    assert promote(b).status_code == 202


async def test_a_seeded_prod_build_also_counts_as_in_flight(b: Bench) -> None:
    await live_in_preview(b)
    (bundle,) = rows_of(b.dsn, b.w.org, "select id from ssc.bundle")
    seed_prod_build(b, bundle["id"])
    assert_problem(promote(b), ErrorCode.BUILD_IN_FLIGHT)


async def test_a_failing_prod_gate_starts_no_prod_instance(b: Bench) -> None:
    await live_in_preview(b)
    build = promote(b).json()["build_id"]
    assert await run_build(b.ports, org_id=b.w.org, build_id=build) == "succeeded"
    prod_release = get(b, f"/v1/builds/{build}").json()["release_id"]
    spy = SpyGate("waiting")
    set_prod_gate(b, spy)
    op = start_deploy(b, b.w.prod, prod_release).json()["operation_id"]
    assert await run(b, op, replace(b.ports, prod_gate=spy)) == "failed"
    assert operation(b, op)["failure_code"] == APPROVAL_REQUIRED
    assert spy.calls == [b.w.prod, b.w.prod]
    assert all(service != service_name(b.w.prod) for _, service in b.runtime.calls)
    assert service_name(b.w.prod) not in b.runtime.services
    assert pointer(b, b.w.prod) is None
    assert audit_of(b, op)[-1]["action"] == "deploy.failed"
