"""SSC-014 (B6): the bundle collector against the filesystem store and postgres:18.

  * stale pending and orphan objects go, the rows stay -> test_stale_pending_and_orphan_objects_go
  * a release's digest, a stored bundle, young objects and other keys stay
                                                        -> the same test
  * a key or row another transaction holds is skipped -> test_a_key_or_row_in_use_is_left_for_later
  * a stored bundle no release or build uses goes after 7 days, its row back to pending, logged
        -> test_a_stored_bundle_nothing_uses_goes_after_a_week
  * a build starting on the bundle holds the row: left for later
        -> test_a_bundle_a_build_is_starting_on_is_left_for_later
  * each org collected in its own cell's store       -> test_each_org_is_collected_in_its_cell_store
  * every org in org_index, one failure logged         -> test_the_job_collects_every_org
  * the hourly worker task                             -> test_the_worker_registers_the_collector
Recording a digest and completing a bundle wait for the collector: test_bundles.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from ssc_testkit import ISSUER, Dsns

from ssc_conformance.contracts.blobstore import ManualClock
from ssc_contracts.ids import new_id
from ssc_control.db import NewOrg, bind_org_sync, create_org, make_engine
from ssc_control.deploy import tasks
from ssc_control.deploy.bundle_gc import (
    GRACE,
    LOCK_CLASS,
    RETENTION,
    BundleGcError,
    Collected,
    collect_all,
    collect_org,
)
from ssc_control.deploy.bundles import bundle_key
from ssc_control.storage import cell_stores
from ssc_control.worker import build_app
from ssc_shared.blobstore import BlobCorruptError, BlobInfo, BlobStore
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
OLD = NOW - GRACE - timedelta(hours=1)
YOUNG = NOW - GRACE + timedelta(hours=1)
SIGNING_KEY = ("gc-" + "signing-" + "key-").encode() * 3


@dataclass(frozen=True)
class Org:
    id: str
    admin: str
    app: str


class BrokenFor(FsBlobStore):
    """Listing one prefix fails, as a damaged object would make it."""

    broken: str | None = None

    async def list(self, prefix: str = "") -> AsyncIterator[BlobInfo]:
        if prefix == self.broken:
            raise BlobCorruptError(prefix)
        async for info in super().list(prefix):
            yield info


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def store(tmp_path: Path, clock: ManualClock) -> BrokenFor:
    signer = UrlSigner({"k1": SIGNING_KEY}, active="k1", clock=clock)
    return BrokenFor(tmp_path / "blobs", signer=signer, base_url="http://blobs.test", clock=clock)


@pytest.fixture
async def engine(dsns: Dsns) -> AsyncIterator[AsyncEngine]:
    e = make_engine(dsns.app)
    yield e
    await e.dispose()


async def make_org(dsn: str) -> Org:
    engine = make_engine(dsn)
    try:
        spec = NewOrg("Collect", "Ada Admin", "ada@example.com", ISSUER, new_id("usr"))
        created = await create_org(engine, spec)
    finally:
        await engine.dispose()
    app = new_id("app")
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, created.org_id)
        conn.execute(
            "insert into ssc.app (id, org_id, slug, owner_user_id) values (%s, %s, 'ledger', %s)",
            (app, created.org_id, created.admin_user_id),
        )
    return Org(created.org_id, created.admin_user_id, app)


def digest_of(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


async def put_at(store: FsBlobStore, clock: ManualClock, key: str, when: datetime) -> None:
    clock.set(when)
    await store.put(key, b"bytes")
    clock.set(NOW)


def add_bundle(dsn: str, o: Org, digest: str, created: datetime, *, stored: bool = False) -> str:
    bid = new_id("bdl")
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, o.id)
        conn.execute(
            "insert into ssc.bundle (id, org_id, app_id, digest, size_bytes, actor_kind, "
            "actor_id, created_at) values (%s, %s, %s, %s, 5, 'user', %s, %s)",
            (bid, o.id, o.app, digest, o.admin, created),
        )
        if stored:
            conn.execute(
                "update ssc.bundle set state = 'stored', manifest = '{}'::jsonb, "
                "manifest_digest = %s, file_count = 0, stored_at = now() where id = %s",
                (digest, bid),
            )
    return bid


def add_release(dsn: str, o: Org, digest: str, number: int) -> None:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, o.id)
        conn.execute(
            "insert into ssc.release (id, org_id, app_id, number, image_digest, manifest_digest, "
            "source_digest, actor_kind, actor_id) values (%s, %s, %s, %s, %s, %s, %s, 'user', %s)",
            (new_id("rel"), o.id, o.app, number, digest, digest, digest, o.admin),
        )


def state_of(dsn: str, o: Org, bundle_id: str) -> str:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, o.id)
        row = conn.execute("select state from ssc.bundle where id = %s", (bundle_id,)).fetchone()
    assert row is not None
    return str(row[0])


async def keys_of(store: FsBlobStore) -> set[str]:
    return {info.key async for info in store.list()}


async def test_stale_pending_and_orphan_objects_go(
    dsns: Dsns, engine: AsyncEngine, store: FsBlobStore, clock: ManualClock
) -> None:
    o, other = await make_org(dsns.app), await make_org(dsns.app)

    def key(label: str) -> str:
        return bundle_key(o.id, o.app, digest_of(label))

    # label: (object written, bundle row (created, stored) or None, a release carries it)
    cases: dict[str, tuple[datetime, tuple[datetime, bool] | None, bool]] = {
        "stale-pending": (OLD, (OLD, False), False),
        "orphan": (OLD, None, False),
        "young-orphan": (YOUNG, None, False),
        "young-object": (YOUNG, (OLD, False), False),
        "young-row": (OLD, (YOUNG, False), False),
        "stored": (OLD, (OLD, True), False),
        "released-pending": (OLD, (OLD, False), True),
        "released-orphan": (OLD, None, True),
    }
    rows: dict[str, str] = {}
    for number, (label, (written, row, released)) in enumerate(cases.items(), start=1):
        await put_at(store, clock, key(label), written)
        if row is not None:
            rows[label] = add_bundle(dsns.app, o, digest_of(label), row[0], stored=row[1])
        if released:
            add_release(dsns.app, o, digest_of(label), number)
    untouched = {
        key("zip").removesuffix(".tar.gz") + ".zip",
        f"bundles/{o.id}/notes",
        f"snapshots/{o.id}/v1.json",
        f"audit-anchors/{o.id}/2026-09-27.json",
        bundle_key(other.id, other.app, digest_of("theirs")),
    }
    for k in untouched:
        await put_at(store, clock, k, OLD)

    assert await collect_org(engine, store, o.id, now=NOW) == Collected(deleted=2, kept=8)
    gone = {key("stale-pending"), key("orphan")}
    assert await keys_of(store) == {key(label) for label in cases} - gone | untouched
    # The row stays pending: asking for the digest again answers a fresh upload URL.
    assert state_of(dsns.app, o, rows["stale-pending"]) == "pending"
    assert await collect_org(engine, store, o.id, now=NOW) == Collected(kept=8)


async def test_a_key_or_row_in_use_is_left_for_later(
    dsns: Dsns, engine: AsyncEngine, store: FsBlobStore, clock: ManualClock
) -> None:
    o = await make_org(dsns.app)
    orphan = bundle_key(o.id, o.app, digest_of("orphan"))
    pending = bundle_key(o.id, o.app, digest_of("pending"))
    await put_at(store, clock, orphan, OLD)
    await put_at(store, clock, pending, OLD)
    bundle = add_bundle(dsns.app, o, digest_of("pending"), OLD)
    with psycopg.connect(dsns.app) as creating, psycopg.connect(dsns.app) as completing:
        # A digest being recorded holds its key; a bundle being completed holds its row.
        bind_org_sync(creating, o.id)
        creating.execute("select pg_advisory_xact_lock(%s, hashtext(%s))", (LOCK_CLASS, orphan))
        bind_org_sync(completing, o.id)
        completing.execute("select 1 from ssc.bundle where id = %s for update", (bundle,))
        assert await collect_org(engine, store, o.id, now=NOW) == Collected(busy=2)
        assert await keys_of(store) == {orphan, pending}
        creating.commit()
        completing.commit()
    assert await collect_org(engine, store, o.id, now=NOW) == Collected(deleted=2)
    assert await keys_of(store) == set()


async def test_the_job_collects_every_org(
    dsns: Dsns, engine: AsyncEngine, store: BrokenFor, clock: ManualClock
) -> None:
    orgs = [await make_org(dsns.app) for _ in range(3)]
    keys = [bundle_key(o.id, o.app, digest_of("orphan")) for o in orgs]
    for k in keys:
        await put_at(store, clock, k, OLD)
    store.broken = f"bundles/{orgs[0].id}/"
    # Every other org is still collected; then the job fails, so the failure is seen.
    with pytest.raises(BundleGcError, match="1 orgs"):
        await collect_all(engine, blob_store=store, cell_stores=None, now=NOW)
    assert await keys_of(store) == {keys[0]}
    store.broken = None
    assert await collect_all(engine, blob_store=store, cell_stores=None, now=NOW) == Collected(
        deleted=1
    )
    assert await keys_of(store) == set()


def stored_at(dsn: str, o: Org, bundle_id: str, when: datetime) -> None:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, o.id)
        conn.execute("update ssc.bundle set stored_at = %s where id = %s", (when, bundle_id))


def add_build(dsn: str, o: Org, bundle_id: str) -> None:
    """A queued build of the bundle, in an environment of its own."""
    env = new_id("env")
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, o.id)
        conn.execute(
            "insert into ssc.environment (id, org_id, app_id, name) values (%s, %s, %s, 'prod')",
            (env, o.id, o.app),
        )
        conn.execute(
            "insert into ssc.build (id, org_id, app_id, environment_id, bundle_id, actor_kind, "
            "actor_id) values (%s, %s, %s, %s, %s, 'user', %s)",
            (new_id("bld"), o.id, o.app, env, bundle_id, o.admin),
        )


async def test_a_stored_bundle_nothing_uses_goes_after_a_week(
    dsns: Dsns,
    engine: AsyncEngine,
    store: FsBlobStore,
    clock: ManualClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    week = NOW - RETENTION - timedelta(hours=1)
    days = NOW - RETENTION + timedelta(hours=1)
    o = await make_org(dsns.app)

    def key(label: str) -> str:
        return bundle_key(o.id, o.app, digest_of(label))

    rows: dict[str, str] = {}
    ages = {"expired": week, "released": week, "building": week, "recent": days}
    for label, stored in ages.items():
        await put_at(store, clock, key(label), week - timedelta(hours=1))
        rows[label] = add_bundle(dsns.app, o, digest_of(label), week, stored=True)
        stored_at(dsns.app, o, rows[label], stored)
    add_release(dsns.app, o, digest_of("released"), 1)
    add_build(dsns.app, o, rows["building"])
    caplog.set_level(logging.INFO, logger="ssc_control.deploy.bundle_gc")

    assert await collect_org(engine, store, o.id, now=NOW) == Collected(
        deleted=1, kept=3, expired=1
    )
    assert await keys_of(store) == {key("released"), key("building"), key("recent")}
    # The row is pending again, so sending the same source uploads it again.
    assert state_of(dsns.app, o, rows["expired"]) == "pending"
    for label in ("released", "building", "recent"):
        assert state_of(dsns.app, o, rows[label]) == "stored"
    (logged,) = [r for r in caplog.records if r.getMessage() == "bundle object deleted"]
    assert (logged.org_id, logged.app_id, logged.digest) == (o.id, o.app, digest_of("expired"))  # type: ignore[attr-defined]
    assert logged.age_hours == RETENTION.total_seconds() / 3600 + 2  # type: ignore[attr-defined]
    assert await collect_org(engine, store, o.id, now=NOW) == Collected(kept=3)


async def test_a_bundle_a_build_is_starting_on_is_left_for_later(
    dsns: Dsns, engine: AsyncEngine, store: FsBlobStore, clock: ManualClock
) -> None:
    o = await make_org(dsns.app)
    key = bundle_key(o.id, o.app, digest_of("starting"))
    week = NOW - RETENTION - timedelta(hours=1)
    await put_at(store, clock, key, week)
    bundle = add_bundle(dsns.app, o, digest_of("starting"), week, stored=True)
    stored_at(dsns.app, o, bundle, week)
    with psycopg.connect(dsns.app) as starting:
        # A build being queued reads the bundle FOR SHARE (api.routes.v1.deployments).
        bind_org_sync(starting, o.id)
        starting.execute("select state from ssc.bundle where id = %s for share", (bundle,))
        assert await collect_org(engine, store, o.id, now=NOW) == Collected(busy=1)
        assert state_of(dsns.app, o, bundle) == "stored"
        starting.commit()
    assert await collect_org(engine, store, o.id, now=NOW) == Collected(deleted=1, expired=1)


def fs_cells(root: Path, clock: ManualClock) -> Callable[[str], BlobStore]:
    def bucket(name: str) -> BlobStore:
        signer = UrlSigner({"k1": SIGNING_KEY}, active="k1", clock=clock)
        return FsBlobStore(root / name, signer=signer, base_url="http://blobs.test", clock=clock)

    return cell_stores("cells-{cell}", bucket=bucket)


def label_of(dsn: str, o: Org) -> str:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, o.id)
        row = conn.execute("select cell_label from ssc.org where id = %s", (o.id,)).fetchone()
    assert row is not None
    return str(row[0])


async def test_each_org_is_collected_in_its_cell_store(
    dsns: Dsns, engine: AsyncEngine, store: FsBlobStore, clock: ManualClock, tmp_path: Path
) -> None:
    a, b = await make_org(dsns.app), await make_org(dsns.app)
    cells = fs_cells(tmp_path / "cells", clock)
    for o in (a, b):
        cell = cells(label_of(dsns.app, o))
        await put_at(cell, clock, bundle_key(o.id, o.app, digest_of("orphan")), OLD)  # type: ignore[arg-type]
        await put_at(cell, clock, bundle_key(o.id, o.app, digest_of("kept")), YOUNG)  # type: ignore[arg-type]
    # A copy left in the control store is not this job's: bundle_move takes it.
    await put_at(store, clock, bundle_key(a.id, a.app, digest_of("orphan")), OLD)
    await collect_all(engine, blob_store=store, cell_stores=cells, now=NOW)
    for o in (a, b):
        cell = cells(label_of(dsns.app, o))
        assert await keys_of(cell) == {bundle_key(o.id, o.app, digest_of("kept"))}  # type: ignore[arg-type]
    assert await keys_of(store) == {bundle_key(a.id, a.app, digest_of("orphan"))}


def test_the_worker_registers_the_collector() -> None:
    app = build_app("postgresql://ssc_app@localhost/ssc")
    assert tasks.COLLECT_BUNDLES in app.tasks
    ((periodic,),) = [
        [p for key, p in app.periodic_registry.periodic_tasks.items() if key[0] == name]
        for name in [tasks.COLLECT_BUNDLES]
    ]
    first = periodic.croniter.get_next(float, start_time=1_790_000_000.0)
    assert periodic.croniter.get_next(float, start_time=first) - first == 3600
