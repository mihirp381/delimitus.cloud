"""SSC-014: the server refuses archives the client would never produce."""

import gzip
import io
import tarfile
import unicodedata
from pathlib import Path

import pytest

from ssc_bundle.limits import BundleMalformedError, BundleTooLargeError, Limits
from ssc_bundle.pack import pack
from ssc_bundle.tarcheck import MAX_META_BYTES, inspect, iter_files
from ssc_contracts.manifest import MAX_MANIFEST_BYTES

LIMITS = Limits()


def member(
    name: str, data: bytes = b"x", kind: bytes = tarfile.REGTYPE
) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    if kind == tarfile.SYMTYPE or kind == tarfile.LNKTYPE:
        info.linkname = "app.py"
    return info, data


def raw_tar(*entries: tuple[tarfile.TarInfo, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in entries:
            tar.addfile(info, io.BytesIO(data) if info.isreg() else None)
    return buf.getvalue()


def gz(data: bytes) -> io.BytesIO:
    return io.BytesIO(gzip.compress(data, mtime=0))


def refused(source: io.BytesIO, limits: Limits = LIMITS) -> str:
    with pytest.raises((BundleMalformedError, BundleTooLargeError)) as e:
        inspect(source, limits)
    return e.value.reason


def test_a_packed_bundle_passes_and_yields_its_manifest(tmp_path: Path) -> None:
    src = tmp_path / "a"
    (src / "web").mkdir(parents=True)
    (src / "ssc.toml").write_text('schema = "ssc/v1"\n')
    (src / "web" / "index.html").write_text("<p>hi</p>")
    packed = pack(src, tmp_path / "b.tar.gz")
    with packed.path.open("rb") as f:
        report = inspect(f, LIMITS)
        f.seek(0)
        files = dict(iter_files(f, LIMITS, 1024))
    assert report.manifest_text == b'schema = "ssc/v1"\n'
    assert (report.file_count, report.unpacked_bytes) == (2, len(b'schema = "ssc/v1"\n') + 9)
    assert files == {"ssc.toml": b'schema = "ssc/v1"\n', "web/index.html": b"<p>hi</p>"}


def test_no_manifest_is_none() -> None:
    assert inspect(gz(raw_tar(member("app.py"))), LIMITS).manifest_text is None


def test_an_oversized_manifest_is_cut_one_byte_over_the_limit() -> None:
    big = b"#" * (MAX_MANIFEST_BYTES + 10)
    text = inspect(gz(raw_tar(member("ssc.toml", big))), LIMITS).manifest_text
    assert text is not None and len(text) == MAX_MANIFEST_BYTES + 1


@pytest.mark.parametrize("name", [".env", "config/.env.local", ".ENV", ".env/x"])
def test_env_files_are_refused(name: str) -> None:
    assert refused(gz(raw_tar(member("app.py"), member(name)))) == "env_file"


@pytest.mark.parametrize(
    "name",
    [
        "/etc/passwd",
        "../up.txt",
        "a/../../up.txt",
        "a/./b",
        "a//b",
        "a\\b",
        unicodedata.normalize("NFD", "café"),
    ],
)
def test_unsafe_names_are_refused(name: str) -> None:
    assert refused(gz(raw_tar(member(name)))) == "bad_name"


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE]
)
def test_links_and_devices_are_refused(kind: bytes) -> None:
    assert refused(gz(raw_tar(member("app.py"), member("odd", b"", kind)))) == "not_regular"


def test_duplicates_are_refused() -> None:
    assert refused(gz(raw_tar(member("app.py"), member("app.py", b"y")))) == "duplicate"


def test_entries_hidden_after_the_end_of_the_archive_are_refused() -> None:
    first = raw_tar(member("app.py"))
    assert refused(gz(first + raw_tar(member(".env", b"TOKEN=1")))) == "trailing_data"
    assert inspect(gz(first + b"\0" * 4096), LIMITS).file_count == 1


def test_entries_hidden_behind_a_damaged_header_are_refused() -> None:
    good = raw_tar(member("app.py"))[:1024]
    hidden = raw_tar(member("secret.txt", b"TOKEN=1"))
    assert refused(gz(good + b"\x01" * 512 + hidden)) == "trailing_data"


def test_not_a_tar_gz() -> None:
    assert refused(io.BytesIO(b"plain text")) == "not_a_tar_gz"
    whole = gzip.compress(raw_tar(member("app.py")), mtime=0)
    assert refused(io.BytesIO(whole[:-12])) == "not_a_tar_gz"


def test_caps_are_enforced() -> None:
    many = gz(raw_tar(*(member(f"f{i}") for i in range(4))))
    assert refused(many, Limits(max_files=3)) == "files"
    big = gz(raw_tar(member("zeros.bin", b"\0" * 5000)))
    assert refused(big, Limits(max_unpacked_bytes=4096)) == "unpacked"


def test_a_decompression_bomb_stops_at_the_stream_cap() -> None:
    bomb = io.BytesIO(gzip.compress(b"\0" * (3 * 1024 * 1024), mtime=0))
    limits = Limits(max_unpacked_bytes=1024, max_files=1)
    assert refused(bomb, limits) == "unpacked"


def test_oversized_extended_headers_are_refused() -> None:
    long_name = "d/" * (MAX_META_BYTES // 2) + "f"
    assert refused(gz(raw_tar(member(long_name)))) == "bad_header"
