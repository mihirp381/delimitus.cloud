"""Filesystem store and URL signer details the shared contract does not cover."""

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from ssc_shared.blobstore import BlobCorruptError, BlobNotFoundError
from ssc_shared.blobstore_fs import (
    HEADER_SIZE,
    FsBlobStore,
    GetGrant,
    PutGrant,
    SignedUrlError,
    UrlSigner,
)

BASE = "http://blobs.test/v1/blobs"
KEY_1 = ("one-" + "test-" + "signing-" + "key-").encode() * 2
KEY_2 = ("two-" + "test-" + "signing-" + "key-").encode() * 2
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now_value = now

    def now(self) -> datetime:
        return self.now_value


def store_at(root: Path, clock: Clock | None = None) -> FsBlobStore:
    clock = clock or Clock()
    signer = UrlSigner({"k1": KEY_1}, active="k1", clock=clock)
    return FsBlobStore(root, signer=signer, base_url=BASE + "/", clock=clock)


def query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


async def read_all(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([c async for c in chunks])


async def test_one_file_per_object_with_a_fixed_header(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    await store.put("a", b"top", content_type="text/plain")
    await store.put("a/b", b"nested")
    raw = (tmp_path / "a/b@blob").read_bytes()
    header = json.loads(raw[:HEADER_SIZE])
    assert raw[HEADER_SIZE:] == b"nested"
    assert raw[HEADER_SIZE - 1 : HEADER_SIZE] == b"\n"
    assert header == {
        "v": 1,
        "size": 6,
        "sha256": hashlib.sha256(b"nested").hexdigest(),
        "content_type": "application/octet-stream",
        "created_at": T0.isoformat(),
    }
    assert await read_all(store.get("a")) == b"top"
    assert names(tmp_path) == ["a", "a@blob"]


async def test_temp_files_are_removed_on_failure_and_never_listed(tmp_path: Path) -> None:
    store = store_at(tmp_path)

    async def broken() -> AsyncIterator[bytes]:
        yield b"partial"
        raise ConnectionError

    with pytest.raises(ConnectionError):
        await store.put("c/k", broken())
    assert names(tmp_path / "c") == []
    (tmp_path / "c" / ".k@blob.0123.tmp").write_bytes(b"junk")
    (tmp_path / "c" / "notes.txt").write_bytes(b"junk")
    assert [i.key async for i in store.list()] == []
    assert await store.stat("c/k") is None


async def test_a_damaged_header_or_body_is_reported(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    await store.put("c/k", b"x" * 100)
    path = tmp_path / "c/k@blob"
    raw = path.read_bytes()
    path.write_bytes(raw[:-1] + b"y")
    with pytest.raises(BlobCorruptError):
        await read_all(store.get("c/k"))
    path.write_bytes(raw[:-10])
    with pytest.raises(BlobCorruptError):
        await read_all(store.get("c/k"))
    path.write_bytes(b"[" + raw[1:])
    with pytest.raises(BlobCorruptError):
        await store.stat("c/k")


async def test_signed_url_shape(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    digest = hashlib.sha256(b"abc").hexdigest()
    put = await store.signed_url("c/k", method="PUT", content_length=3, sha256=digest)
    assert put.url.startswith(BASE + "/c/k?")
    assert put.headers == {"content-length": "3"}
    assert put.expires_at == T0 + timedelta(minutes=10)
    params = query(put.url)
    assert set(params) == {"m", "exp", "len", "sha", "kid", "sig"}
    assert (params["m"], params["len"], params["sha"], params["kid"]) == ("PUT", "3", digest, "k1")
    get = await store.signed_url("c/k", method="GET", expires_in=timedelta(seconds=90))
    assert set(query(get.url)) == {"m", "exp", "kid", "sig"}
    assert get.headers == {}
    assert get.expires_at == T0 + timedelta(seconds=90)


async def test_signed_get_of_a_missing_object(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    get = await store.signed_url("c/none", method="GET")
    with pytest.raises(BlobNotFoundError):
        await store.open_get("c/none", query(get.url))


def test_verify_returns_the_grant() -> None:
    clock = Clock()
    signer = UrlSigner({"k1": KEY_1}, active="k1", clock=clock)
    params, expires_at = signer.sign("PUT", "c/k", content_length=7)
    assert signer.verify("PUT", "c/k", params) == PutGrant("c/k", 7, None, expires_at)
    params, expires_at = signer.sign("GET", "c/k")
    assert signer.verify("GET", "c/k", params) == GetGrant("c/k", expires_at)


def test_expiry_is_exclusive_and_far_expiries_are_refused() -> None:
    clock = Clock()
    signer = UrlSigner({"k1": KEY_1}, active="k1", clock=clock)
    params, _ = signer.sign("GET", "c/k", expires_in=timedelta(seconds=30))
    clock.now_value = T0 + timedelta(seconds=29)
    signer.verify("GET", "c/k", params)
    clock.now_value = T0 + timedelta(seconds=30)
    with pytest.raises(SignedUrlError) as err:
        signer.verify("GET", "c/k", params)
    assert err.value.reason == "expired"
    ahead = UrlSigner({"k1": KEY_1}, active="k1", clock=Clock(T0 + timedelta(minutes=15)))
    far, _ = ahead.sign("GET", "c/k")
    clock.now_value = T0
    with pytest.raises(SignedUrlError) as err:
        signer.verify("GET", "c/k", far)
    assert err.value.reason == "too_long"


def test_key_rotation() -> None:
    clock = Clock()
    old = UrlSigner({"k1": KEY_1}, active="k1", clock=clock)
    params, _ = old.sign("GET", "c/k")
    both = UrlSigner({"k1": KEY_1, "k2": KEY_2}, active="k2", clock=clock)
    both.verify("GET", "c/k", params)
    new_params, _ = both.sign("GET", "c/k")
    assert new_params["kid"] == "k2"
    new = UrlSigner({"k2": KEY_2}, active="k2", clock=clock)
    new.verify("GET", "c/k", new_params)
    with pytest.raises(SignedUrlError) as err:
        new.verify("GET", "c/k", params)
    assert err.value.reason == "unknown_key"
    forged = {**params, "kid": "k2"}
    with pytest.raises(SignedUrlError) as err:
        new.verify("GET", "c/k", forged)
    assert err.value.reason == "bad_signature"


@pytest.mark.parametrize(
    "keys",
    [{"k1": KEY_1[:31]}, {"K1": KEY_1}, {"": KEY_1}, {"k" * 33: KEY_1}],
)
def test_weak_or_misnamed_signing_keys_are_refused(keys: dict[str, bytes]) -> None:
    with pytest.raises(ValueError, match="signing key"):
        UrlSigner(keys, active=next(iter(keys)), clock=Clock())


def test_the_active_key_must_exist() -> None:
    with pytest.raises(ValueError, match="active"):
        UrlSigner({"k1": KEY_1}, active="k2", clock=Clock())


def _malformed(put: dict[str, str], get: dict[str, str]) -> list[tuple[str, dict[str, str]]]:
    drop = lambda d, k: {n: v for n, v in d.items() if n != k}  # noqa: E731
    return [
        ("PUT", drop(put, "sig")),
        ("PUT", drop(put, "exp")),
        ("PUT", drop(put, "kid")),
        ("PUT", drop(put, "m")),
        ("PUT", drop(put, "len")),
        ("PUT", {**put, "extra": "1"}),
        ("PUT", {**put, "exp": "soon"}),
        ("PUT", {**put, "exp": "1" * 16}),
        ("PUT", {**put, "len": "-1"}),
        ("PUT", {**put, "len": ""}),
        ("PUT", {**put, "sha": "A" * 64}),
        ("PUT", {**put, "sig": put["sig"][:-1]}),
        ("PUT", {**put, "sig": put["sig"][:-1] + "="}),
        ("PUT", {**put, "kid": "K1"}),
        ("PUT", {**put, "m": "POST"}),
        ("GET", {**get, "len": "3"}),
        ("GET", {**get, "sha": "0" * 64}),
    ]


def test_malformed_parameters_are_refused_before_any_mac() -> None:
    signer = UrlSigner({"k1": KEY_1}, active="k1", clock=Clock())
    put, _ = signer.sign("PUT", "c/k", content_length=3, sha256="0" * 64)
    get, _ = signer.sign("GET", "c/k")
    for method, params in _malformed(put, get):
        with pytest.raises(SignedUrlError) as err:
            signer.verify(method, "c/k", params)  # type: ignore[arg-type]
        assert err.value.reason == "malformed", params


def test_wrong_method_and_wrong_key() -> None:
    signer = UrlSigner({"k1": KEY_1}, active="k1", clock=Clock())
    get, _ = signer.sign("GET", "c/k")
    with pytest.raises(SignedUrlError) as err:
        signer.verify("PUT", "c/k", get)
    assert err.value.reason == "wrong_method"
    with pytest.raises(SignedUrlError) as err:
        signer.verify("GET", "c/other", get)
    assert err.value.reason == "bad_signature"


async def test_accept_put_refuses_a_get_url(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    get = await store.signed_url("c/k", method="GET")
    with pytest.raises(SignedUrlError):
        await store.accept_put("c/k", query(get.url), b"x")
    assert await store.stat("c/k") is None
