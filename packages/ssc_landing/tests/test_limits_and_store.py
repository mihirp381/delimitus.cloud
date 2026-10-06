"""SSC-065: the per-address limit and the create-only request store."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from ssc_landing.limits import LIMIT, RateLimit, client_address
from ssc_landing.pilot import PilotRequest
from ssc_landing.store import GcsPilotStore, StoreError, object_key

REQUEST = PilotRequest(
    first_name="Dana",
    last_name="Ortiz",
    email="dana@example.com",
    company="Example Logistics",
    tools="",
    asked_at=datetime(2026, 10, 1, 22, 15, 30, tzinfo=UTC),
)


@pytest.mark.parametrize(
    ("forwarded", "peer", "hops", "expected"),
    [
        ("203.0.113.7, 35.191.0.1", "169.254.1.1", 2, "203.0.113.7"),
        ("6.6.6.6, 203.0.113.7, 35.191.0.1", "169.254.1.1", 2, "203.0.113.7"),
        ("35.191.0.1", "169.254.1.1", 2, "169.254.1.1"),
        (None, "127.0.0.1", 2, "127.0.0.1"),
        ("203.0.113.7, 35.191.0.1", "127.0.0.1", 0, "127.0.0.1"),
        (None, None, 0, "unknown"),
    ],
)
def test_the_client_is_read_from_the_load_balancers_entry(
    forwarded: str | None, peer: str | None, hops: int, expected: str
) -> None:
    assert client_address(forwarded, peer, hops) == expected


def test_a_sixth_request_in_a_minute_is_refused_then_allowed_again() -> None:
    now = [1000.0]
    limit = RateLimit(lambda: now[0])
    assert all(limit.allow("a") for _ in range(LIMIT))
    assert not limit.allow("a")
    assert limit.allow("b")
    now[0] += 59.9
    assert not limit.allow("a")
    now[0] += 0.2
    assert limit.allow("a")


def test_the_limit_forgets_addresses_rather_than_growing() -> None:
    now = [0.0]
    limit = RateLimit(lambda: now[0], max_tracked=10)
    for i in range(10):
        limit.allow(f"busy-{i}")
    assert limit.allow("new")  # everyone is in the window: the oldest half is dropped
    assert len(limit._seen) <= 10  # pyright: ignore[reportPrivateUsage]
    now[0] = 120.0
    for i in range(20):
        limit.allow(f"later-{i}")
    assert len(limit._seen) <= 10  # pyright: ignore[reportPrivateUsage]


def test_object_keys_sort_by_time() -> None:
    assert object_key(REQUEST, "0123abcd") == "requests/2026/10/01/221530-0123abcd.json"


class _Blob:
    def __init__(self, sink: list[dict[str, Any]], key: str, fail: bool) -> None:
        self._sink, self._key, self._fail = sink, key, fail

    def upload_from_string(
        self, data: bytes, content_type: str, *, if_generation_match: int, timeout: float
    ) -> None:
        if self._fail:
            raise RuntimeError("403 Forbidden")
        self._sink.append(
            {
                "key": self._key,
                "data": data,
                "content_type": content_type,
                "if_generation_match": if_generation_match,
            }
        )


class _Bucket:
    def __init__(self, fail: bool = False) -> None:
        self.uploads: list[dict[str, Any]] = []
        self._fail = fail

    def blob(self, blob_name: str) -> _Blob:
        return _Blob(self.uploads, blob_name, self._fail)


async def test_the_store_creates_and_never_replaces() -> None:
    bucket = _Bucket()
    await GcsPilotStore(bucket).add(REQUEST)
    [upload] = bucket.uploads
    assert upload["if_generation_match"] == 0
    assert upload["content_type"] == "application/json"
    assert upload["key"].startswith("requests/2026/10/01/221530-")
    assert json.loads(upload["data"])["email"] == "dana@example.com"


async def test_a_failed_write_is_a_store_error_without_the_details() -> None:
    with pytest.raises(StoreError, match="^RuntimeError$"):
        await GcsPilotStore(_Bucket(fail=True)).add(REQUEST)
