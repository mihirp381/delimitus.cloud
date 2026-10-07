"""Decision 015 (amended): an org's source bundles rest in its own cell's bucket, never in the
control plane's store. Uses test_deploy's helpers, the fake build and runtime drivers, and one
filesystem store per cell label, each signing with its own key, against postgres:18.

  * two orgs through upload, complete, a preview build, promote's prod rebuild and the
    collector: each org's bundles only in its own cell's store, nothing in the control store
        -> test_each_orgs_source_stays_in_its_own_cell
  * an org whose cell is not served gets no upload URL (CELL_UNAVAILABLE)
        -> test_an_org_whose_cell_is_not_served_gets_no_upload_url
  * a stored bundle whose object is gone is asked for again; one whose object is there is not
        -> test_a_stored_bundle_whose_object_is_gone_is_asked_for_again
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
import test_deploy
from fastapi.testclient import TestClient
from httpx import Response
from ssc_testkit import ISSUER, Dsns, SigningKey, assert_problem, mint, new_key
from test_deploy import (
    FIXTURES,
    MASTER,
    Bench,
    Hold,
    SpyTimers,
    Tokens,
    World,
    deploy,
    get,
    post,
    rows_of,
    start_build,
)

from ssc_bundle.client import prepare
from ssc_contracts.errors import ErrorCode
from ssc_control.api import Settings, create_app
from ssc_control.db import make_engine
from ssc_control.deploy.build_driver import FakeBuildDriver
from ssc_control.deploy.builds import run_build
from ssc_control.deploy.bundle_gc import RETENTION, Collected, collect_all
from ssc_control.deploy.bundles import bundle_key
from ssc_control.deploy.gates import approvals_prod_gate
from ssc_control.metrics import metrics_port
from ssc_control.runtime.cells import OrgCell, StaticCells
from ssc_control.runtime.fake import FakeRuntimeDriver
from ssc_control.runtime.specs import BundleReleaseSpecs
from ssc_control.storage import cell_stores
from ssc_control.worker import Ports
from ssc_shared.blobstore import BlobStore
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.clock import SystemClock

CONTROL_KEYS = {"k1": b"c" * 32}


@dataclass
class Cells:
    """Two served orgs, one org whose cell is not served, and every store they could reach."""

    client: TestClient
    orgs: tuple[Bench, Bench]
    lost: Bench
    control: FsBlobStore
    buckets: dict[str, FsBlobStore]
    """Each cell's store by bucket name, as the API and the worker built it."""

    def bucket_of(self, b: Bench) -> FsBlobStore:
        return self.buckets[f"cells-{label_of(b)}"]


def label_in(dsn: str, w: World) -> str:
    (row,) = rows_of(dsn, w.org, "select cell_label from ssc.org where id = %s", w.org)
    return str(row["cell_label"])


def label_of(b: Bench) -> str:
    return label_in(b.dsn, b.w)


def tokens_of(w: World, signing_key: SigningKey) -> Tokens:
    def token(sub: str) -> str:
        return mint(signing_key, org=w.org, sub=sub, jti=f"cred_{new_key()[:16]}")

    return Tokens(admin=token(w.admin), member=token(w.member), builder=token(w.builder))


@pytest.fixture
async def c(dsns: Dsns, signing_key: SigningKey, tmp_path: Path) -> AsyncIterator[Cells]:
    worlds = [await test_deploy.make_world(dsns.app) for _ in range(3)]
    buckets: dict[str, FsBlobStore] = {}

    def bucket(name: str) -> BlobStore:
        key = hashlib.sha256(name.encode()).digest()  # each cell signs with its own key
        buckets[name] = FsBlobStore(
            tmp_path / name,
            signer=UrlSigner({"k1": key}, active="k1", clock=SystemClock()),
            base_url=f"http://{name}.test/blobs",
        )
        return buckets[name]

    stores = cell_stores("cells-{cell}", bucket=bucket)
    control = FsBlobStore(
        tmp_path / "control",
        signer=UrlSigner(CONTROL_KEYS, active="k1", clock=SystemClock()),
        base_url="http://testserver/blobs",
    )
    engine = make_engine(dsns.app)
    hold, timers = Hold(), SpyTimers()
    runtime = FakeRuntimeDriver(sleep=hold)
    served = worlds[:2]
    builds = {w.org: FakeBuildDriver() for w in served}
    cells = StaticCells(
        orgs={
            w.org: OrgCell(label=label_in(dsns.app, w), runtime=runtime, build=builds[w.org])
            for w in served
        }
    )
    ports = Ports(
        engine=engine,
        cells=cells,
        blob_store=control,
        cell_stores=stores,
        release_specs=BundleReleaseSpecs(),
        timers=timers,
        prod_gate=approvals_prod_gate(),
        metrics=metrics_port(MASTER),
    )
    settings = Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
        metrics_key=MASTER,
        environment="test",
    )
    app = create_app(settings, None, control, cells=cells, cell_stores=stores)
    with TestClient(app) as client:
        benches = [
            Bench(
                client,
                w,
                tokens_of(w, signing_key),
                dsns.app,
                runtime,
                hold,
                builds.get(w.org, FakeBuildDriver()),
                timers,
                ports,
            )
            for w in worlds
        ]
        yield Cells(client, (benches[0], benches[1]), benches[2], control, buckets)
    await engine.dispose()


# ── helpers ──────────────────────────────────────────────────────────────────


def source_of(root: Path, name: str) -> bytes:
    """cs-fastapi-hello with a line of its own, packed: each org ships different bytes."""
    source = root / name
    shutil.copytree(FIXTURES / "cs-fastapi-hello", source)
    with (source / "main.py").open("a") as f:
        f.write(f"# {name}\n")
    return prepare(source, root / f"{name}.tar.gz").bundle.path.read_bytes()


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def create(b: Bench, data: bytes) -> Response:
    body = {"digest": sha(data), "size_bytes": len(data)}
    return post(b, f"/v1/apps/{b.w.app}/bundles", body, None)


async def upload(c: Cells, target: dict[str, Any], data: bytes) -> FsBlobStore:
    """PUT ``data`` to the store the URL names, as a bucket would take it; that store."""
    parts = urlsplit(target["url"])
    host = parts.hostname or ""
    store = c.buckets[host.removesuffix(".test")] if host != "testserver" else c.control
    key = unquote(parts.path.removeprefix("/blobs/"))
    await store.accept_put(key, dict(parse_qsl(parts.query)), data)
    return store


async def stored(c: Cells, b: Bench, data: bytes) -> str:
    """Create, upload and complete ``data``; the bundle id."""
    r = create(b, data)
    assert r.status_code == 201, r.text
    assert await upload(c, r.json()["upload"], data) is c.bucket_of(b)
    done = post(b, f"/v1/apps/{b.w.app}/bundles/{r.json()['bundle_id']}/complete", {}, None)
    assert done.json()["state"] == "stored", done.text
    return str(r.json()["bundle_id"])


async def keys_in(store: FsBlobStore) -> set[str]:
    return {i.key async for i in store.list("")}


def release_of(b: Bench, build: str) -> dict[str, Any]:
    out = get(b, f"/v1/builds/{build}").json()
    assert out["state"] == "succeeded", out
    (row,) = rows_of(b.dsn, b.w.org, "select * from ssc.release where id = %s", out["release_id"])
    return row


# ── tests ────────────────────────────────────────────────────────────────────


async def test_each_orgs_source_stays_in_its_own_cell(c: Cells, tmp_path: Path) -> None:
    released: dict[str, str] = {}
    unused: dict[str, str] = {}
    for b in c.orgs:
        data = source_of(tmp_path, b.w.org)
        bundle = await stored(c, b, data)
        released[b.w.org] = bundle_key(b.w.org, b.w.app, sha(data))
        # Preview builds from the cell's copy: the source check reads it there.
        build = start_build(b, b.w.preview, bundle).json()["build_id"]
        assert await run_build(b.ports, org_id=b.w.org, build_id=build) == "succeeded"
        preview = release_of(b, build)
        assert (await deploy(b, b.w.preview, preview["id"]))[1] == "healthy"
        # Promote rebuilds the same source for prod, read from the same place.
        r = post(b, f"/v1/apps/{b.w.app}/promote", {"preview_release_id": preview["id"]}, None)
        assert r.status_code == 202, r.text
        prod_build = r.json()["build_id"]
        assert await run_build(b.ports, org_id=b.w.org, build_id=prod_build) == "succeeded"
        assert release_of(b, prod_build)["source_digest"] == preview["source_digest"] == sha(data)
        # A second bundle no release uses, for the collector.
        extra = source_of(tmp_path, b.w.org + "-unused")
        await stored(c, b, extra)
        unused[b.w.org] = bundle_key(b.w.org, b.w.app, sha(extra))

    for b in c.orgs:
        assert await keys_in(c.bucket_of(b)) == {released[b.w.org], unused[b.w.org]}
    assert await keys_in(c.control) == set()

    # A week on, the collector expires the unused bundle in each org's cell and keeps the
    # released one; it never looks in the control store for bundles.
    ports = c.orgs[0].ports
    later = datetime.now(UTC) + RETENTION + timedelta(days=1)
    collected = await collect_all(
        ports.engine, blob_store=ports.blob_store, cell_stores=ports.cell_stores, now=later
    )
    assert collected == Collected(deleted=2, kept=2, expired=2)
    for b in c.orgs:
        assert await keys_in(c.bucket_of(b)) == {released[b.w.org]}
        states = rows_of(b.dsn, b.w.org, "select digest, state from ssc.bundle order by digest")
        assert {s["state"] for s in states} == {"stored", "pending"}
    assert await keys_in(c.control) == set()
    # The collector lists every org's cell; the unserved org's holds nothing.
    assert await keys_in(c.bucket_of(c.lost)) == set()


async def test_an_org_whose_cell_is_not_served_gets_no_upload_url(c: Cells, tmp_path: Path) -> None:
    b = c.lost
    assert_problem(create(b, source_of(tmp_path, "lost")), ErrorCode.CELL_UNAVAILABLE)
    assert rows_of(b.dsn, b.w.org, "select id from ssc.bundle") == []
    assert await keys_in(c.control) == set()
    assert f"cells-{label_of(b)}" not in c.buckets


async def test_a_stored_bundle_whose_object_is_gone_is_asked_for_again(
    c: Cells, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    b = c.orgs[0]
    data = source_of(tmp_path, "gone")
    bundle = await stored(c, b, data)
    there = create(b, data)
    assert (there.status_code, there.json()["state"], there.json()["upload"]) == (
        200,
        "stored",
        None,
    )

    # The collector deleted the object and crashed before the row went back to pending.
    assert await c.bucket_of(b).delete(bundle_key(b.w.org, b.w.app, sha(data))) is True
    again = create(b, data)
    assert again.status_code == 200, again.text
    out = again.json()
    assert (out["bundle_id"], out["state"], out["stored_at"]) == (bundle, "pending", None)
    assert (out["manifest_digest"], out["file_count"]) == (None, None)
    logged = [r.getMessage() for r in caplog.records]
    assert "stored bundle had no object; asked for again" in logged
    assert await upload(c, out["upload"], data) is c.bucket_of(b)
    done = post(b, f"/v1/apps/{b.w.app}/bundles/{bundle}/complete", {}, None)
    assert done.json()["state"] == "stored", done.text
