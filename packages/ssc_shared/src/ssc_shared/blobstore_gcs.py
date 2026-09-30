"""``BlobStore`` on a Google Cloud Storage bucket (SSC-013): the cell bucket that holds access
snapshots and audit anchors.

Each object carries its sha256 in the metadata key ``sha256``; an object without it, or whose
body no longer matches it, is ``BlobCorruptError``. Bodies are held in memory, so this store is
for small objects (snapshots, pointers, anchors). Signed URLs are not offered yet: the API serves
signed URLs only for the filesystem store, and bundle uploads to a bucket arrive with SSC-014.
"""

import asyncio
import hashlib
import hmac
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, cast

from google.api_core.exceptions import GoogleAPIError, NotFound
from google.auth.exceptions import GoogleAuthError

from ssc_shared.blobstore import (
    DEFAULT_CONTENT_TYPE,
    MAX_URL_LIFETIME,
    BlobCorruptError,
    BlobError,
    BlobInfo,
    BlobMismatchError,
    BlobNotFoundError,
    BlobTooLargeError,
    Method,
    SignedUrl,
    check_content_type,
    check_key,
    check_lifetime,
    check_prefix,
    check_sha256,
    check_signing,
)

SHA256_METADATA: Final = "sha256"
CHUNK_SIZE: Final = 64 * 1024
TIMEOUT_SECONDS: Final = 10.0
_TRANSPORT_ERRORS: Final = (GoogleAPIError, GoogleAuthError, OSError)


class GcsBlob(Protocol):
    """The part of ``google.cloud.storage.Blob`` this store uses."""

    name: str | None
    metadata: Mapping[str, str] | None
    content_type: str | None

    @property
    def size(self) -> int | None: ...
    @property
    def generation(self) -> int | None: ...
    @property
    def time_created(self) -> datetime | None: ...

    def upload_from_string(
        self, data: bytes, content_type: str, *, checksum: str, timeout: float
    ) -> None: ...
    def download_as_bytes(self, *, if_generation_match: int | None, timeout: float) -> bytes: ...


class GcsBucket(Protocol):
    """The part of ``google.cloud.storage.Bucket`` this store uses."""

    def blob(self, blob_name: str) -> GcsBlob: ...
    def get_blob(self, blob_name: str, *, timeout: float) -> GcsBlob | None: ...
    def list_blobs(self, *, prefix: str, timeout: float) -> Iterable[GcsBlob]: ...
    def delete_blob(self, blob_name: str, *, timeout: float) -> None: ...


def bucket_of(name: str, *, project: str | None = None) -> GcsBucket:
    """The named bucket through a client on Application Default Credentials. No I/O."""
    from google.cloud import storage  # pyright: ignore[reportMissingTypeStubs]  # noqa: PLC0415

    client: Any = storage.Client(project=project)
    return cast(GcsBucket, client.bucket(name))


class GcsBlobStore:
    def __init__(self, bucket: GcsBucket) -> None:
        self._bucket = bucket

    async def put(
        self,
        key: str,
        data: bytes | AsyncIterable[bytes],
        *,
        content_type: str = DEFAULT_CONTENT_TYPE,
        size: int | None = None,
        sha256: str | None = None,
    ) -> BlobInfo:
        check_key(key)
        check_content_type(content_type)
        if size is not None and size < 0:
            raise ValueError("size must be zero or more")
        if sha256 is not None:
            check_sha256(sha256)
        body = bytearray()
        if isinstance(data, bytes):
            _append(body, data, size)
        else:
            async for chunk in data:
                _append(body, bytes(chunk), size)
        digest = hashlib.sha256(body).hexdigest()
        if size is not None and len(body) != size:
            raise BlobMismatchError(f"body is {len(body)} bytes, not {size}")
        if sha256 is not None and not hmac.compare_digest(digest, sha256):
            raise BlobMismatchError("body sha256 differs from the declared one")
        blob = self._bucket.blob(key)
        blob.metadata = {SHA256_METADATA: digest}
        await _call(
            blob.upload_from_string,
            bytes(body),
            content_type,
            checksum="crc32c",
            timeout=TIMEOUT_SECONDS,
        )
        created = blob.time_created or datetime.now(UTC)
        return BlobInfo(key, len(body), digest, content_type, created)

    async def get(self, key: str) -> AsyncIterator[bytes]:
        check_key(key)
        blob = await _call(self._bucket.get_blob, key, timeout=TIMEOUT_SECONDS)
        if blob is None:
            raise BlobNotFoundError(key)
        info = _info(key, blob)
        body = await _call(
            blob.download_as_bytes, if_generation_match=blob.generation, timeout=TIMEOUT_SECONDS
        )
        if len(body) != info.size or hashlib.sha256(body).hexdigest() != info.sha256:
            raise BlobCorruptError(f"{key} no longer matches its recorded size or sha256")
        for start in range(0, len(body), CHUNK_SIZE):
            yield body[start : start + CHUNK_SIZE]

    async def stat(self, key: str) -> BlobInfo | None:
        check_key(key)
        blob = await _call(self._bucket.get_blob, key, timeout=TIMEOUT_SECONDS)
        return None if blob is None else _info(key, blob)

    async def list(self, prefix: str = "") -> AsyncIterator[BlobInfo]:
        check_prefix(prefix)
        blobs = await _call(_listed, self._bucket, prefix)
        for blob in sorted(blobs, key=lambda b: b.name or ""):
            if blob.name is not None:
                yield _info(blob.name, blob)

    async def delete(self, key: str) -> bool:
        check_key(key)
        try:
            await _call(self._bucket.delete_blob, key, timeout=TIMEOUT_SECONDS)
        except BlobNotFoundError:
            return False
        return True

    async def signed_url(
        self,
        key: str,
        *,
        method: Method,
        expires_in: timedelta = MAX_URL_LIFETIME,
        content_length: int | None = None,
        sha256: str | None = None,
    ) -> SignedUrl:
        check_key(key)
        check_lifetime(expires_in)
        check_signing(method, content_length, sha256)
        raise BlobError("the bucket store signs no URLs yet (SSC-014)")


def _append(body: bytearray, chunk: bytes, limit: int | None) -> None:
    if limit is not None and len(body) + len(chunk) > limit:
        raise BlobTooLargeError(f"body is longer than {limit} bytes")
    body.extend(chunk)


def _listed(bucket: GcsBucket, prefix: str) -> list[GcsBlob]:
    return list(bucket.list_blobs(prefix=prefix, timeout=TIMEOUT_SECONDS))


def _info(key: str, blob: GcsBlob) -> BlobInfo:
    sha = (blob.metadata or {}).get(SHA256_METADATA, "")
    try:
        check_sha256(sha)
    except ValueError as exc:
        raise BlobCorruptError(f"{key} has no recorded sha256") from exc
    return BlobInfo(
        key,
        blob.size or 0,
        sha,
        blob.content_type or DEFAULT_CONTENT_TYPE,
        blob.time_created or datetime.fromtimestamp(0, UTC),
    )


async def _call[**P, R](fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except NotFound as exc:
        raise BlobNotFoundError(str(exc)) from exc
    except _TRANSPORT_ERRORS as exc:
        raise BlobError(f"bucket call failed: {type(exc).__name__}") from exc
