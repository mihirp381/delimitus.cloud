"""The bucket ``BlobStore`` passes the core contract: against an in-memory bucket always, and
against a real one when ``SSC_TEST_GCS_BUCKET`` names it (Application Default Credentials)."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed, ServiceUnavailable

from ssc_conformance.contracts.blobstore import BlobStoreCoreContract, read_all
from ssc_shared.blobstore import (
    BlobCorruptError,
    BlobError,
    BlobKeyError,
    BlobStore,
    check_key,
    check_prefix,
)
from ssc_shared.blobstore_gcs import GcsBlob, GcsBlobStore, bucket_of

LIVE_BUCKET_ENV = "SSC_TEST_GCS_BUCKET"


@dataclass
class _Stored:
    body: bytes
    content_type: str
    metadata: dict[str, str]
    generation: int
    created: datetime


@dataclass
class FakeBlob:
    """Behaves like ``google.cloud.storage.Blob`` for the calls the store makes."""

    bucket: FakeBucket
    name: str | None
    metadata: Mapping[str, str] | None = None
    content_type: str | None = None
    stored: _Stored | None = field(default=None, repr=False)

    @property
    def size(self) -> int | None:
        return None if self.stored is None else len(self.stored.body)

    @property
    def generation(self) -> int | None:
        return None if self.stored is None else self.stored.generation

    @property
    def time_created(self) -> datetime | None:
        return None if self.stored is None else self.stored.created

    def upload_from_string(
        self, data: bytes, content_type: str, *, checksum: str, timeout: float
    ) -> None:
        assert checksum == "crc32c"
        assert self.name is not None
        self.bucket.generation += 1
        self.stored = _Stored(
            data, content_type, dict(self.metadata or {}), self.bucket.generation, _now()
        )
        self.bucket.objects[self.name] = self.stored

    def download_as_bytes(self, *, if_generation_match: int | None, timeout: float) -> bytes:
        assert self.name is not None
        current = self.bucket.objects.get(self.name)
        if current is None:
            raise NotFound(self.name)
        if if_generation_match is not None and current.generation != if_generation_match:
            raise PreconditionFailed(self.name)
        return current.body


@dataclass
class FakeBucket:
    objects: dict[str, _Stored] = field(default_factory=dict)
    generation: int = 0

    def blob(self, blob_name: str) -> GcsBlob:
        return FakeBlob(self, blob_name)

    def _view(self, name: str, stored: _Stored) -> FakeBlob:
        return FakeBlob(self, name, dict(stored.metadata), stored.content_type, replace(stored))

    def get_blob(self, blob_name: str, *, timeout: float) -> GcsBlob | None:
        stored = self.objects.get(blob_name)
        return None if stored is None else self._view(blob_name, stored)

    def list_blobs(self, *, prefix: str, timeout: float) -> Iterable[GcsBlob]:
        return [self._view(n, s) for n, s in sorted(self.objects.items()) if n.startswith(prefix)]

    def delete_blob(self, blob_name: str, *, timeout: float) -> None:
        if self.objects.pop(blob_name, None) is None:
            raise NotFound(blob_name)


def _now() -> datetime:
    return datetime.now(UTC)


class TestGcsBlobStoreInMemory(BlobStoreCoreContract):
    @pytest.fixture
    def bucket(self) -> FakeBucket:
        return FakeBucket()

    @pytest.fixture
    def blob_store(self, bucket: FakeBucket) -> BlobStore:
        return GcsBlobStore(bucket)

    async def test_the_sha256_is_kept_in_the_metadata(
        self, blob_store: BlobStore, bucket: FakeBucket
    ) -> None:
        await blob_store.put("m/k", b"abc", content_type="text/plain")
        stored = bucket.objects["m/k"]
        assert stored.metadata == {"sha256": hashlib.sha256(b"abc").hexdigest()}
        assert stored.content_type == "text/plain"

    async def test_a_changed_body_is_corrupt(
        self, blob_store: BlobStore, bucket: FakeBucket
    ) -> None:
        await blob_store.put("m/k", b"abc")
        bucket.objects["m/k"].body = b"abd"
        with pytest.raises(BlobCorruptError):
            await read_all(blob_store.get("m/k"))

    async def test_an_object_without_a_sha256_is_corrupt(
        self, blob_store: BlobStore, bucket: FakeBucket
    ) -> None:
        await blob_store.put("m/k", b"abc")
        bucket.objects["m/k"].metadata = {}
        with pytest.raises(BlobCorruptError):
            await blob_store.stat("m/k")

    async def test_a_bucket_failure_is_a_blob_error(self) -> None:
        class Down(FakeBucket):
            def get_blob(self, blob_name: str, *, timeout: float) -> GcsBlob | None:
                raise ServiceUnavailable("down")

        with pytest.raises(BlobError):
            await GcsBlobStore(Down()).stat("m/k")

    async def test_signed_urls_wait_for_ssc_014(self, blob_store: BlobStore) -> None:
        with pytest.raises(BlobError, match="SSC-014"):
            await blob_store.signed_url("m/k", method="GET")


@pytest.mark.skipif(not os.environ.get(LIVE_BUCKET_ENV), reason=f"{LIVE_BUCKET_ENV} not set")
class TestGcsBlobStoreLive(BlobStoreCoreContract):
    """Each test writes under its own random prefix and removes what it wrote."""

    @pytest.fixture
    async def blob_store(self) -> AsyncIterator[BlobStore]:
        root = f"conformance/{uuid.uuid4().hex}"
        store = _Prefixed(GcsBlobStore(bucket_of(os.environ[LIVE_BUCKET_ENV])), root)
        yield store
        async for info in store.inner.list(root + "/"):
            await store.inner.delete(info.key)


class _Prefixed:
    """Runs the contract's keys under a private prefix of a shared bucket. A key or prefix the
    store refuses is passed through unchanged, so the refusal is still the store's own."""

    def __init__(self, inner: GcsBlobStore, root: str) -> None:
        self.inner = inner
        self._root = root

    def _k(self, key: str) -> str:
        try:
            check_key(key)
        except BlobKeyError:
            return key
        return f"{self._root}/{key}"

    def __getattr__(self, name: str) -> object:
        fn = getattr(self.inner, name)
        if name == "list":
            return self._list

        def call(key: str, *args: object, **kwargs: object) -> object:
            return fn(self._k(key), *args, **kwargs)

        return call

    async def _list(self, prefix: str = "") -> AsyncIterator[object]:
        try:
            check_prefix(prefix)
        except BlobKeyError:
            async for info in self.inner.list(prefix):
                yield info
            return
        async for info in self.inner.list(f"{self._root}/{prefix}"):
            yield replace(info, key=info.key.removeprefix(self._root + "/"))
