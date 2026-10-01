"""``BlobStore`` on a Google Cloud Storage bucket (SSC-013): the cell bucket that holds access
snapshots, audit anchors and source bundles.

Each object carries its sha256 in the metadata key ``sha256``; an object without it, or whose
body no longer matches it, is ``BlobCorruptError``. ``put`` holds the body in memory, so it is for
small objects; ``get`` reads in ranges pinned to one generation, so bundles stream.

Signed URLs (SSC-014, decision 015) are V4 query-signed XML API URLs, signed here with this
store's clock by a ``UrlSigner``: a service account's key, or IAM ``signBlob`` for the identity a
Cloud Run service runs as. A PUT URL signs these headers, so the bucket refuses any other value:
``x-goog-content-length-range: n,n`` (the exact length), ``x-goog-content-sha256`` (the body's
sha256, checked by the bucket), ``x-goog-meta-sha256`` (what ``stat`` reports),
``x-goog-if-generation-match: 0`` (create only: a stored object is never replaced through a URL)
and ``content-type: application/octet-stream``. A PUT URL therefore needs the sha256.
"""

import asyncio
import binascii
import hashlib
import hmac
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, Protocol, cast
from urllib.parse import quote

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
from ssc_shared.clock import Clock, SystemClock

SHA256_METADATA: Final = "sha256"
CHUNK_SIZE: Final = 64 * 1024
RANGE_SIZE: Final = 8 * 1024 * 1024
ENDPOINT: Final = "https://storage.googleapis.com"
HOST: Final = "storage.googleapis.com"
ALGORITHM: Final = "GOOG4-RSA-SHA256"
UNSIGNED_PAYLOAD: Final = "UNSIGNED-PAYLOAD"
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
    def download_as_bytes(
        self, *, start: int, end: int, if_generation_match: int | None, timeout: float
    ) -> bytes: ...


class GcsBucket(Protocol):
    """The part of ``google.cloud.storage.Bucket`` this store uses."""

    name: str

    def blob(self, blob_name: str) -> GcsBlob: ...
    def get_blob(self, blob_name: str, *, timeout: float) -> GcsBlob | None: ...
    def list_blobs(self, *, prefix: str, timeout: float) -> Iterable[GcsBlob]: ...
    def delete_blob(self, blob_name: str, *, timeout: float) -> None: ...


def bucket_of(name: str, *, project: str | None = None) -> GcsBucket:
    """The named bucket through a client on Application Default Credentials. No I/O."""
    from google.cloud import storage  # pyright: ignore[reportMissingTypeStubs]  # noqa: PLC0415

    client: Any = storage.Client(project=project)
    return cast(GcsBucket, client.bucket(name))


class UrlSigner(Protocol):
    """Signs V4 URLs as one service account: ``sign`` is RSA-SHA256 with its key."""

    @property
    def email(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


class KeySigner:
    """A service account's own key (``google.auth.crypt.Signer``); tests use a generated one."""

    def __init__(self, email: str, signer: Any) -> None:
        self._email = email
        self._signer = signer

    @property
    def email(self) -> str:
        return self._email

    def sign(self, message: bytes) -> bytes:
        return bytes(self._signer.sign(message))


class IamSigner:
    """IAM ``signBlob`` as ``email``, called with Application Default Credentials, which are read
    on the first signature. The caller needs ``iam.serviceAccounts.signBlob`` on ``email`` (Token
    Creator); for the control plane that is its own account (decision 015)."""

    def __init__(self, email: str) -> None:
        self._email = email
        self._signer: Any = None

    @property
    def email(self) -> str:
        return self._email

    def sign(self, message: bytes) -> bytes:
        if self._signer is None:
            import google.auth  # noqa: PLC0415
            from google.auth import iam  # noqa: PLC0415
            from google.auth.transport.requests import Request  # noqa: PLC0415

            default = cast(Callable[..., tuple[Any, str | None]], google.auth.default)  # pyright: ignore[reportUnknownMemberType]
            credentials = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])[0]
            self._signer = iam.Signer(Request(), credentials, self._email)
        return bytes(self._signer.sign(message))


def v4_url(  # noqa: PLR0913  (keyword-only)
    *,
    signer: UrlSigner,
    bucket: str,
    key: str,
    method: str,
    headers: Mapping[str, str],
    now: datetime,
    expires_in: timedelta,
) -> str:
    """A V4 query-signed URL for ``method`` on ``bucket/key`` that requires ``headers``."""
    timestamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    scope = f"{timestamp[:8]}/auto/storage/goog4_request"
    signed = {k.lower(): " ".join(v.split()) for k, v in {**headers, "host": HOST}.items()}
    names = sorted(signed)
    query = {
        "X-Goog-Algorithm": ALGORITHM,
        "X-Goog-Credential": f"{signer.email}/{scope}",
        "X-Goog-Date": timestamp,
        "X-Goog-Expires": str(int(expires_in.total_seconds())),
        "X-Goog-SignedHeaders": ";".join(names),
    }
    canonical_query = "&".join(
        f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(query.items())
    )
    resource = f"/{bucket}/{quote(key, safe='/~')}"
    canonical_request = "\n".join(
        [
            method,
            resource,
            canonical_query,
            "".join(f"{n}:{signed[n]}\n" for n in names),
            ";".join(names),
            signed.get("x-goog-content-sha256", UNSIGNED_PAYLOAD),
        ]
    )
    to_sign = "\n".join(
        [ALGORITHM, timestamp, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    signature = binascii.hexlify(signer.sign(to_sign.encode())).decode()
    return f"{ENDPOINT}{resource}?{canonical_query}&X-Goog-Signature={signature}"


class GcsBlobStore:
    def __init__(
        self, bucket: GcsBucket, *, signer: UrlSigner | None = None, clock: Clock | None = None
    ) -> None:
        self._bucket = bucket
        self._signer = signer
        self._clock = clock or SystemClock()

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
        h = hashlib.sha256()
        n = 0
        for start in range(0, info.size, RANGE_SIZE):
            end = min(start + RANGE_SIZE, info.size) - 1
            part = await _call(
                blob.download_as_bytes,
                start=start,
                end=end,
                if_generation_match=blob.generation,
                timeout=TIMEOUT_SECONDS,
            )
            if len(part) != end - start + 1:
                raise BlobCorruptError(f"{key} no longer matches its recorded size")
            h.update(part)
            n += len(part)
            for offset in range(0, len(part), CHUNK_SIZE):
                yield part[offset : offset + CHUNK_SIZE]
        if n != info.size or not hmac.compare_digest(h.hexdigest(), info.sha256):
            raise BlobCorruptError(f"{key} no longer matches its recorded size or sha256")

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
        length, digest = check_signing(method, content_length, sha256)
        if method == "PUT" and digest is None:
            raise ValueError("a bucket PUT URL needs the sha256")
        if self._signer is None:
            raise BlobError("this bucket store has no URL signer")
        headers: dict[str, str] = {}
        if length is not None and digest is not None:
            headers = {
                "content-type": DEFAULT_CONTENT_TYPE,
                "x-goog-content-length-range": f"{length},{length}",
                "x-goog-content-sha256": digest,
                "x-goog-if-generation-match": "0",
                f"x-goog-meta-{SHA256_METADATA}": digest,
            }
        now = self._clock.now()
        url = await _call(
            v4_url,
            signer=self._signer,
            bucket=self._bucket.name,
            key=key,
            method=method,
            headers=headers,
            now=now,
            expires_in=expires_in,
        )
        return SignedUrl(url, method, MappingProxyType(headers), now + expires_in)


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
