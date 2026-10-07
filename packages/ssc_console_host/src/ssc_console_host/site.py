"""The built console, read once at start: every file of ``dist/`` by its URL path, in memory.

A request is answered from this table only, by exact key, so nothing outside ``dist/`` (and
nothing written there after start) can ever be served. Symbolic links are not followed.
"""

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Final

INDEX: Final = "/index.html"
TEXT_TYPES: Final = frozenset(
    {"application/javascript", "application/json", "image/svg+xml", "text/javascript"}
)


class SiteError(ValueError):
    """``dist/`` is missing, or has no ``index.html``."""


@dataclass(frozen=True, slots=True)
class File:
    body: bytes
    content_type: str
    etag: str


@dataclass(frozen=True, slots=True)
class Site:
    files: dict[str, File]

    @property
    def index(self) -> File:
        return self.files[INDEX]


def content_type(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name, strict=True)
    kind = guessed or "application/octet-stream"
    if kind.startswith("text/") or kind in TEXT_TYPES:
        return f"{kind}; charset=utf-8"
    return kind


def file_of(name: str, body: bytes) -> File:
    digest = hashlib.sha256(body).hexdigest()[:32]
    return File(body=body, content_type=content_type(name), etag=f'"{digest}"')


def load_site(dist: Path) -> Site:
    if not dist.is_dir():
        raise SiteError(f"{dist} is not a directory; build the console first")
    files: dict[str, File] = {}
    for path in sorted(dist.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        key = "/" + path.relative_to(dist).as_posix()
        files[key] = file_of(path.name, path.read_bytes())
    if INDEX not in files:
        raise SiteError(f"{dist} has no index.html")
    return Site(files)
