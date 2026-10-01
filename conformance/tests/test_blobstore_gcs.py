"""The bucket ``BlobStore`` passes the contract against an in-memory bucket behind a fake XML API
endpoint that checks V4 signatures with Google's own signing code. Against a real bucket when
``SSC_TEST_GCS_BUCKET`` names it (Application Default Credentials): the core always, and the
signed-URL contract too when ``SSC_TEST_GCS_SIGNER`` names a service account the caller may sign
as (Token Creator) and that may write the bucket."""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.api_core.exceptions import NotFound, PreconditionFailed, ServiceUnavailable
from google.auth import crypt
from google.cloud.storage._signing import (  # pyright: ignore[reportMissingTypeStubs]
    generate_signed_url_v4,  # pyright: ignore[reportUnknownVariableType]
)
from google.oauth2 import service_account

from ssc_conformance.contracts.blobstore import (
    BlobStoreContract,
    BlobStoreCoreContract,
    FetchResult,
    ManualClock,
    read_all,
    sha,
)
from ssc_shared import blobstore_gcs
from ssc_shared.blobstore import (
    BlobCorruptError,
    BlobError,
    BlobKeyError,
    BlobStore,
    check_key,
    check_prefix,
)
from ssc_shared.blobstore_gcs import (
    ALGORITHM,
    ENDPOINT,
    HOST,
    GcsBlob,
    GcsBlobStore,
    IamSigner,
    KeySigner,
    bucket_of,
    v4_url,
)

LIVE_BUCKET_ENV = "SSC_TEST_GCS_BUCKET"
LIVE_SIGNER_ENV = "SSC_TEST_GCS_SIGNER"
BUCKET = "ssc-c-testcell01-cell"
SIGNER_EMAIL = "ssc-control@ssc-control-staging.iam.gserviceaccount.com"


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

    def download_as_bytes(
        self, *, start: int, end: int, if_generation_match: int | None, timeout: float
    ) -> bytes:
        assert self.name is not None
        current = self.bucket.objects.get(self.name)
        if current is None:
            raise NotFound(self.name)
        if if_generation_match is not None and current.generation != if_generation_match:
            raise PreconditionFailed(self.name)
        self.bucket.ranges.append((start, end))
        return current.body[start : end + 1]


@dataclass
class FakeBucket:
    name: str = BUCKET
    objects: dict[str, _Stored] = field(default_factory=dict)
    generation: int = 0
    ranges: list[tuple[int, int]] = field(default_factory=list)

    def store(self, name: str, body: bytes, content_type: str, metadata: dict[str, str]) -> None:
        self.generation += 1
        self.objects[name] = _Stored(body, content_type, metadata, self.generation, _now())

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


@dataclass(frozen=True)
class _Key:
    pem: str

    def signer(self) -> KeySigner:
        return KeySigner(SIGNER_EMAIL, crypt.RSASigner.from_string(self.pem))

    def credentials(self) -> Any:
        info = {
            "type": "service_account",
            "client_email": SIGNER_EMAIL,
            "private_key": self.pem,
            "token_uri": "https://oauth2.googleapis.com/token",
            "project_id": "ssc-control-staging",
        }
        return service_account.Credentials.from_service_account_info(info)  # pyright: ignore[reportUnknownMemberType]


@pytest.fixture(scope="module")
def key() -> _Key:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return _Key(pem)


def _status(code: int) -> FetchResult:
    return FetchResult(code, b"")


class FakeXmlApi:
    """The XML API's handling of a V4 query-signed request, for one bucket. The signature is
    recomputed with ``google.cloud.storage._signing``, not with the store's own signer."""

    def __init__(self, bucket: FakeBucket, key: _Key, clock: ManualClock) -> None:
        self._bucket = bucket
        self._credentials = key.credentials()
        self._clock = clock

    async def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> FetchResult:
        parts = urlsplit(url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        query = dict(pairs)
        if f"{parts.scheme}://{parts.netloc}" != ENDPOINT or len(query) != len(pairs):
            return _status(400)
        bucket, _, quoted = parts.path.removeprefix("/").partition("/")
        if bucket != self._bucket.name:
            return _status(404)
        given = {k.lower(): v for k, v in headers.items()} | {"host": HOST}
        try:
            stamp = query["X-Goog-Date"]
            signed_at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            expires = int(query["X-Goog-Expires"])
            names = query["X-Goog-SignedHeaders"].split(";")
            signed = {n: given[n] for n in names if n != "host"}
        except KeyError, ValueError:
            return _status(400)
        if "host" not in names or not 0 < expires <= 604800:
            return _status(400)
        expected = generate_signed_url_v4(
            self._credentials,
            parts.path,
            expiration=expires,
            method=method,
            headers=dict(signed),
            _request_timestamp=stamp,
        )
        if dict(parse_qsl(urlsplit(expected).query)) != query:
            return _status(403)
        if not signed_at <= self._clock.now() < signed_at + timedelta(seconds=expires):
            return _status(400)
        name = unquote(quoted)
        if method == "GET":
            stored = self._bucket.objects.get(name)
            return _status(404) if stored is None else FetchResult(200, stored.body)
        return self._put(name, signed, body or b"")

    def _put(self, name: str, signed: Mapping[str, str], body: bytes) -> FetchResult:
        low, _, high = signed.get("x-goog-content-length-range", f"0,{len(body)}").partition(",")
        if not int(low) <= len(body) <= int(high):
            return _status(400)
        want = signed.get("x-goog-content-sha256")
        if want is not None and not hmac.compare_digest(sha(body), want):
            return _status(400)
        if signed.get("x-goog-if-generation-match") == "0" and name in self._bucket.objects:
            return _status(412)
        metadata = {
            k.removeprefix("x-goog-meta-"): v
            for k, v in signed.items()
            if k.startswith("x-goog-meta-")
        }
        content_type = signed.get("content-type", "application/octet-stream")
        self._bucket.store(name, body, content_type, metadata)
        return _status(200)


class TestGcsBlobStoreInMemory(BlobStoreContract):
    put_needs_sha256 = True

    @pytest.fixture
    def bucket(self) -> FakeBucket:
        return FakeBucket()

    @pytest.fixture
    def clock(self) -> ManualClock:
        return ManualClock()

    @pytest.fixture
    def blob_store(self, bucket: FakeBucket, key: _Key, clock: ManualClock) -> GcsBlobStore:
        return GcsBlobStore(bucket, signer=key.signer(), clock=clock)

    @pytest.fixture
    def fetch(self, bucket: FakeBucket, key: _Key, clock: ManualClock) -> FakeXmlApi:
        return FakeXmlApi(bucket, key, clock)

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

    async def test_a_put_url_signs_every_binding(self, blob_store: BlobStore) -> None:
        digest = sha(b"abc")
        put = await blob_store.signed_url("m/k", method="PUT", content_length=3, sha256=digest)
        assert dict(put.headers) == {
            "content-type": "application/octet-stream",
            "x-goog-content-length-range": "3,3",
            "x-goog-content-sha256": digest,
            "x-goog-if-generation-match": "0",
            "x-goog-meta-sha256": digest,
        }
        query = dict(parse_qsl(urlsplit(put.url).query))
        assert query["X-Goog-SignedHeaders"] == ";".join(sorted([*put.headers, "host"]))
        assert query["X-Goog-Expires"] == "600"
        assert put.url.startswith(f"{ENDPOINT}/{BUCKET}/m/k?")
        get = await blob_store.signed_url("m/k", method="GET")
        assert dict(get.headers) == {}

    async def test_a_put_url_never_replaces_an_object(
        self, blob_store: BlobStore, fetch: FakeXmlApi
    ) -> None:
        put = await blob_store.signed_url("m/k", method="PUT", content_length=3, sha256=sha(b"abc"))
        assert (await fetch("PUT", put.url, put.headers, b"abc")).status == 200
        assert (await fetch("PUT", put.url, put.headers, b"abc")).status == 412
        info = await blob_store.stat("m/k")
        assert info is not None and info.sha256 == sha(b"abc")

    async def test_urls_are_the_ones_googles_library_signs(self, key: _Key) -> None:
        now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        digest = sha(b"abc")
        headers = {"content-type": "application/octet-stream", "x-goog-content-sha256": digest}
        for method, signed in (("PUT", headers), ("GET", {})):
            ours = v4_url(
                signer=key.signer(),
                bucket=BUCKET,
                key="bundles/org_a/app_b/sha256/x=y.tar.gz",
                method=method,
                headers=signed,
                now=now,
                expires_in=timedelta(minutes=10),
            )
            theirs = generate_signed_url_v4(
                key.credentials(),
                f"/{BUCKET}/bundles/org_a/app_b/sha256/x%3Dy.tar.gz",
                expiration=timedelta(minutes=10),
                method=method,
                headers=dict(signed),
                _request_timestamp="20261001T120000Z",
            )
            assert ours == theirs
            assert f"X-Goog-Algorithm={ALGORITHM}" in ours

    async def test_without_a_signer_urls_are_a_blob_error(self, bucket: FakeBucket) -> None:
        with pytest.raises(BlobError, match="signer"):
            await GcsBlobStore(bucket).signed_url("m/k", method="GET")

    async def test_a_signing_failure_is_a_blob_error(self, bucket: FakeBucket) -> None:
        class Refusing:
            email = SIGNER_EMAIL

            def sign(self, message: bytes) -> bytes:
                raise OSError("iamcredentials unreachable")

        with pytest.raises(BlobError):
            await GcsBlobStore(bucket, signer=Refusing()).signed_url("m/k", method="GET")

    async def test_get_reads_in_ranges_of_one_generation(
        self, blob_store: BlobStore, bucket: FakeBucket, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(blobstore_gcs, "RANGE_SIZE", 4)
        await blob_store.put("m/k", b"0123456789")
        assert await read_all(blob_store.get("m/k")) == b"0123456789"
        assert bucket.ranges == [(0, 3), (4, 7), (8, 9)]
        bucket.objects["m/k"].body = b"0123456780"
        with pytest.raises(BlobCorruptError):
            await read_all(blob_store.get("m/k"))

    async def test_an_empty_object_reads_no_range(
        self, blob_store: BlobStore, bucket: FakeBucket
    ) -> None:
        await blob_store.put("m/k", b"")
        assert await read_all(blob_store.get("m/k")) == b""
        assert bucket.ranges == []

    async def test_a_lying_upload_is_corrupt_on_read(
        self, blob_store: BlobStore, bucket: FakeBucket
    ) -> None:
        bucket.store("m/k", b"abd", "application/octet-stream", {"sha256": sha(b"abc")})
        info = await blob_store.stat("m/k")
        assert info is not None and info.sha256 == sha(b"abc")
        with pytest.raises(BlobCorruptError):
            await read_all(blob_store.get("m/k"))


def _live_root() -> str:
    return f"conformance/{uuid.uuid4().hex}"


async def _remove(store: _Prefixed, root: str) -> None:
    async for info in store.inner.list(root + "/"):
        await store.inner.delete(info.key)


@pytest.mark.skipif(not os.environ.get(LIVE_BUCKET_ENV), reason=f"{LIVE_BUCKET_ENV} not set")
class TestGcsBlobStoreLive(BlobStoreCoreContract):
    """Each test writes under its own random prefix and removes what it wrote."""

    @pytest.fixture
    async def blob_store(self) -> AsyncIterator[BlobStore]:
        root = _live_root()
        store = _Prefixed(GcsBlobStore(bucket_of(os.environ[LIVE_BUCKET_ENV])), root)
        yield store
        await _remove(store, root)


async def _http(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> FetchResult:
    async with httpx2.AsyncClient(timeout=30, follow_redirects=False) as client:
        r = await client.request(method, url, headers=dict(headers), content=body)
        return FetchResult(r.status_code, r.content)


@pytest.mark.skipif(
    not (os.environ.get(LIVE_BUCKET_ENV) and os.environ.get(LIVE_SIGNER_ENV)),
    reason=f"{LIVE_BUCKET_ENV} and {LIVE_SIGNER_ENV} not set",
)
class TestGcsSignedUrlsLive(BlobStoreContract):
    """The signed-URL contract against the real XML API, signed through IAM ``signBlob``."""

    put_needs_sha256 = True

    @pytest.fixture
    def clock(self) -> ManualClock:
        return ManualClock()

    @pytest.fixture
    async def blob_store(self, clock: ManualClock) -> AsyncIterator[BlobStore]:
        root = _live_root()
        inner = GcsBlobStore(
            bucket_of(os.environ[LIVE_BUCKET_ENV]),
            signer=IamSigner(os.environ[LIVE_SIGNER_ENV]),
            clock=clock,
        )
        store = _Prefixed(inner, root)
        yield store
        await _remove(store, root)

    @pytest.fixture
    def fetch(self) -> object:
        return _http


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
