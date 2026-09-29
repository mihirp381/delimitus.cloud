"""SSC-014: packing is deterministic and never carries what must stay local."""

import hashlib
import os
import shutil
import tarfile
import unicodedata
from pathlib import Path

import pytest

from ssc_bundle.limits import BundleError, BundleTooLargeError, Limits
from ssc_bundle.pack import pack


def write(root: Path, rel: str, data: str | bytes = "x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)
    return path


def names(archive: Path) -> list[str]:
    with tarfile.open(archive) as tar:
        return tar.getnames()


def test_left_out_files_never_reach_the_tar(tmp_path: Path) -> None:
    src = tmp_path / "app"
    for rel in (
        "app.py",
        ".env",
        ".env.local",
        "config/.ENV.production",
        "node_modules/left-pad/index.js",
        ".git/config",
        ".venv/bin/python",
        "pkg/__pycache__/m.cpython-314.pyc",
        ".DS_Store",
        "dist/index.html",
        "build/app.js",
        "logs/today.log",
        "keep.log",
        "noise.log",
        "private/key.txt",
    ):
        write(src, rel)
    write(src, ".sscignore", "*.log\n!keep.log\nprivate/\n")
    packed = pack(src, tmp_path / "b.tar.gz")
    assert names(packed.path) == [
        ".sscignore",
        "app.py",
        "build/app.js",
        "dist/index.html",
        "keep.log",
    ]
    assert packed.file_count == 5
    assert packed.excluded == {
        ".env": 3,
        "node_modules/": 1,
        ".git": 1,
        ".venv/": 1,
        "__pycache__/": 1,
        ".DS_Store": 1,
        ".sscignore": 3,
    }


def test_a_worktree_git_file_is_left_out(tmp_path: Path) -> None:
    src = tmp_path / "a"
    write(src, ".git", "gitdir: /elsewhere/.git/worktrees/a")
    write(src, "app.py")
    packed = pack(src, tmp_path / "b.tar.gz")
    assert names(packed.path) == ["app.py"]
    assert packed.excluded == {".git": 1}


def test_packing_is_deterministic(tmp_path: Path) -> None:
    src = tmp_path / "a"
    write(src, "main.py", "print('hi')\n")
    write(src, "static/logo.svg", "<svg/>")
    run = write(src, "run.sh", "#!/bin/sh\n")
    run.chmod(0o775)
    first = pack(src, tmp_path / "1.tar.gz")
    for path in src.rglob("*"):
        os.utime(path, (1_000_000_000, 1_000_000_000))
    second = pack(src, tmp_path / "2.tar.gz")
    copy = shutil.copytree(src, tmp_path / "elsewhere" / "b")
    third = pack(copy, tmp_path / "3.tar.gz")
    assert first.digest == second.digest == third.digest
    assert first.digest == "sha256:" + hashlib.sha256(first.path.read_bytes()).hexdigest()
    assert first.size == first.path.stat().st_size


def test_headers_are_normalised_and_sorted_by_bytes(tmp_path: Path) -> None:
    src = tmp_path / "a"
    nfd = unicodedata.normalize("NFD", "café.txt")
    write(src, nfd)
    write(src, "Z.txt")
    write(src, "a/b.txt")
    write(src, "a-b.txt")
    tool = write(src, "tool")
    tool.chmod(0o700)
    (src / "Z.txt").chmod(0o600)
    packed = pack(src, tmp_path / "b.tar.gz")
    with tarfile.open(packed.path) as tar:
        members = tar.getmembers()
    got = [m.name for m in members]
    assert got == sorted(got, key=str.encode)
    assert unicodedata.normalize("NFC", "café.txt") in got
    for m in members:
        assert (m.uid, m.gid, m.uname, m.gname, m.mtime) == (0, 0, "", "", 0)
        assert m.isreg()
    modes = {m.name: m.mode for m in members}
    assert modes["tool"] == 0o755
    assert modes["Z.txt"] == 0o644


def test_symlinks_are_refused(tmp_path: Path) -> None:
    src = tmp_path / "a"
    write(src, "real.txt")
    (src / "link.txt").symlink_to(src / "real.txt")
    with pytest.raises(BundleError) as e:
        pack(src, tmp_path / "b.tar.gz")
    assert (e.value.reason, e.value.path) == ("symlink", "link.txt")
    assert not (tmp_path / "b.tar.gz").exists()


def test_a_symlink_inside_an_excluded_folder_is_ignored(tmp_path: Path) -> None:
    src = tmp_path / "a"
    write(src, "app.py")
    (src / ".venv" / "bin").mkdir(parents=True)
    (src / ".venv" / "bin" / "python").symlink_to("/usr/bin/python3")
    assert names(pack(src, tmp_path / "b.tar.gz").path) == ["app.py"]


def test_submodules_are_refused(tmp_path: Path) -> None:
    src = tmp_path / "a"
    write(src, "lib/vendor/.git", "gitdir: ../../.git/modules/vendor")
    with pytest.raises(BundleError) as e:
        pack(src, tmp_path / "b.tar.gz")
    assert (e.value.reason, e.value.path) == ("submodule", "lib/vendor")


def test_declared_submodules_are_refused_unless_ignored(tmp_path: Path) -> None:
    src = tmp_path / "a"
    write(src, ".gitmodules", '[submodule "theme"]\n\tpath = theme\n\turl = ../theme.git\n')
    (src / "theme").mkdir()
    with pytest.raises(BundleError) as e:
        pack(src, tmp_path / "b.tar.gz")
    assert e.value.reason == "submodule"
    write(src, ".sscignore", "theme/\n.gitmodules\n")
    assert names(pack(src, tmp_path / "b.tar.gz").path) == [".sscignore"]


def test_case_collisions_are_refused(tmp_path: Path) -> None:
    src = tmp_path / "a"
    write(src, "README.md")
    write(src, "readme.md")
    if len(os.listdir(src)) != 2:
        pytest.skip("this filesystem is case-insensitive")
    with pytest.raises(BundleError) as e:
        pack(src, tmp_path / "b.tar.gz")
    assert e.value.reason == "case_collision"


def test_special_files_are_refused(tmp_path: Path) -> None:
    src = tmp_path / "a"
    src.mkdir()
    os.mkfifo(src / "pipe")
    with pytest.raises(BundleError) as e:
        pack(src, tmp_path / "b.tar.gz")
    assert e.value.reason == "special_file"


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        (Limits(max_files=2), "files"),
        (Limits(max_unpacked_bytes=2_000), "unpacked"),
        (Limits(max_bytes=1_000), "bytes"),
    ],
)
def test_oversize_is_refused(tmp_path: Path, limits: Limits, reason: str) -> None:
    src = tmp_path / "a"
    for i in range(3):
        write(src, f"f{i}.bin", os.urandom(1_000))
    with pytest.raises(BundleTooLargeError) as e:
        pack(src, tmp_path / "b.tar.gz", limits=limits)
    assert e.value.reason == reason
    assert not (tmp_path / "b.tar.gz").exists()
