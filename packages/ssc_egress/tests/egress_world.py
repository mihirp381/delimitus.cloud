"""Shared fixtures for the egress proxy tests: one org's snapshot with an allowlist."""

import hashlib
from pathlib import Path
from typing import Any

from ssc_contracts.egress import token_digest
from ssc_contracts.snapshot import FORMAT_V1
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.canonical import canonical_bytes
from ssc_shared.clock import SystemClock
from ssc_shared.snapshot_feed import latest_key, object_key

ORG = "org_" + "e" * 20
APP = "app_" + "e" * 20
PROD = "env_" + "p" * 20
PREVIEW = "env_" + "v" * 20
STOPPED = "env_" + "s" * 20
TOKEN = "prod-token-" + "x" * 32
NEXT_TOKEN = "prod-token-" + "y" * 32
PREVIEW_TOKEN = "preview-token-" + "z" * 32
STOPPED_TOKEN = "stopped-token-" + "w" * 32
SIGNING_KEY = ("egress-" + "test-" + "signing-" + "key-").encode() * 2


def snapshot(
    version: int = 1,
    *,
    hosts: tuple[str, ...] = ("api.allowed.test", "*.wild.test"),
    **changes: Any,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "format": FORMAT_V1,
        "org_id": ORG,
        "version": version,
        "compiled_at": "2026-10-03T12:00:00Z",
        "environments": {
            PROD: {"app_id": APP, "name": "prod", "status": "active", "floor": "user"},
            PREVIEW: {"app_id": APP, "name": "preview", "status": "active", "floor": "builder"},
            STOPPED: {"app_id": APP, "name": "prod", "status": "disabled", "floor": "user"},
        },
        "hosts": {},
        "grants": {},
        "groups_by_user": {},
        "users": {},
        "ceiling": None,
        "egress": {
            "hosts": list(hosts),
            "credentials": {
                PROD: [
                    {"credential_id": "prod00000001", "sha1": token_digest(TOKEN)},
                    {"credential_id": "prod00000002", "sha1": token_digest(NEXT_TOKEN)},
                ],
                PREVIEW: [{"credential_id": "prev00000001", "sha1": token_digest(PREVIEW_TOKEN)}],
                STOPPED: [{"credential_id": "stop00000001", "sha1": token_digest(STOPPED_TOKEN)}],
            },
        },
    }
    doc.update(changes)
    return doc


def store(root: Path) -> FsBlobStore:
    signer = UrlSigner({"k1": SIGNING_KEY}, active="k1", clock=SystemClock())
    return FsBlobStore(root, signer=signer, base_url="http://blobs.test")


async def publish(blobs: FsBlobStore, doc: dict[str, Any]) -> None:
    raw = canonical_bytes(doc)
    sha = hashlib.sha256(raw).hexdigest()
    key = object_key(ORG, int(doc["version"]), sha)
    await blobs.put(key, raw)
    pointer = {"version": doc["version"], "key": key, "digest": f"sha256:{sha}"}
    await blobs.put(latest_key(ORG), canonical_bytes(pointer))
