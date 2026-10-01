"""``BlobStore`` contract. The filesystem store and the bucket store pass all of it.
``BlobStoreCoreContract`` needs only the ``blob_store`` fixture. A store whose PUT URLs need the
sha256 (the bucket store records it as object metadata) sets ``put_needs_sha256``.

Subclass ``BlobStoreContract`` as a ``Test*`` class and provide three fixtures: ``blob_store``,
``fetch`` (sends a request to a signed URL, real HTTP for a cloud store) and ``clock`` (the clock
the store signs with). Refusals through a URL are checked as "any 4xx and nothing stored", since
buckets differ in which 4xx they send.
"""

import hashlib
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import parse_qsl, urlencode

import pytest

from ssc_shared.blobstore import (
    MAX_URL_LIFETIME,
    BlobKeyError,
    BlobMismatchError,
    BlobNotFoundError,
    BlobStore,
    BlobTooLargeError,
    SignedUrl,
)

BAD_KEYS = (
    "",
    "/a",
    "a/",
    "a//b",
    "../a",
    "a/../b",
    "a..b",
    "A",
    ".hidden",
    "a b",
    "\xe9",
    "a" * 513,
)


@dataclass(frozen=True, slots=True)
class FetchResult:
    status: int
    body: bytes


class Fetch(Protocol):
    async def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> FetchResult: ...


class SettableClock(Protocol):
    def now(self) -> datetime: ...
    def set(self, when: datetime) -> None: ...


class ManualClock:
    """A clock tests move by hand."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime.now(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def read_all(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([c async for c in chunks])


async def chunked(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def send(
    fetch: Fetch,
    signed: SignedUrl,
    body: bytes | None = None,
    *,
    method: str | None = None,
    url: str | None = None,
) -> FetchResult:
    """Use a signed URL as a client would; the body's real length replaces the signed one."""
    headers = dict(signed.headers)
    if body is not None:
        headers["content-length"] = str(len(body))
    return await fetch(method or signed.method, url or signed.url, headers, body)


def refused(result: FetchResult) -> bool:
    return 400 <= result.status < 500


def with_param(url: str, name: str, value: str) -> str:
    base, _, query = url.partition("?")
    pairs = [(k, value if k == name else v) for k, v in parse_qsl(query, keep_blank_values=True)]
    return f"{base}?{urlencode(pairs)}"


def altered(value: str) -> str:
    if value.isdigit():
        return str(int(value) + 1)
    return value[:-1] + ("b" if value[-1:] != "b" else "c")


class BlobStoreCoreContract:
    """Behaviour every ``BlobStore`` shows without signed URLs."""

    async def test_bytes_round_trip(self, blob_store: BlobStore) -> None:
        info = await blob_store.put("c/one", b"hello", content_type="text/plain")
        assert (info.key, info.size, info.sha256) == ("c/one", 5, sha(b"hello"))
        assert await read_all(blob_store.get("c/one")) == b"hello"
        stat = await blob_store.stat("c/one")
        assert stat is not None
        assert (stat.size, stat.sha256, stat.content_type) == (5, sha(b"hello"), "text/plain")

    async def test_streamed_round_trip(self, blob_store: BlobStore) -> None:
        parts = (b"a" * 70_000, b"", b"b" * 3, b"c" * 65_536)
        info = await blob_store.put("c/big", chunked(*parts), size=sum(map(len, parts)))
        assert info.sha256 == sha(b"".join(parts))
        assert await read_all(blob_store.get("c/big")) == b"".join(parts)

    async def test_empty_object(self, blob_store: BlobStore) -> None:
        await blob_store.put("c/empty", b"", size=0, sha256=sha(b""))
        assert await read_all(blob_store.get("c/empty")) == b""

    async def test_missing_key(self, blob_store: BlobStore) -> None:
        assert await blob_store.stat("c/none") is None
        with pytest.raises(BlobNotFoundError):
            await read_all(blob_store.get("c/none"))

    async def test_list_isolates_prefixes_and_sorts(self, blob_store: BlobStore) -> None:
        for key in ("a/2", "ab/1", "a/b/3", "a/1", "a", "b"):
            await blob_store.put(key, key.encode())

        async def keys(prefix: str) -> list[str]:
            return [i.key async for i in blob_store.list(prefix)]

        assert await keys("a/") == ["a/1", "a/2", "a/b/3"]
        assert await keys("ab/") == ["ab/1"]
        assert await keys("a/b") == ["a/b/3"]
        assert await keys("a") == ["a", "a/1", "a/2", "a/b/3", "ab/1"]
        assert await keys("") == ["a", "a/1", "a/2", "a/b/3", "ab/1", "b"]
        assert await keys("zz/") == []

    async def test_bad_keys_are_refused(self, blob_store: BlobStore) -> None:
        for key in BAD_KEYS:
            with pytest.raises(BlobKeyError):
                await blob_store.put(key, b"x")
            with pytest.raises(BlobKeyError):
                await blob_store.stat(key)
            with pytest.raises(BlobKeyError):
                await read_all(blob_store.get(key))
            with pytest.raises(BlobKeyError):
                await blob_store.signed_url(key, method="GET")
            with pytest.raises(BlobKeyError):
                await blob_store.delete(key)
        with pytest.raises(BlobKeyError):
            [i async for i in blob_store.list("../")]

    async def test_delete_removes_only_that_key(self, blob_store: BlobStore) -> None:
        for key in ("d/a", "d/a/b", "d/ab"):
            await blob_store.put(key, key.encode())
        assert await blob_store.delete("d/a") is True
        assert await blob_store.stat("d/a") is None
        with pytest.raises(BlobNotFoundError):
            await read_all(blob_store.get("d/a"))
        assert [i.key async for i in blob_store.list("d/")] == ["d/a/b", "d/ab"]
        assert await read_all(blob_store.get("d/a/b")) == b"d/a/b"
        # Deleting again, or a key never written, is not an error.
        assert await blob_store.delete("d/a") is False
        assert await blob_store.delete("d/none") is False
        await blob_store.put("d/a", b"again")
        assert await read_all(blob_store.get("d/a")) == b"again"

    async def test_overwrite_replaces(self, blob_store: BlobStore) -> None:
        await blob_store.put("c/k", b"first")
        await blob_store.put("c/k", b"second!")
        assert await read_all(blob_store.get("c/k")) == b"second!"

    async def test_a_body_that_differs_stores_nothing(self, blob_store: BlobStore) -> None:
        await blob_store.put("c/k", b"old")
        with pytest.raises(BlobTooLargeError):
            await blob_store.put("c/k", b"new!", size=3)
        with pytest.raises(BlobMismatchError):
            await blob_store.put("c/k", chunked(b"ne", b"w"), size=5)
        with pytest.raises(BlobMismatchError):
            await blob_store.put("c/k", b"new", sha256=sha(b"other"))
        with pytest.raises(BlobMismatchError):
            await blob_store.put("c/fresh", b"new", size=4)
        assert await read_all(blob_store.get("c/k")) == b"old"
        assert await blob_store.stat("c/fresh") is None


class BlobStoreContract(BlobStoreCoreContract):
    """The core, plus signed URLs. Test methods take the three fixtures by name."""

    put_needs_sha256: bool = False

    def _sha(self, body: bytes) -> str | None:
        return sha(body) if self.put_needs_sha256 else None

    async def test_a_deleted_keys_url_is_refused(self, blob_store: BlobStore, fetch: Fetch) -> None:
        await blob_store.put("d/a", b"d/a")
        get = await blob_store.signed_url("d/a", method="GET")
        assert await blob_store.delete("d/a") is True
        assert refused(await send(fetch, get))

    async def test_signed_url_lifetime_is_at_most_ten_minutes(
        self, blob_store: BlobStore, clock: SettableClock
    ) -> None:
        for too_long in (MAX_URL_LIFETIME + timedelta(seconds=1), timedelta(hours=1)):
            with pytest.raises(ValueError, match="10 minutes"):
                await blob_store.signed_url("c/k", method="GET", expires_in=too_long)
        for too_short in (timedelta(0), timedelta(seconds=-1)):
            with pytest.raises(ValueError, match="10 minutes"):
                await blob_store.signed_url("c/k", method="GET", expires_in=too_short)
        signed = await blob_store.signed_url("c/k", method="GET")
        assert timedelta(0) < signed.expires_at - clock.now() <= MAX_URL_LIFETIME

    async def test_signed_url_arguments(self, blob_store: BlobStore) -> None:
        with pytest.raises(ValueError, match="content length"):
            await blob_store.signed_url("c/k", method="PUT")
        with pytest.raises(ValueError, match="GET"):
            await blob_store.signed_url("c/k", method="GET", content_length=1)
        with pytest.raises(ValueError, match="GET"):
            await blob_store.signed_url("c/k", method="GET", sha256=sha(b""))
        with pytest.raises(ValueError, match="sha256"):
            await blob_store.signed_url("c/k", method="PUT", content_length=1, sha256="ABC")

    async def test_put_and_get_through_signed_urls(
        self, blob_store: BlobStore, fetch: Fetch
    ) -> None:
        body = b"bundle bytes " * 1000
        put = await blob_store.signed_url(
            "u/bundle", method="PUT", content_length=len(body), sha256=sha(body)
        )
        assert put.method == "PUT"
        assert 200 <= (await send(fetch, put, body)).status < 300
        stat = await blob_store.stat("u/bundle")
        assert stat is not None
        assert (stat.size, stat.sha256) == (len(body), sha(body))
        get = await blob_store.signed_url("u/bundle", method="GET")
        result = await send(fetch, get)
        assert (result.status, result.body) == (200, body)

    async def test_put_url_without_sha_takes_any_body_of_that_length(
        self, blob_store: BlobStore, fetch: Fetch
    ) -> None:
        if self.put_needs_sha256:
            with pytest.raises(ValueError, match="sha256"):
                await blob_store.signed_url("u/free", method="PUT", content_length=3)
            return
        put = await blob_store.signed_url("u/free", method="PUT", content_length=3)
        assert 200 <= (await send(fetch, put, b"abc")).status < 300
        assert await read_all(blob_store.get("u/free")) == b"abc"

    async def test_expired_url_is_refused(
        self, blob_store: BlobStore, fetch: Fetch, clock: SettableClock
    ) -> None:
        await blob_store.put("u/old", b"old")
        now = clock.now()
        clock.set(now - timedelta(minutes=11))
        put = await blob_store.signed_url(
            "u/new", method="PUT", content_length=3, sha256=self._sha(b"new")
        )
        get = await blob_store.signed_url("u/old", method="GET")
        clock.set(now)
        assert refused(await send(fetch, put, b"new"))
        assert refused(await send(fetch, get))
        assert await blob_store.stat("u/new") is None

    async def test_url_for_one_key_cannot_touch_another(
        self, blob_store: BlobStore, fetch: Fetch
    ) -> None:
        await blob_store.put("u/secret", b"secret")
        put = await blob_store.signed_url(
            "u/mine", method="PUT", content_length=3, sha256=self._sha(b"bad")
        )
        get = await blob_store.signed_url("u/mine", method="GET")
        assert "/u/mine?" in put.url
        assert refused(await send(fetch, put, b"bad", url=put.url.replace("/u/mine?", "/u/other?")))
        assert refused(await send(fetch, get, url=get.url.replace("/u/mine?", "/u/secret?")))
        assert await blob_store.stat("u/other") is None

    async def test_changing_any_query_parameter_invalidates_the_url(
        self, blob_store: BlobStore, fetch: Fetch
    ) -> None:
        body = b"abc"
        put = await blob_store.signed_url("u/k", method="PUT", content_length=3, sha256=sha(body))
        params = parse_qsl(put.url.partition("?")[2], keep_blank_values=True)
        assert params
        for name, value in params:
            url = with_param(put.url, name, altered(value))
            assert refused(await send(fetch, put, body, url=url)), name
        assert await blob_store.stat("u/k") is None

    async def test_wrong_length_is_refused(self, blob_store: BlobStore, fetch: Fetch) -> None:
        put = await blob_store.signed_url(
            "u/k", method="PUT", content_length=10, sha256=self._sha(b"x" * 10)
        )
        assert refused(await send(fetch, put, b"x" * 11))
        assert refused(await send(fetch, put, b"x" * 9))
        assert await blob_store.stat("u/k") is None

    async def test_wrong_sha_is_refused(self, blob_store: BlobStore, fetch: Fetch) -> None:
        put = await blob_store.signed_url(
            "u/k", method="PUT", content_length=10, sha256=sha(b"a" * 10)
        )
        assert refused(await send(fetch, put, b"b" * 10))
        assert await blob_store.stat("u/k") is None

    async def test_a_url_allows_only_its_method(self, blob_store: BlobStore, fetch: Fetch) -> None:
        await blob_store.put("u/k", b"old")
        get = await blob_store.signed_url("u/k", method="GET")
        assert refused(await send(fetch, get, b"new", method="PUT"))
        assert await read_all(blob_store.get("u/k")) == b"old"
        put = await blob_store.signed_url(
            "u/k", method="PUT", content_length=3, sha256=self._sha(b"new")
        )
        result = await send(fetch, put, method="GET")
        assert refused(result)
        assert result.body != b"old"
