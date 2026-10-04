"""A commit's source as a bundle: GitHub's tarball, unpacked without its top folder, then packed
by ``ssc_bundle.pack`` exactly as ``ssc deploy`` packs a folder (``.sscignore``, the default
exclusions, the deterministic digest).

The tarball is read as a stream and never extracted with ``tarfile``'s own extraction: each
entry's path is checked (no absolute path, no ``.`` or ``..`` part, one top folder), only
directories and regular files are written, a link or special file refuses the source, and the
file count and unpacked bytes stop at the bundle limits. The download stops at
``max_unpacked_bytes``.
"""

import asyncio
import os
import shutil
import tarfile
import zlib
from pathlib import Path
from typing import Final

from ssc_bundle.limits import BundleMalformedError, BundleTooLargeError, Limits
from ssc_bundle.pack import PackedBundle, pack
from ssc_control.github.client import GitHubApp, RepoRef

CHUNK_BYTES: Final = 64 * 1024
TARBALL: Final = "source.tar.gz"
TREE: Final = "tree"
BUNDLE: Final = "bundle.tar.gz"


async def fetch_bundle(
    github: GitHubApp, repo: RepoRef, sha: str, workdir: Path, limits: Limits
) -> PackedBundle:
    """Download ``sha`` of ``repo`` into ``workdir`` and pack it; raises ``BundleError`` for a
    source that cannot be a bundle and ``GitHubError`` when GitHub does not hand it over."""
    tarball = workdir / TARBALL
    with tarball.open("wb") as out:
        await github.download_tarball(repo, sha, out, limits.max_unpacked_bytes)
    return await asyncio.to_thread(repack, tarball, workdir, limits)


def repack(tarball: Path, workdir: Path, limits: Limits) -> PackedBundle:
    tree = workdir / TREE
    tree.mkdir()
    try:
        unpack(tarball, tree, limits)
        return pack(tree, workdir / BUNDLE, limits=limits)
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def _relative(name: str, top: list[str]) -> str | None:
    """``name`` without the top folder, or None for the top folder itself."""
    if name.startswith("/"):
        raise BundleMalformedError("absolute_path", "absolute paths are not unpacked", path=name)
    parts = name.split("/")
    if any(p in {"", ".", ".."} for p in parts):
        raise BundleMalformedError("unsafe_path", "unsafe path in the source", path=name)
    if not top:
        top.append(parts[0])
    elif parts[0] != top[0]:
        raise BundleMalformedError("two_roots", "the source has more than one top folder")
    rest = parts[1:]
    return "/".join(rest) if rest else None


def unpack(tarball: Path, dest: Path, limits: Limits) -> None:
    files = unpacked = 0
    top: list[str] = []
    try:
        with tarfile.open(tarball, mode="r|gz") as tar:
            for member in tar:
                rel = _relative(member.name, top)
                if rel is None:
                    continue
                target = dest / rel
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if member.issym() or member.islnk():
                    raise BundleMalformedError("link", "links are not packed", path=rel)
                if not member.isfile():
                    raise BundleMalformedError("special_file", "only regular files", path=rel)
                files += 1
                unpacked += member.size
                if files > limits.max_files:
                    raise BundleTooLargeError("files", f"more than {limits.max_files} files")
                if unpacked > limits.max_unpacked_bytes:
                    raise BundleTooLargeError(
                        "unpacked", f"more than {limits.max_unpacked_bytes} bytes of files"
                    )
                _write(tar, member, target, rel)
    except (tarfile.TarError, EOFError, zlib.error) as exc:
        raise BundleMalformedError("not_tar_gz", "the source is not a tar.gz") from exc


def _write(tar: tarfile.TarFile, member: tarfile.TarInfo, target: Path, rel: str) -> None:
    source = tar.extractfile(member)
    if source is None:
        raise BundleMalformedError("special_file", "only regular files", path=rel)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with source, target.open("xb") as out:
            shutil.copyfileobj(source, out, CHUNK_BYTES)
        os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
    except FileExistsError:
        raise BundleMalformedError(
            "case_collision", "two entries share a name, in case or form", path=rel
        ) from None
    except OSError as exc:
        raise BundleMalformedError("unwritable", "the entry cannot be unpacked", path=rel) from exc
