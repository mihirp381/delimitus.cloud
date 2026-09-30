"""Done-when check 3: a published snapshot reaches the cell in under 5 seconds.

    uv run python -m ssc_infra.snapshot_rtt testcell01 [rounds]

Publishes versions 1..rounds of a throwaway org's snapshot to the cell bucket the way the compiler
does (object, then ``latest.json``) while a ``SnapshotFeed`` on the same bucket polls every 2 s.
Each round is timed from the start of the publish to the feed holding that version. Removes the
throwaway org's objects afterwards.
"""

import asyncio
import hashlib
import secrets
import string
import sys
import time
from typing import Any, Final

from ssc_contracts.snapshot import FORMAT_V1
from ssc_infra import naming as n
from ssc_shared.access import ViewHolder
from ssc_shared.blobstore import BlobStore
from ssc_shared.blobstore_gcs import GcsBlobStore, bucket_of
from ssc_shared.canonical import JsonValue, canonical_bytes
from ssc_shared.snapshot_feed import SnapshotFeed, latest_key, object_key, snapshot_prefix

LIMIT_SECONDS: Final = 5.0
DEFAULT_ROUNDS: Final = 5
_ALPHABET: Final = string.ascii_lowercase + string.digits


def throwaway_org() -> str:
    return "org_" + "".join(secrets.choice(_ALPHABET) for _ in range(20))


def document(org_id: str, version: int) -> dict[str, Any]:
    return {
        "format": FORMAT_V1,
        "org_id": org_id,
        "version": version,
        "compiled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environments": {},
        "hosts": {},
        "grants": {},
        "groups_by_user": {},
        "users": {},
        "ceiling": None,
    }


async def publish(store: BlobStore, org_id: str, version: int) -> None:
    raw = canonical_bytes(document(org_id, version))
    sha = hashlib.sha256(raw).hexdigest()
    key = object_key(org_id, version, sha)
    await store.put(key, raw, content_type="application/json")
    pointer: JsonValue = {"version": version, "key": key, "digest": f"sha256:{sha}"}
    await store.put(latest_key(org_id), canonical_bytes(pointer), content_type="application/json")


async def measure(store: BlobStore, rounds: int) -> list[float]:
    org_id = throwaway_org()
    feed = SnapshotFeed(store, ViewHolder(org_id))
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    times: list[float] = []
    try:
        for version in range(1, rounds + 1):
            await asyncio.sleep(secrets.randbelow(2000) / 1000)
            started = time.monotonic()
            await publish(store, org_id, version)
            while feed.version != version:
                if time.monotonic() - started > 3 * LIMIT_SECONDS:
                    raise TimeoutError(f"v{version} not applied: {feed.last_error}")
                await asyncio.sleep(0.02)
            times.append(time.monotonic() - started)
    finally:
        stop.set()
        await task
        async for info in store.list(snapshot_prefix(org_id)):
            await store.delete(info.key)
    return times


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print(__doc__, file=sys.stderr)  # noqa: T201
        return 2
    label = argv[0]
    rounds = int(argv[1]) if len(argv) == 2 else DEFAULT_ROUNDS
    store = GcsBlobStore(bucket_of(n.cell_bucket(label), project=n.cell_project(label)))
    times = asyncio.run(measure(store, rounds))
    for i, t in enumerate(times, 1):
        print(f"v{i}: {t:.2f} s")  # noqa: T201
    worst = max(times)
    verdict = "PASS" if worst < LIMIT_SECONDS else "FAIL"
    print(f"worst {worst:.2f} s, limit {LIMIT_SECONDS:.0f} s: {verdict}")  # noqa: T201
    return 0 if worst < LIMIT_SECONDS else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
