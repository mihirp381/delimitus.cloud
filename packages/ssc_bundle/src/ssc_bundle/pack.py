"""A deterministic ``.tar.gz`` of an app folder: the same files give the same digest.

POSIX relative NFC paths sorted by their UTF-8 bytes, regular files only, modes 0644 or 0755,
uid and gid 0 with empty names, mtime 0, PAX format, gzip level 6 with mtime 0 and no file name.
Symlinks, git submodules, case collisions, special files and anything over a cap are refused.
"""

import gzip
import hashlib
import os
import re
import stat
import tarfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from ssc_bundle.ignore import Ignore
from ssc_bundle.limits import DEFAULT_LIMITS, BundleError, BundleTooLargeError, Limits

GZIP_LEVEL: Final = 6
GITMODULES: Final = ".gitmodules"
_SUBMODULE_PATH = re.compile(r"^\s*path\s*=\s*(.+?)\s*$")


@dataclass(frozen=True, slots=True)
class PackedBundle:
    path: Path
    digest: str
    size: int
    file_count: int
    unpacked_bytes: int
    excluded: Counter[str]
    """Left-out entries per reason (a pattern, or ``.sscignore``)."""


@dataclass(frozen=True, slots=True)
class _Entry:
    arcname: str
    source: Path
    size: int
    mode: int


def pack(root: Path, dest: Path, *, limits: Limits = DEFAULT_LIMITS) -> PackedBundle:
    """Pack ``root`` into ``dest``; raises ``BundleError`` and leaves no ``dest`` behind."""
    ignore = Ignore.load(root)
    excluded: Counter[str] = Counter()
    entries = _collect(root, ignore, excluded, limits)
    try:
        with dest.open("wb") as raw:
            out = _Capped(raw, limits.max_bytes)
            _write(entries, out)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return PackedBundle(
        path=dest,
        digest="sha256:" + out.sha256.hexdigest(),
        size=out.size,
        file_count=len(entries),
        unpacked_bytes=sum(e.size for e in entries),
        excluded=excluded,
    )


def _collect(root: Path, ignore: Ignore, excluded: Counter[str], limits: Limits) -> list[_Entry]:
    _refuse_declared_submodules(root, ignore)
    entries: list[_Entry] = []
    folded: dict[str, str] = {}
    unpacked = 0
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        with os.scandir(root / rel_dir if rel_dir else root) as it:
            children = sorted(it, key=lambda e: e.name)
        for child in children:
            rel = f"{rel_dir}/{child.name}" if rel_dir else child.name
            _check_utf8(rel)
            is_dir = child.is_dir(follow_symlinks=False)
            reason = ignore.reason(rel, is_dir=is_dir)
            if reason is not None:
                excluded[reason] += 1
                continue
            if child.is_symlink():
                raise BundleError("symlink", "symlinks are not packed", path=rel)
            if is_dir:
                if os.path.lexists(os.path.join(child.path, ".git")):
                    raise BundleError("submodule", "git submodules are not packed", path=rel)
                stack.append(rel)
                continue
            st = child.stat(follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                raise BundleError("special_file", "only regular files are packed", path=rel)
            arcname = unicodedata.normalize("NFC", rel)
            other = folded.setdefault(arcname.casefold(), rel)
            if other != rel:
                raise BundleError(
                    "case_collision", f"differs from {other} only in case or form", path=rel
                )
            unpacked += st.st_size
            entries.append(_Entry(arcname, Path(child.path), st.st_size, _mode(st.st_mode)))
            if len(entries) > limits.max_files:
                raise BundleTooLargeError("files", f"more than {limits.max_files} files")
            if unpacked > limits.max_unpacked_bytes:
                raise BundleTooLargeError(
                    "unpacked", f"more than {limits.max_unpacked_bytes} bytes of files"
                )
    entries.sort(key=lambda e: e.arcname.encode())
    return entries


def _refuse_declared_submodules(root: Path, ignore: Ignore) -> None:
    path = root / GITMODULES
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _SUBMODULE_PATH.match(line)
        if m and ignore.reason(m.group(1).strip("/"), is_dir=True) is None:
            raise BundleError("submodule", "git submodules are not packed", path=m.group(1))


def _check_utf8(rel: str) -> None:
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:
        raise BundleError("not_utf8", "file names must be UTF-8", path=repr(rel)) from None


def _mode(st_mode: int) -> int:
    return 0o755 if st_mode & 0o111 else 0o644


def _write(entries: list[_Entry], out: _Capped) -> None:
    with (
        gzip.GzipFile(filename="", mode="wb", compresslevel=GZIP_LEVEL, fileobj=out, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for e in entries:
            info = tarfile.TarInfo(e.arcname)
            info.size = e.size
            info.mode = e.mode
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            try:
                with e.source.open("rb") as f:
                    tar.addfile(info, f)
            except OSError as exc:
                raise BundleError("unreadable", str(exc), path=e.arcname) from exc


class _Capped:
    """A write-only file that hashes, counts and refuses to grow past ``limit`` bytes."""

    def __init__(self, raw: BinaryIO, limit: int) -> None:
        self._raw = raw
        self._limit = limit
        self.size = 0
        self.sha256 = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.size += len(data)
        if self.size > self._limit:
            raise BundleTooLargeError("bytes", f"compressed bundle is over {self._limit} bytes")
        self.sha256.update(data)
        return self._raw.write(data)

    def flush(self) -> None:
        self._raw.flush()
