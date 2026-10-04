"""The proxy follows the snapshot: a changed allowlist or credential rewrites the listener, an
unchanged one never does, and a failing feed keeps the last good listener (SSC-053)."""

import asyncio
import json
from pathlib import Path
from typing import Any

from egress_world import ORG, PREVIEW, PROD, publish, snapshot, store

from ssc_egress.envoy import LDS_FILE, EgressConfig, Policy, policy_of
from ssc_egress.runner import ListenerWriter, follow
from ssc_shared.access import ViewHolder
from ssc_shared.snapshot_feed import SnapshotFeed, latest_key


def listener_of(lds_dir: Path) -> dict[str, Any]:
    doc = json.loads((lds_dir / LDS_FILE).read_text())
    return doc["resources"][0]


def routes(lds_dir: Path) -> list[str]:
    hcm = listener_of(lds_dir)["filter_chains"][0]["filters"][0]["typed_config"]
    return [r["name"] for r in hcm["route_config"]["virtual_hosts"][0]["routes"]]


def users(lds_dir: Path) -> list[str]:
    hcm = listener_of(lds_dir)["filter_chains"][0]["filters"][0]["typed_config"]
    auth = [f for f in hcm["http_filters"] if f["name"] == "envoy.filters.http.basic_auth"]
    if not auth:
        return []
    text = auth[0]["typed_config"]["users"]["inline_string"]
    return [line.split(":", 1)[0] for line in text.splitlines()]


def test_the_writer_moves_a_whole_file_into_place_and_skips_an_unchanged_policy(
    tmp_path: Path,
) -> None:
    writer = ListenerWriter(EgressConfig(lds_dir=str(tmp_path)))
    assert writer.write(Policy()) is True
    assert routes(tmp_path) == ["no-credentials", "no-credentials-plain"]
    assert writer.write(Policy()) is False
    assert writer.write(Policy(("api.allowed.test",), ((PROD + ".prod00000001", "A" * 27 + "="),)))
    assert routes(tmp_path) == ["allow-0", "refuse", "not-connect"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [LDS_FILE]


async def test_follow_writes_each_change_and_only_active_environments_credentials(
    tmp_path: Path,
) -> None:
    blobs = store(tmp_path / "blobs")
    lds = tmp_path / "lds"
    lds.mkdir()
    holder = ViewHolder(ORG)
    feed = SnapshotFeed(blobs, holder)
    writer = ListenerWriter(EgressConfig(lds_dir=str(lds)))
    writer.write(Policy())
    stop = asyncio.Event()
    task = asyncio.create_task(follow(feed, holder, writer, stop=stop, interval=0.01))

    async def until(check: Any) -> None:
        async with asyncio.timeout(5):
            while not check():  # noqa: ASYNC110  (polls a file the task writes)
                await asyncio.sleep(0.01)

    await publish(blobs, snapshot(1))
    await until(lambda: users(lds) != [])
    assert users(lds) == [
        PROD + ".prod00000001",
        PROD + ".prod00000002",
        PREVIEW + ".prev00000001",
    ]
    assert routes(lds) == ["allow-0", "allow-1", "refuse", "not-connect"]
    first = writer.version

    await publish(blobs, snapshot(2, compiled_at="2026-10-03T12:00:05Z"))
    await until(lambda: holder.view is not None and holder.view.version == 2)  # noqa: PLR2004
    assert writer.version == first

    await publish(blobs, snapshot(3, hosts=("api.allowed.test",)))
    await until(lambda: writer.version != first)
    assert routes(lds) == ["allow-0", "refuse", "not-connect"]
    third = writer.version

    await blobs.delete(latest_key(ORG))
    await asyncio.sleep(0.1)
    assert feed.failures > 0
    assert writer.version == third
    stop.set()
    await task


def test_no_view_or_no_egress_member_allows_nothing() -> None:
    assert policy_of(None) == Policy()
    holder = ViewHolder(ORG)
    holder.apply({k: v for k, v in snapshot().items() if k != "egress"})
    assert policy_of(holder.view) == Policy()
