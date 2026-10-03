"""Admission from the access snapshot (SSC-050, C19 step 3), read when a request needs it.

The data gateway runs request-billed from zero, so nothing runs between requests and there is no
background poll. The snapshot is read once before the first request is accepted, again by a
request when no read has confirmed it for ``RECHECK_SECONDS``, and every ``WATCH_SECONDS`` by
the kill watch while a query runs. A service that was at zero when the kill switch fired reads
the snapshot that carries the kill before it answers anything. A view no read has confirmed for
``Settings.max_stale`` (120 s) admits nothing: ``DATA_SNAPSHOT_STALE``.
"""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Final, Literal

from ssc_contracts.snapshot import SnapshotConnection, SnapshotConnectionGrant
from ssc_shared.access import AccessView, EnvironmentIndex, ViewHolder
from ssc_shared.snapshot_feed import POLL_SECONDS, SnapshotFeed

log = logging.getLogger(__name__)

RECHECK_SECONDS: Final = POLL_SECONDS
READ_WAIT: Final = 4.0
FIRST_READ_WAIT: Final = 10.0

Refusal = Literal[
    "DATA_SNAPSHOT_STALE",
    "UNKNOWN_ENVIRONMENT",
    "APP_NOT_ACTIVE",
    "CONNECTION_NOT_GRANTED",
    "CONNECTION_SUSPENDED",
]


@dataclass(frozen=True, slots=True)
class Admitted:
    view: AccessView
    env_id: str
    environment: EnvironmentIndex
    connection: SnapshotConnection
    grant: SnapshotConnectionGrant

    @property
    def grant_key(self) -> str:
        """The grant's key for budgets and slots: one grant per connection and environment."""
        return f"{self.connection.connection_id}/{self.env_id}"


def environment_refusal(view: AccessView | None, env_id: str) -> Refusal | None:
    if view is None:
        return "DATA_SNAPSHOT_STALE"
    env = view.environments.get(env_id)
    if env is None:
        return "UNKNOWN_ENVIRONMENT"
    return None if env.active else "APP_NOT_ACTIVE"


def admit(view: AccessView | None, env_id: str, name: str) -> Admitted | Refusal:
    """Whether ``env_id`` may query connection ``name`` now. An unknown connection and one not
    granted to the environment get the same answer, so an app cannot list another's."""
    refused = environment_refusal(view, env_id)
    if refused is not None or view is None:
        return refused or "DATA_SNAPSHOT_STALE"
    connection = view.connections.get(name)
    grant = None if connection is None else connection.grants.get(env_id)
    if connection is None or grant is None:
        return "CONNECTION_NOT_GRANTED"
    if connection.status != "active":
        return "CONNECTION_SUSPENDED"
    return Admitted(view, env_id, view.environments[env_id], connection, grant)


class OnDemandSnapshot:
    """The org's snapshot as requests need it, one read at a time."""

    def __init__(self, feed: SnapshotFeed, holder: ViewHolder, *, max_stale: float) -> None:
        self._feed = feed
        self._holder = holder
        self._max_stale = max_stale
        self._read: asyncio.Task[None] | None = None

    async def _poll(self) -> None:
        try:
            await self._feed.poll_once()
        except Exception:
            log.exception("snapshot read failed")

    async def _wait(self, seconds: float) -> None:
        if self._read is None or self._read.done():
            self._read = asyncio.create_task(self._poll())
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._read), seconds)

    async def first_read(self) -> bool:
        """The read before the first request; False leaves every query refused as stale until
        a later read succeeds."""
        await self._wait(FIRST_READ_WAIT)
        if self.view() is None:
            log.warning(
                "no snapshot at start: every query is DATA_SNAPSHOT_STALE until one is read"
            )
            return False
        return True

    async def refresh(self) -> None:
        """Read ``latest.json`` again unless a read confirmed the view within
        ``RECHECK_SECONDS``, waiting for it up to ``READ_WAIT``."""
        if not self._feed.fresh(RECHECK_SECONDS):
            await self._wait(READ_WAIT)

    def view(self) -> AccessView | None:
        return self._holder.view if self._feed.fresh(self._max_stale) else None

    async def aclose(self) -> None:
        if self._read is not None:
            self._read.cancel()
            await asyncio.gather(self._read, return_exceptions=True)
