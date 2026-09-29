"""``BlobStore`` on a local directory, with HMAC-signed URLs served by the control plane.

Each object is one file, ``<root>/<key>@blob``: a fixed 512-byte JSON header (size, sha256,
content type, created_at) and then the body. ``@`` is outside the key alphabet, so ``a`` and
``a/b`` coexist. Writes go to a temp file in the same directory, are fsynced and then renamed, so
readers see the old object or the new one, never a mix.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import IO, Final, Literal, Self
from urllib.parse import urlencode

from ssc_shared.blobstore import (
    DEFAULT_CONTENT_TYPE,
    MAX_URL_LIFETIME,
    BlobCorruptError,
    BlobError,
    BlobInfo,
    BlobMismatchError,
    BlobNotFoundError,
    BlobStore,
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

HEADER_SIZE: Final = 512
CHUNK_SIZE: Final = 64 * 1024
SUFFIX: Final = "@blob"
MIN_SIGNING_KEY_BYTES: Final = 32
CLOCK_SKEW: Final = timedelta(seconds=5)
SIGNED_PARAMS: Final = frozenset({"m", "exp", "len", "sha", "kid", "sig"})

Reason = Literal["malformed", "unknown_key", "bad_signature", "wrong_method", "expired", "too_long"]

_KID = re.compile(r"[a-z0-9-]{1,32}")
_SIG = re.compile(r"[A-Za-z0-9_-]{43}")
_DIGITS = re.compile(r"[0-9]{1,15}")
_SHA = re.compile(r"[0-9a-f]{64}")


class SignedUrlError(BlobError):
    """A signed URL was refused; ``reason`` says why (never shown to the caller in detail)."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


@dataclass(frozen=True, slots=True)
class PutGrant:
    key: str
    content_length: int
    sha256: str | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class GetGrant:
    key: str
    expires_at: datetime


class UrlSigner:
    """Signs and checks ``m, exp, len, sha, kid, sig`` query parameters with HMAC-SHA256.

    Every key in ``keys`` verifies; ``active`` signs. Rotate by adding a key, switching
    ``active``, and dropping the old key once its URLs (10 minutes at most) have expired.
    """

    def __init__(self, keys: Mapping[str, bytes], *, active: str, clock: Clock) -> None:
        for kid, key in keys.items():
            if not _KID.fullmatch(kid) or len(key) < MIN_SIGNING_KEY_BYTES:
                raise ValueError(f"signing key {kid!r} needs a-z0-9- id and 32+ bytes")
        if active not in keys:
            raise ValueError(f"active signing key {active!r} is not in keys")
        self._keys = MappingProxyType(dict(keys))
        self._active = active
        self._clock = clock

    def sign(
        self,
        method: Method,
        key: str,
        *,
        expires_in: timedelta = MAX_URL_LIFETIME,
        content_length: int | None = None,
        sha256: str | None = None,
    ) -> tuple[dict[str, str], datetime]:
        """Query parameters and the expiry, rounded down to a whole second."""
        check_key(key)
        check_lifetime(expires_in)
        length, sha = check_signing(method, content_length, sha256)
        exp = int((self._clock.now() + expires_in).timestamp())
        params = {"m": method, "exp": str(exp)}
        if length is not None:
            params["len"] = str(length)
        if sha is not None:
            params["sha"] = sha
        params["kid"] = self._active
        params["sig"] = self._mac(self._keys[self._active], params, key)
        return params, datetime.fromtimestamp(exp, UTC)

    def verify(self, method: Method, key: str, params: Mapping[str, str]) -> PutGrant | GetGrant:
        """Check a request against its URL parameters; raises ``SignedUrlError``."""
        if not self._well_formed(params):
            raise SignedUrlError("malformed")
        secret = self._keys.get(params["kid"])
        if secret is None:
            raise SignedUrlError("unknown_key")
        if not hmac.compare_digest(self._mac(secret, params, key), params["sig"]):
            raise SignedUrlError("bad_signature")
        if params["m"] != method:
            raise SignedUrlError("wrong_method")
        exp = int(params["exp"])
        now = self._clock.now().timestamp()
        if now >= exp:
            raise SignedUrlError("expired")
        if exp - now > (MAX_URL_LIFETIME + CLOCK_SKEW).total_seconds():
            raise SignedUrlError("too_long")
        expires_at = datetime.fromtimestamp(exp, UTC)
        if method == "GET":
            return GetGrant(key=key, expires_at=expires_at)
        return PutGrant(
            key=key,
            content_length=int(params["len"]),
            sha256=params.get("sha"),
            expires_at=expires_at,
        )

    @staticmethod
    def _well_formed(params: Mapping[str, str]) -> bool:
        if not {"m", "exp", "kid", "sig"} <= params.keys() <= SIGNED_PARAMS:
            return False
        put = params["m"] == "PUT"
        return (
            params["m"] in ("GET", "PUT")
            and bool(_DIGITS.fullmatch(params["exp"]))
            and bool(_KID.fullmatch(params["kid"]))
            and bool(_SIG.fullmatch(params["sig"]))
            and ("len" in params) == put
            and bool(_DIGITS.fullmatch(params.get("len", "0")))
            and (put or "sha" not in params)
            and bool(_SHA.fullmatch(params.get("sha", "0" * 64)))
        )

    @staticmethod
    def _mac(secret: bytes, params: Mapping[str, str], key: str) -> str:
        fields = ("ssc-blob-v1", params["m"], key, params["exp"], *_optional(params))
        digest = hmac.new(secret, "\n".join(fields).encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _optional(params: Mapping[str, str]) -> tuple[str, str]:
    return params.get("len", ""), params.get("sha", "")


class FsBlobStore:
    """``BlobStore`` on a directory. ``base_url`` is where the control plane serves signed URLs."""

    def __init__(
        self,
        root: Path,
        *,
        signer: UrlSigner,
        base_url: str,
        clock: Clock | None = None,
    ) -> None:
        self._root = root
        self._signer = signer
        self._base_url = base_url.rstrip("/")
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
        writer = await asyncio.to_thread(_Writer.open, self._path(key), size)
        try:
            if isinstance(data, bytes):
                await asyncio.to_thread(writer.write, data)
            else:
                async for chunk in data:
                    await asyncio.to_thread(writer.write, bytes(chunk))
            info = writer.finish(key, content_type, sha256, self._clock.now())
            await asyncio.to_thread(writer.commit, info)
        except BaseException:
            await asyncio.to_thread(writer.abort)
            raise
        return info

    async def get(self, key: str) -> AsyncIterator[bytes]:
        check_key(key)
        reader = await asyncio.to_thread(_Reader.open, self._path(key), key)
        async for chunk in _stream(reader):
            yield chunk

    async def stat(self, key: str) -> BlobInfo | None:
        check_key(key)
        return await asyncio.to_thread(_stat, self._path(key), key)

    async def list(self, prefix: str = "") -> AsyncIterator[BlobInfo]:
        check_prefix(prefix)
        keys = await asyncio.to_thread(_keys, self._root, prefix)
        for key in keys:
            info = await self.stat(key)
            if info is not None:
                yield info

    async def delete(self, key: str) -> bool:
        check_key(key)
        return await asyncio.to_thread(_delete, self._path(key))

    async def signed_url(
        self,
        key: str,
        *,
        method: Method,
        expires_in: timedelta = MAX_URL_LIFETIME,
        content_length: int | None = None,
        sha256: str | None = None,
    ) -> SignedUrl:
        params, expires_at = self._signer.sign(
            method, key, expires_in=expires_in, content_length=content_length, sha256=sha256
        )
        headers = {"content-length": params["len"]} if method == "PUT" else {}
        return SignedUrl(
            url=f"{self._base_url}/{key}?{urlencode(params)}",
            method=method,
            headers=MappingProxyType(headers),
            expires_at=expires_at,
        )

    async def accept_put(
        self,
        key: str,
        params: Mapping[str, str],
        body: bytes | AsyncIterable[bytes],
        *,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> BlobInfo:
        """Serve a signed PUT: check the URL, then store exactly the granted length and sha."""
        check_key(key)
        grant = self._signer.verify("PUT", key, params)
        assert isinstance(grant, PutGrant)
        return await self.put(
            key, body, content_type=content_type, size=grant.content_length, sha256=grant.sha256
        )

    async def open_get(
        self, key: str, params: Mapping[str, str]
    ) -> tuple[BlobInfo, AsyncIterator[bytes]]:
        """Serve a signed GET: check the URL and open the object before any byte is sent."""
        check_key(key)
        self._signer.verify("GET", key, params)
        reader = await asyncio.to_thread(_Reader.open, self._path(key), key)
        return reader.info, _stream(reader)

    def _path(self, key: str) -> Path:
        return self._root / (key + SUFFIX)


def _keys(root: Path, prefix: str) -> list[str]:
    found: list[str] = []
    for folder, _, files in os.walk(root / prefix[: prefix.rfind("/") + 1]):
        base = Path(folder).relative_to(root).as_posix()
        for name in files:
            if name.endswith(SUFFIX):
                stem = name.removesuffix(SUFFIX)
                key = stem if base == "." else f"{base}/{stem}"
                if key.startswith(prefix):
                    found.append(key)
    return sorted(found)


async def _stream(reader: _Reader) -> AsyncIterator[bytes]:
    try:
        while chunk := await asyncio.to_thread(reader.read):
            yield chunk
    finally:
        reader.close()


def _header(info: BlobInfo) -> bytes:
    raw = json.dumps(
        {
            "v": 1,
            "size": info.size,
            "sha256": info.sha256,
            "content_type": info.content_type,
            "created_at": info.created_at.isoformat(),
        },
        separators=(",", ":"),
    ).encode()
    if len(raw) >= HEADER_SIZE:
        raise BlobError("blob header does not fit")
    return raw.ljust(HEADER_SIZE - 1) + b"\n"


def _parse_header(raw: bytes, key: str) -> BlobInfo:
    try:
        meta = json.loads(raw)
        if len(raw) != HEADER_SIZE or meta["v"] != 1 or int(meta["size"]) < 0:
            raise ValueError(key)
        return BlobInfo(
            key=key,
            size=int(meta["size"]),
            sha256=check_sha256(meta["sha256"]),
            content_type=str(meta["content_type"]),
            created_at=datetime.fromisoformat(meta["created_at"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise BlobCorruptError(f"blob {key!r} has a damaged header") from exc


def _stat(path: Path, key: str) -> BlobInfo | None:
    try:
        with path.open("rb") as f:
            return _parse_header(f.read(HEADER_SIZE), key)
    except FileNotFoundError, NotADirectoryError:
        return None


class _Writer:
    """A temp file beside the target; ``commit`` renames it into place, ``abort`` removes it."""

    def __init__(self, target: Path, temp: Path, file: IO[bytes], limit: int | None) -> None:
        self._target = target
        self._temp = temp
        self._file = file
        self._limit = limit
        self._size = 0
        self._hash = hashlib.sha256()
        self._lock = threading.Lock()

    @classmethod
    def open(cls, target: Path, limit: int | None) -> Self:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        file = temp.open("xb")
        file.write(b"\0" * HEADER_SIZE)
        return cls(target, temp, file, limit)

    def write(self, chunk: bytes) -> None:
        with self._lock:
            if self._limit is not None and self._size + len(chunk) > self._limit:
                raise BlobTooLargeError(f"body is longer than {self._limit} bytes")
            self._file.write(chunk)
            self._hash.update(chunk)
            self._size += len(chunk)

    def finish(self, key: str, content_type: str, sha256: str | None, now: datetime) -> BlobInfo:
        digest = self._hash.hexdigest()
        if self._limit is not None and self._size != self._limit:
            raise BlobMismatchError(f"body is {self._size} bytes, not {self._limit}")
        if sha256 is not None and not hmac.compare_digest(digest, sha256):
            raise BlobMismatchError("body sha256 differs from the declared one")
        return BlobInfo(key, self._size, digest, content_type, now)

    def commit(self, info: BlobInfo) -> None:
        with self._lock:
            self._file.seek(0)
            self._file.write(_header(info))
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            self._temp.replace(self._target)
            _fsync_dir(self._target.parent)

    def abort(self) -> None:
        with self._lock:
            self._file.close()
            self._temp.unlink(missing_ok=True)


def _delete(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError, NotADirectoryError:
        return False
    _fsync_dir(path.parent)
    return True


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _Reader:
    """An open object: header parsed, body read in chunks and checked against size and sha256."""

    def __init__(self, file: IO[bytes], info: BlobInfo) -> None:
        self._file = file
        self.info = info
        self._left = info.size
        self._hash = hashlib.sha256()

    @classmethod
    def open(cls, path: Path, key: str) -> Self:
        try:
            file = path.open("rb")
        except FileNotFoundError, NotADirectoryError:
            raise BlobNotFoundError(key) from None
        try:
            return cls(file, _parse_header(file.read(HEADER_SIZE), key))
        except BaseException:
            file.close()
            raise

    def read(self) -> bytes:
        if self._left == 0:
            return b""
        chunk = self._file.read(min(CHUNK_SIZE, self._left))
        self._left -= len(chunk)
        self._hash.update(chunk)
        if not chunk or (self._left == 0 and self._hash.hexdigest() != self.info.sha256):
            raise BlobCorruptError(f"blob {self.info.key!r} does not match its header")
        return chunk

    def close(self) -> None:
        self._file.close()


_CONFORMS: type[BlobStore] = FsBlobStore
