"""Where pilot requests go: one JSON object each, in a private bucket of the control project.

Not the control database: every control table belongs to an org, and a pilot request has none.
The service account that writes may create objects and nothing else
(``roles/storage.objectCreator``), so this service cannot read, list or replace a stored request;
the founder reads them. Objects
are created with ``ifGenerationMatch=0``, which needs no read permission and never overwrites.
"""

import asyncio
import secrets
from typing import Any, Final, Protocol

from ssc_landing.pilot import PilotRequest

TIMEOUT_SECONDS: Final = 10.0
PREFIX: Final = "requests"


class StoreError(Exception):
    """The request was not stored."""


class PilotStore(Protocol):
    async def add(self, request: PilotRequest) -> None: ...


class GcsBlob(Protocol):
    """The part of ``google.cloud.storage.Blob`` this store uses."""

    def upload_from_string(
        self, data: bytes, content_type: str, *, if_generation_match: int, timeout: float
    ) -> None: ...


class GcsBucket(Protocol):
    """The part of ``google.cloud.storage.Bucket`` this store uses."""

    def blob(self, blob_name: str) -> GcsBlob: ...


def bucket_named(name: str) -> GcsBucket:
    """The bucket, without a call: a bucket GET would need a read permission we do not hold."""
    from google.cloud import storage  # pyright: ignore[reportMissingTypeStubs]  # noqa: PLC0415

    client: Any = storage.Client()
    return client.bucket(name)


def object_key(request: PilotRequest, token: str) -> str:
    """``requests/2026/10/01/221530-<token>.json``: sorted by time, never guessable or reused."""
    return f"{PREFIX}/{request.asked_at:%Y/%m/%d/%H%M%S}-{token}.json"


class MemoryPilotStore:
    """For tests and the dev server."""

    def __init__(self) -> None:
        self.requests: list[PilotRequest] = []

    async def add(self, request: PilotRequest) -> None:
        self.requests.append(request)


class GcsPilotStore:
    def __init__(self, bucket: GcsBucket) -> None:
        self._bucket = bucket

    def _write(self, key: str, data: bytes) -> None:
        self._bucket.blob(key).upload_from_string(
            data, "application/json", if_generation_match=0, timeout=TIMEOUT_SECONDS
        )

    async def add(self, request: PilotRequest) -> None:
        key = object_key(request, secrets.token_hex(8))
        try:
            await asyncio.to_thread(self._write, key, request.to_json())
        except Exception as e:  # noqa: BLE001  (any failure is "not stored"; the cause is logged)
            raise StoreError(type(e).__name__) from e
