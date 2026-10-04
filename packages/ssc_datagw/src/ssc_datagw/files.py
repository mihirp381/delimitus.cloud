"""The file broker (SSC-046): signed links to an app environment's own files in the cell bucket.

An app never holds storage credentials. It asks the data gateway, with its workload token, for a
link to one file, and the broker answers with a V4 signed URL valid ``LINK_SECONDS``, signed by
the gateway's own service account through IAM ``signBlob`` (no key file: SSC-095). Every file
of an environment lives under ``files/<env_id>/``, and the environment comes from the token,
never from the request, so no name an app sends can reach another environment's files; a link
is bound to its one object, so editing its path breaks the signature.

- An upload link (``PUT``) signs ``content-type`` and ``x-goog-content-length-range: 0,<cap>``,
  so the bucket refuses a body over ``MAX_FILE_BYTES`` or a different type. It replaces a file
  of the same name. It is refused while the environment's files already use ``QUOTA_BYTES``;
  the check runs when the link is made, so links made just before the quota fills can each add
  one more file.
- A download link (``GET``) signs ``response-content-disposition: attachment``, so a stored HTML
  or SVG file is saved, never rendered as a page.
- ``delete`` removes one file at once, so an app can free its quota.

Files are not scanned for viruses; that is deferred and disclosed in the data gateway contract.
"""

import asyncio
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final, Literal, Protocol

from google.api_core.exceptions import GoogleAPIError, NotFound
from google.auth.exceptions import GoogleAuthError

from ssc_shared.blobstore import DEFAULT_CONTENT_TYPE, check_content_type
from ssc_shared.blobstore_gcs import TIMEOUT_SECONDS, UrlSigner, v4_url

FILES_PREFIX: Final = "files/"
MAX_FILE_BYTES: Final = 25 * 1024 * 1024
QUOTA_BYTES: Final = 1024**3
"""What one app environment may keep, all its files together."""
LINK_SECONDS: Final = 600
MAX_NAME_LENGTH: Final = 256
_SEGMENT: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TRANSPORT_ERRORS: Final = (GoogleAPIError, GoogleAuthError, OSError)

Method = Literal["PUT", "GET"]


class FileNameError(ValueError):
    """The name is not one the broker stores."""


class FileMissingError(Exception):
    """No file of that name in the environment."""


class FilesQuotaError(Exception):
    """The environment's files already use its quota."""


class FilesUnavailableError(Exception):
    """The bucket or the signer did not answer."""


def check_name(name: str) -> str:
    """A file name: ``/``-separated segments of letters, digits, ``.``, ``_`` and ``-``, each
    starting with a letter or digit (no ``..``, no hidden or empty segment), at most
    ``MAX_NAME_LENGTH`` characters."""
    if not 0 < len(name) <= MAX_NAME_LENGTH or not all(
        _SEGMENT.fullmatch(part) for part in name.split("/")
    ):
        raise FileNameError(
            "a file name is segments of A-Z, a-z, 0-9, '.', '_' and '-' separated by '/', each "
            f"starting with a letter or digit, at most {MAX_NAME_LENGTH} characters"
        )
    return name


def environment_prefix(env_id: str) -> str:
    return f"{FILES_PREFIX}{env_id}/"


def file_key(env_id: str, name: str) -> str:
    """Where ``name`` of ``env_id`` is kept in the cell bucket."""
    return environment_prefix(env_id) + check_name(name)


def attachment(name: str) -> str:
    """The download's ``Content-Disposition``. The last segment is safe to quote as it is."""
    return f'attachment; filename="{check_name(name).rsplit("/", 1)[-1]}"'


@dataclass(frozen=True, slots=True)
class Link:
    """One method on one file until ``expires_at``; the caller sends ``headers`` with it."""

    url: str
    method: Method
    headers: Mapping[str, str]
    expires_at: datetime
    max_bytes: int | None = None

    def to_wire(self) -> dict[str, object]:
        body: dict[str, object] = {
            "url": self.url,
            "method": self.method,
            "headers": dict(self.headers),
            "expires_at": self.expires_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        if self.max_bytes is not None:
            body["max_bytes"] = self.max_bytes
        return body


class StoredFile(Protocol):
    @property
    def size(self) -> int | None: ...


class FileBucket(Protocol):
    """The part of ``google.cloud.storage.Bucket`` the broker uses."""

    name: str

    def get_blob(self, blob_name: str, *, timeout: float) -> StoredFile | None: ...
    def list_blobs(self, *, prefix: str, timeout: float) -> Iterable[StoredFile]: ...
    def delete_blob(self, blob_name: str, *, timeout: float) -> None: ...


def _used(bucket: FileBucket, prefix: str) -> int:
    return sum(blob.size or 0 for blob in bucket.list_blobs(prefix=prefix, timeout=TIMEOUT_SECONDS))


async def _call[**P, R](fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except NotFound:
        raise FileMissingError from None
    except _TRANSPORT_ERRORS as exc:
        raise FilesUnavailableError(f"bucket call failed: {type(exc).__name__}") from exc


class FileBroker:
    """Links to the files of one cell bucket, signed as ``signer``. ``clock`` is for tests."""

    def __init__(
        self,
        bucket: FileBucket,
        signer: UrlSigner,
        *,
        quota: int = QUOTA_BYTES,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._bucket = bucket
        self._signer = signer
        self._quota = quota
        self._clock = clock

    async def upload(self, env_id: str, name: str, content_type: str | None = None) -> Link:
        """A link that stores one body of at most ``MAX_FILE_BYTES`` as ``name``."""
        key = file_key(env_id, name)
        kind = check_content_type(content_type or DEFAULT_CONTENT_TYPE)
        if await _call(_used, self._bucket, environment_prefix(env_id)) >= self._quota:
            raise FilesQuotaError
        headers = {
            "content-type": kind,
            "x-goog-content-length-range": f"0,{MAX_FILE_BYTES}",
        }
        return await self._link(key, "PUT", headers, {}, MAX_FILE_BYTES)

    async def download(self, env_id: str, name: str) -> Link:
        """A link that reads ``name`` as an attachment."""
        key = file_key(env_id, name)
        if await _call(self._bucket.get_blob, key, timeout=TIMEOUT_SECONDS) is None:
            raise FileMissingError
        query = {"response-content-disposition": attachment(name)}
        return await self._link(key, "GET", {}, query, None)

    async def delete(self, env_id: str, name: str) -> None:
        """Remove ``name``; ``FileMissingError`` when there is none."""
        await _call(self._bucket.delete_blob, file_key(env_id, name), timeout=TIMEOUT_SECONDS)

    async def _link(
        self,
        key: str,
        method: Method,
        headers: dict[str, str],
        query: dict[str, str],
        max_bytes: int | None,
    ) -> Link:
        now = self._clock()
        lifetime = timedelta(seconds=LINK_SECONDS)
        url = await _call(
            v4_url,
            signer=self._signer,
            bucket=self._bucket.name,
            key=key,
            method=method,
            headers=headers,
            now=now,
            expires_in=lifetime,
            query=query,
        )
        return Link(url, method, MappingProxyType(headers), now + lifetime, max_bytes)
