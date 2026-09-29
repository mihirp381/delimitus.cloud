"""The server's reading of an uploaded ``.tar.gz``: refuse what the client would never produce.

Streaming, without extracting. Refused as malformed: absolute paths, ``.``/``..`` segments,
backslashes, names that are not NFC UTF-8, duplicates, links, devices, FIFOs, sparse files,
``.env`` files, and any non-zero byte after the end of the archive (another tar reader could
find entries there that this one never saw). Over a cap: more entries than ``max_files``, file
sizes past ``max_unpacked_bytes``, or a decompressed stream past that plus header room.
"""

import gzip
import tarfile
import unicodedata
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Final

from ssc_bundle.ignore import is_env_name
from ssc_bundle.limits import BundleMalformedError, BundleTooLargeError, Limits
from ssc_contracts.manifest import MANIFEST_FILE, MAX_MANIFEST_BYTES

MAX_META_BYTES: Final = 16 * 1024
"""Largest PAX or GNU long-name header accepted; tarfile reads one into memory whole."""
ENTRY_OVERHEAD_BYTES: Final = 4096
TAIL_BYTES: Final = 1024 * 1024
CHUNK_BYTES: Final = 64 * 1024
_META_TYPES: Final = frozenset(
    {
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.SOLARIS_XHDTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    }
)


@dataclass(frozen=True, slots=True)
class TarReport:
    manifest_text: bytes | None
    """``ssc.toml`` at the root, cut at one byte over the manifest limit; None when absent."""
    file_count: int
    unpacked_bytes: int


def inspect(source: IO[bytes], limits: Limits) -> TarReport:
    """Check every entry of the archive in ``source`` (read from its current position)."""
    manifest: bytes | None = None
    files = unpacked = 0
    for tar, member in _entries(source, limits):
        if member.isreg():
            files += 1
            unpacked += member.size
            if member.name == MANIFEST_FILE:
                manifest = _read(tar, member, MAX_MANIFEST_BYTES + 1)
    return TarReport(manifest, files, unpacked)


def iter_files(source: IO[bytes], limits: Limits, max_bytes: int) -> Iterator[tuple[str, bytes]]:
    """``(path, content)`` of every regular file of at most ``max_bytes``, checked as above."""
    for tar, member in _entries(source, limits):
        if member.isreg() and member.size <= max_bytes:
            yield member.name, _read(tar, member, member.size)


def _read(tar: tarfile.TarFile, member: tarfile.TarInfo, size: int) -> bytes:
    f = tar.extractfile(member)
    if f is None:
        raise BundleMalformedError("unreadable", "entry has no data", path=member.name)
    return f.read(size)


def _entries(
    source: IO[bytes], limits: Limits
) -> Iterator[tuple[tarfile.TarFile, tarfile.TarInfo]]:
    cap = limits.max_unpacked_bytes + limits.max_files * ENTRY_OVERHEAD_BYTES + TAIL_BYTES
    stream = _Decompressed(gzip.GzipFile(fileobj=source, mode="rb"), cap)
    seen: set[str] = set()
    unpacked = 0
    try:
        with tarfile.open(fileobj=stream, mode="r|", tarinfo=_BoundedInfo) as tar:
            for member in tar:
                if len(seen) >= limits.max_files:
                    raise BundleTooLargeError("files", f"more than {limits.max_files} entries")
                name = _checked_name(member)
                if member.isreg():
                    unpacked += member.size
                    if unpacked > limits.max_unpacked_bytes:
                        raise BundleTooLargeError(
                            "unpacked", f"more than {limits.max_unpacked_bytes} bytes of files"
                        )
                elif not member.isdir():
                    raise BundleMalformedError("not_regular", "links and devices", path=name)
                if name in seen:
                    raise BundleMalformedError("duplicate", "entry appears twice", path=name)
                seen.add(name)
                yield tar, member
            end = tar.offset
        stream.drain()
    except (tarfile.TarError, EOFError, zlib.error, OSError, RecursionError) as exc:
        raise BundleMalformedError("not_a_tar_gz", "not a readable tar.gz") from exc
    if stream.last_data_end > end:
        raise BundleMalformedError("trailing_data", "data after the end of the archive")


def _checked_name(member: tarfile.TarInfo) -> str:
    name = member.name
    parts = name.split("/")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise BundleMalformedError("bad_name", "name is not UTF-8", path=repr(name)) from None
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or "\0" in name
        or any(p in ("", ".", "..") for p in parts)
        or unicodedata.normalize("NFC", name) != name
    ):
        raise BundleMalformedError("bad_name", "not a plain relative NFC path", path=name)
    if any(is_env_name(p) for p in parts):
        raise BundleMalformedError("env_file", ".env files are never uploaded", path=name)
    return name


class _BoundedInfo(tarfile.TarInfo):
    """Refuses oversized PAX/GNU headers and sparse files before tarfile reads them.

    ``_proc_member`` is tarfile's documented extension point (see its source); typeshed omits it.
    """

    def _proc_member(self, tarfile_: tarfile.TarFile) -> tarfile.TarInfo:
        if self.size < 0:
            raise BundleMalformedError("bad_header", "negative size")
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise BundleMalformedError("not_regular", "sparse files", path=self.name)
        if self.type in _META_TYPES and self.size > MAX_META_BYTES:
            raise BundleMalformedError("bad_header", "extended header too large")
        return super()._proc_member(tarfile_)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]


class _Decompressed:
    """Read-only view of the decompressed stream: stops at ``cap`` bytes and remembers where the
    last non-zero byte ended, so trailing data after the archive can be refused."""

    def __init__(self, raw: gzip.GzipFile, cap: int) -> None:
        self._raw = raw
        self._cap = cap
        self._pos = 0
        self.last_data_end = 0

    def read(self, size: int = -1, /) -> bytes:
        want = CHUNK_BYTES if size < 0 else size
        data = self._raw.read(min(want, self._cap + 1 - self._pos))
        stripped = len(data.rstrip(b"\0"))
        if stripped:
            self.last_data_end = self._pos + stripped
        self._pos += len(data)
        if self._pos > self._cap:
            raise BundleTooLargeError("unpacked", f"decompressed stream is over {self._cap} bytes")
        return data

    def drain(self) -> None:
        while self.read(CHUNK_BYTES):
            pass

    def write(self, b: bytes, /) -> int:
        raise OSError("read-only")

    def tell(self) -> int:
        return self._pos

    def seek(self, pos: int, /) -> int:
        raise OSError("not seekable")

    def close(self) -> None:
        self._raw.close()
