"""Blob storage seam: the protocol every store implements, its key rule and its errors.

Bundles, build logs and snapshots live behind this protocol. The filesystem store is in
``blobstore_fs``; a cloud bucket binding (SSC-013) must pass the same contract suite in
``ssc_conformance.contracts.blobstore``.
"""

import re
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol

MAX_URL_LIFETIME: Final = timedelta(minutes=10)
MAX_KEY_LENGTH: Final = 512
MAX_CONTENT_TYPE_LENGTH: Final = 200
DEFAULT_CONTENT_TYPE: Final = "application/octet-stream"

Method = Literal["GET", "PUT"]

_SEGMENT = re.compile(r"[a-z0-9][a-z0-9_.=-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CONTENT_TYPE = re.compile(r"[!#-\[\]-~]+/[!#-\[\]-~]+( ?; ?[!#-\[\]-~]+)*")


class BlobError(Exception):
    """Base for blob store refusals."""


class BlobKeyError(BlobError, ValueError):
    """The key or prefix breaks the key rule."""


class BlobNotFoundError(BlobError):
    """No object under that key."""


class BlobTooLargeError(BlobError):
    """The body is longer than the declared size."""


class BlobMismatchError(BlobError):
    """The body is shorter than the declared size, or its sha256 differs."""


class BlobCorruptError(BlobError):
    """A stored object no longer matches its recorded size or sha256."""


@dataclass(frozen=True, slots=True)
class BlobInfo:
    """One stored object. ``sha256`` is lowercase hex of the body."""

    key: str
    size: int
    sha256: str
    content_type: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SignedUrl:
    """A URL that allows one method on one key until ``expires_at``; send ``headers`` with it."""

    url: str
    method: Method
    headers: Mapping[str, str]
    expires_at: datetime


def check_key(key: str) -> str:
    """Refuse keys outside ``segment(/segment)*``, each ``[a-z0-9][a-z0-9_.=-]*``, no ``..``."""
    if (
        not 0 < len(key) <= MAX_KEY_LENGTH
        or ".." in key
        or not all(_SEGMENT.fullmatch(s) for s in key.split("/"))
    ):
        raise BlobKeyError(f"bad blob key {key!r}")
    return key


def check_prefix(prefix: str) -> str:
    """A list prefix: empty, or a key, optionally ending in ``/``."""
    if prefix:
        check_key(prefix.removesuffix("/"))
    return prefix


def check_sha256(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("sha256 must be 64 lowercase hex characters")
    return value


def check_content_type(value: str) -> str:
    if len(value) > MAX_CONTENT_TYPE_LENGTH or not _CONTENT_TYPE.fullmatch(value):
        raise ValueError(f"bad content type {value!r}")
    return value


def check_lifetime(expires_in: timedelta) -> timedelta:
    if not timedelta(0) < expires_in <= MAX_URL_LIFETIME:
        raise ValueError("a signed URL lives more than 0 seconds and at most 10 minutes")
    return expires_in


def check_signing(
    method: Method, content_length: int | None, sha256: str | None
) -> tuple[int | None, str | None]:
    """PUT needs the exact length (and may pin the sha256); GET takes neither."""
    if method == "PUT":
        if content_length is None or content_length < 0:
            raise ValueError("a PUT URL needs the exact content length")
        return content_length, None if sha256 is None else check_sha256(sha256)
    if content_length is not None or sha256 is not None:
        raise ValueError("a GET URL takes no content length or sha256")
    return None, None


class BlobStore(Protocol):
    """Async object storage under checked keys. Writes are all or nothing."""

    async def put(
        self,
        key: str,
        data: bytes | AsyncIterable[bytes],
        *,
        content_type: str = DEFAULT_CONTENT_TYPE,
        size: int | None = None,
        sha256: str | None = None,
    ) -> BlobInfo:
        """Store or replace ``key``. With ``size``/``sha256`` a body that differs stores nothing."""
        ...

    def get(self, key: str) -> AsyncIterator[bytes]:
        """Stream the body; ``BlobNotFoundError`` is raised on the first iteration."""
        ...

    async def stat(self, key: str) -> BlobInfo | None: ...

    def list(self, prefix: str = "") -> AsyncIterator[BlobInfo]:
        """Objects whose key starts with ``prefix`` (plain string match), sorted by key."""
        ...

    async def signed_url(
        self,
        key: str,
        *,
        method: Method,
        expires_in: timedelta = MAX_URL_LIFETIME,
        content_length: int | None = None,
        sha256: str | None = None,
    ) -> SignedUrl:
        """A URL for one method on ``key``. PUT URLs are bound to the length and optional sha."""
        ...
