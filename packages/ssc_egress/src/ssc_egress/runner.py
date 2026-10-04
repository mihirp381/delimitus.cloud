"""Keeps Envoy's listener on the org's newest snapshot (SSC-053).

``follow`` polls the cell bucket's snapshot (``ssc_shared.snapshot_feed``) every
``POLL_SECONDS`` and, when a newer version changes what the proxy allows, writes the listener
for it. The file is written beside ``LDS_FILE`` and moved over it, so Envoy never reads half a
file. A snapshot that changes nothing the proxy reads writes nothing, so its tunnels are not
drained for it. When the feed fails, the last good listener stays.
"""

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

from ssc_egress.envoy import LDS_FILE, EgressConfig, Policy, lds_document, policy_of
from ssc_shared.access import ViewHolder
from ssc_shared.snapshot_feed import POLL_SECONDS, SnapshotFeed

log = logging.getLogger(__name__)


class ListenerWriter:
    """Writes the listener for a policy when it differs from the last one written."""

    def __init__(self, cfg: EgressConfig) -> None:
        self._cfg = cfg
        self.version: str | None = None

    def write(self, policy: Policy) -> bool:
        """True when a new listener was written."""
        doc = lds_document(self._cfg, policy)
        if doc["version_info"] == self.version:
            return False
        path = Path(self._cfg.lds_dir) / LDS_FILE
        tmp = path.with_name(f".{LDS_FILE}.tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)
        self.version = doc["version_info"]
        log.info(
            "egress listener written",
            extra={
                "listener_version": self.version,
                "hosts": len(policy.hosts),
                "credentials": len(policy.users),
            },
        )
        return True


async def follow(  # noqa: PLR0913  (keyword-only)
    feed: SnapshotFeed,
    holder: ViewHolder,
    writer: ListenerWriter,
    *,
    stop: asyncio.Event,
    envoy: subprocess.Popen[bytes] | None = None,
    interval: float = POLL_SECONDS,
) -> None:
    """Poll until ``stop`` is set or ``envoy`` exits, writing each newer snapshot's listener.
    Both are checked every ``interval`` even while a poll waits on an unreachable bucket; that
    poll is then abandoned."""

    def running() -> bool:
        return not stop.is_set() and (envoy is None or envoy.poll() is None)

    while running():
        poll = asyncio.ensure_future(feed.poll_once())
        while running() and not poll.done():
            await asyncio.wait({poll}, timeout=interval)
        if not poll.done():
            poll.cancel()
            return
        if poll.result():
            writer.write(policy_of(holder.view))
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
