"""The filesystem ``BlobStore`` passes the contract, behind a stand-in for the signed-URL route."""

from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qsl

import pytest

from ssc_conformance.contracts.blobstore import BlobStoreContract, FetchResult, ManualClock
from ssc_shared.blobstore import (
    DEFAULT_CONTENT_TYPE,
    BlobKeyError,
    BlobMismatchError,
    BlobNotFoundError,
    BlobTooLargeError,
)
from ssc_shared.blobstore_fs import FsBlobStore, SignedUrlError, UrlSigner

BASE = "http://blobs.test/v1/blobs"
SIGNING_KEY = ("test-" + "blob-" + "signing-" + "key-").encode() * 2


class Route:
    """What B3's HTTP route does: map the URL to ``accept_put``/``open_get`` and errors to 4xx."""

    def __init__(self, store: FsBlobStore) -> None:
        self._store = store

    async def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> FetchResult:
        if not url.startswith(BASE + "/"):
            return FetchResult(404, b"")
        key, _, query = url.removeprefix(BASE + "/").partition("?")
        pairs = parse_qsl(query, keep_blank_values=True)
        params = dict(pairs)
        if len(params) != len(pairs):
            return FetchResult(400, b"")
        try:
            if method == "PUT":
                content_type = headers.get("content-type", DEFAULT_CONTENT_TYPE)
                await self._store.accept_put(key, params, body or b"", content_type=content_type)
                return FetchResult(201, b"")
            if method == "GET":
                _, chunks = await self._store.open_get(key, params)
                return FetchResult(200, b"".join([c async for c in chunks]))
        except SignedUrlError:
            return FetchResult(403, b"")
        except BlobKeyError:
            return FetchResult(400, b"")
        except BlobNotFoundError:
            return FetchResult(404, b"")
        except BlobTooLargeError:
            return FetchResult(413, b"")
        except BlobMismatchError:
            return FetchResult(422, b"")
        return FetchResult(405, b"")


class TestFsBlobStore(BlobStoreContract):
    @pytest.fixture
    def clock(self) -> ManualClock:
        return ManualClock()

    @pytest.fixture
    def blob_store(self, tmp_path: Path, clock: ManualClock) -> FsBlobStore:
        signer = UrlSigner({"k1": SIGNING_KEY}, active="k1", clock=clock)
        return FsBlobStore(tmp_path / "blobs", signer=signer, base_url=BASE, clock=clock)

    @pytest.fixture
    def fetch(self, blob_store: FsBlobStore) -> Route:
        return Route(blob_store)
