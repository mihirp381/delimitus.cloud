"""SSC-012 (A1b): the worker and the operator commands read the blob store from the same
``SSC_BLOB_*`` settings as the API, and refuse the filesystem store outside dev and test."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from pathlib import Path

import pytest

from ssc_control.api.routes.blobs import blob_store_for, check_fs_allowed
from ssc_control.api.settings import Settings
from ssc_control.storage import StorageConfigError, blob_store_from_env
from ssc_control.worker import CompositionError, compose_ports
from ssc_shared.blobstore_fs import FsBlobStore

DSN = "postgresql://ssc_app@localhost/ssc"


def fs_env(root: Path, **extra: str) -> dict[str, str]:
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    return {
        "SSC_DATABASE_DSN": DSN,
        "SSC_API_JWKS": json.dumps({"keys": []}),
        "SSC_API_ISSUER": "https://auth.delimitus.com",
        "SSC_ENV": "test",
        "SSC_BLOB_BACKEND": "fs",
        "SSC_BLOB_ROOT": str(root),
        "SSC_BLOB_SIGNING_KEYS": json.dumps({"k1": key}),
        "SSC_BLOB_SIGNING_KID": "k1",
        **extra,
    }


async def read(store: FsBlobStore, key: str) -> bytes:
    return b"".join([chunk async for chunk in store.get(key)])


@pytest.mark.parametrize("public_url", [None, "http://localhost:8000/"], ids=["default", "set"])
def test_worker_and_api_build_the_same_store(tmp_path: Path, public_url: str | None) -> None:
    env = fs_env(tmp_path, **({"SSC_API_PUBLIC_URL": public_url} if public_url else {}))
    ours, theirs = blob_store_from_env(env), blob_store_for(Settings.from_env(env))
    assert isinstance(ours, FsBlobStore) and isinstance(theirs, FsBlobStore)

    async def same() -> None:
        await ours.put("audit-anchors/x/one.json", b"{}")
        assert await read(theirs, "audit-anchors/x/one.json") == b"{}"
        mine = await ours.signed_url("audit-anchors/x/one.json", method="GET")
        api = await theirs.signed_url("audit-anchors/x/one.json", method="GET")
        assert mine.url.split("?")[0] == api.url.split("?")[0]

    asyncio.run(same())


def test_none_is_the_default_and_unknown_backends_are_refused(tmp_path: Path) -> None:
    assert blob_store_from_env({}) is None
    assert blob_store_from_env({"SSC_BLOB_BACKEND": "none"}) is None
    assert blob_store_for(Settings.from_env(fs_env(tmp_path, SSC_BLOB_BACKEND="none"))) is None
    with pytest.raises(StorageConfigError, match="unknown"):
        blob_store_from_env({"SSC_BLOB_BACKEND": "gcs"})
    with pytest.raises(StorageConfigError, match="SSC_BLOB_ROOT"):
        blob_store_from_env(fs_env(tmp_path, SSC_BLOB_ROOT=""))
    with pytest.raises(StorageConfigError):
        blob_store_from_env(fs_env(tmp_path, SSC_BLOB_SIGNING_KEYS='{"k1": "c2hvcnQ="}'))
    with pytest.raises(StorageConfigError, match="JSON"):
        blob_store_from_env(fs_env(tmp_path, SSC_BLOB_SIGNING_KEYS="k1"))


@pytest.mark.parametrize("environment", [None, "prod", "staging"])
def test_the_filesystem_store_is_refused_outside_dev_and_test(
    tmp_path: Path, environment: str | None
) -> None:
    env = fs_env(tmp_path)
    if environment is None:
        del env["SSC_ENV"]
    else:
        env["SSC_ENV"] = environment
    with pytest.raises(StorageConfigError, match="dev"):
        blob_store_from_env(env)
    with pytest.raises(ValueError, match="dev"):  # the API refuses the same settings
        settings = Settings.from_env(env)
        check_fs_allowed(blob_store_for(settings), settings)
    with pytest.raises(CompositionError, match="dev"):
        compose_ports(env)


def test_the_worker_composes_the_blob_store(tmp_path: Path) -> None:
    assert isinstance(compose_ports(fs_env(tmp_path)).blob_store, FsBlobStore)
    assert compose_ports({"SSC_DATABASE_DSN": DSN}).blob_store is None
