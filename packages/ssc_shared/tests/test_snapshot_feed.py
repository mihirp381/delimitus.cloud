"""The cell's snapshot feed (SSC-013): newest version applied, last good view kept on any fault."""

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any

import pytest

from ssc_contracts.snapshot import FORMAT_V1
from ssc_shared.access import ViewHolder
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.canonical import canonical_bytes
from ssc_shared.clock import SystemClock
from ssc_shared.snapshot_feed import (
    POLL_SECONDS,
    SnapshotFeed,
    latest_key,
    object_key,
    parse_pointer,
)

ORG = "org_" + "a" * 20
OTHER = "org_" + "b" * 20
SIGNING_KEY = ("feed-" + "test-" + "signing-" + "key-").encode() * 2


def doc(version: int, org_id: str = ORG) -> dict[str, Any]:
    return {
        "format": FORMAT_V1,
        "org_id": org_id,
        "version": version,
        "compiled_at": "2026-09-30T12:00:00Z",
        "environments": {},
        "hosts": {},
        "grants": {},
        "groups_by_user": {},
        "users": {},
        "ceiling": None,
    }


@pytest.fixture
def store(tmp_path: Path) -> FsBlobStore:
    signer = UrlSigner({"k1": SIGNING_KEY}, active="k1", clock=SystemClock())
    return FsBlobStore(tmp_path, signer=signer, base_url="http://blobs.test")


async def put_object(store: FsBlobStore, body: dict[str, Any], org_id: str = ORG) -> str:
    raw = canonical_bytes(body)
    sha = hashlib.sha256(raw).hexdigest()
    key = object_key(org_id, body["version"], sha)
    await store.put(key, raw)
    return key


async def point(store: FsBlobStore, version: int, key: str, sha: str, org_id: str = ORG) -> None:
    pointer = {"version": version, "key": key, "digest": f"sha256:{sha}"}
    await store.put(latest_key(org_id), canonical_bytes(pointer))


async def publish(store: FsBlobStore, version: int, org_id: str = ORG) -> None:
    key = await put_object(store, doc(version, org_id), org_id)
    sha = hashlib.sha256(canonical_bytes(doc(version, org_id))).hexdigest()
    await point(store, version, key, sha, org_id)


def feed_for(store: FsBlobStore, interval: float = POLL_SECONDS) -> SnapshotFeed:
    return SnapshotFeed(store, ViewHolder(ORG), interval=interval)


async def test_nothing_published_is_no_view_and_an_error(store: FsBlobStore) -> None:
    feed = feed_for(store)
    assert await feed.poll_once() is False
    assert feed.version is None
    assert feed.last_error == "no snapshot published"


async def test_the_newest_version_is_applied_once(store: FsBlobStore) -> None:
    feed = feed_for(store)
    await publish(store, 1)
    assert await feed.poll_once() is True
    assert await feed.poll_once() is False
    await publish(store, 2)
    assert await feed.poll_once() is True
    assert (feed.version, feed.last_error, feed.failures) == (2, None, 0)


async def test_fresh_means_a_poll_confirmed_the_view_recently(store: FsBlobStore) -> None:
    now = [100.0]
    feed = SnapshotFeed(store, ViewHolder(ORG), monotonic=lambda: now[0])
    assert not feed.fresh(300)
    await publish(store, 1)
    await feed.poll_once()
    now[0] += 299
    assert feed.fresh(300)
    await feed.poll_once()  # same version: still a confirmation
    now[0] += 299
    assert feed.fresh(300)
    await store.put(latest_key(ORG), b"torn")
    await feed.poll_once()
    now[0] += 2
    assert not feed.fresh(300) and feed.version == 1


async def test_a_slow_poll_confirms_the_view_as_of_when_it_asked(
    tmp_path: Path, store: FsBlobStore
) -> None:
    now = [100.0]

    class SlowStore(FsBlobStore):
        def get(self, key: str) -> Any:
            now[0] += 5
            return super().get(key)

    signer = UrlSigner({"k1": SIGNING_KEY}, active="k1", clock=SystemClock())
    slow = SlowStore(tmp_path, signer=signer, base_url="http://blobs.test")
    feed = SnapshotFeed(slow, ViewHolder(ORG), monotonic=lambda: now[0])
    await publish(store, 1)
    await feed.poll_once()
    assert feed.last_ok_at == 100.0 and now[0] == 110.0


async def test_an_older_pointer_changes_nothing(store: FsBlobStore) -> None:
    feed = feed_for(store)
    await publish(store, 2)
    await feed.poll_once()
    await publish(store, 1)
    assert await feed.poll_once() is False
    assert (feed.version, feed.last_error) == (2, None)


@pytest.mark.parametrize(
    "fault",
    ["digest", "missing_object", "foreign_key", "not_json", "invalid_document", "foreign_org"],
)
async def test_a_fault_keeps_the_last_good_view(store: FsBlobStore, fault: str) -> None:
    feed = feed_for(store)
    await publish(store, 1)
    await feed.poll_once()
    body = doc(2)
    sha = hashlib.sha256(canonical_bytes(body)).hexdigest()
    match fault:
        case "digest":
            key = await put_object(store, body)
            other = sha[:12] + ("1" if sha[12] == "0" else "0") + sha[13:]
            await point(store, 2, key, other)
        case "missing_object":
            await point(store, 2, object_key(ORG, 2, sha), sha)
        case "foreign_key":
            await point(store, 2, object_key(OTHER, 2, sha), sha)
        case "not_json":
            await store.put(latest_key(ORG), b"{not json")
        case "invalid_document":
            broken = {**body, "format": "ssc-snapshot/v9"}
            key = await put_object(store, broken)
            await point(store, 2, key, hashlib.sha256(canonical_bytes(broken)).hexdigest())
        case "foreign_org":
            foreign = doc(2, OTHER)
            raw = canonical_bytes(foreign)
            foreign_sha = hashlib.sha256(raw).hexdigest()
            key = object_key(ORG, 2, foreign_sha)
            await store.put(key, raw)
            await point(store, 2, key, foreign_sha)
    assert await feed.poll_once() is False
    assert feed.version == 1
    assert feed.failures == 1
    assert feed.last_error is not None
    await publish(store, 3)
    assert await feed.poll_once() is True
    assert (feed.version, feed.failures, feed.last_error) == (3, 0, None)


def test_the_pointer_is_strict() -> None:
    sha = "a" * 64
    good = {"version": 4, "key": object_key(ORG, 4, sha), "digest": f"sha256:{sha}"}
    assert parse_pointer(canonical_bytes(good), ORG).version == 4
    for bad in (
        {**good, "extra": 1},
        {**good, "version": 0},
        {**good, "version": True},
        {**good, "digest": sha},
        {**good, "key": object_key(ORG, 5, sha)},
        {**good, "key": "snapshots/../x.json"},
    ):
        with pytest.raises(ValueError):
            parse_pointer(canonical_bytes(bad), ORG)


async def test_a_published_version_reaches_the_cell_within_five_seconds(
    store: FsBlobStore,
) -> None:
    """The done-when's round trip, at the production poll interval."""
    feed = feed_for(store)
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    try:
        await asyncio.sleep(0.3)
        started = time.monotonic()
        await publish(store, 7)
        while feed.version != 7:
            assert time.monotonic() - started < 5.0
            await asyncio.sleep(0.05)
        assert time.monotonic() - started <= POLL_SECONDS + 1.0
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=3)
