"""Abuse limits on the form: five requests a minute per address, counted in this instance.

Cloud Run may run more than one instance, so the limit is per instance; the hosting caps the
service at a few. The address is the client's, as the external load balancer appends it to
``X-Forwarded-For``: ``<anything the client sent>, <client>, <load balancer>``. Entries a client
writes itself come first and are never read.
"""

from collections import deque
from collections.abc import Callable
from typing import Final

LIMIT: Final = 5
WINDOW_SECONDS: Final = 60.0
MAX_TRACKED: Final = 10_000
LOAD_BALANCER_HOPS: Final = 2
"""The client is the second entry from the right of ``X-Forwarded-For``."""


def client_address(forwarded_for: str | None, peer: str | None, hops: int) -> str:
    """The address to count against; ``hops`` 0 means no proxy is trusted (dev)."""
    if hops > 0 and forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return peer or "unknown"


class RateLimit:
    def __init__(
        self,
        clock: Callable[[], float],
        limit: int = LIMIT,
        window: float = WINDOW_SECONDS,
        max_tracked: int = MAX_TRACKED,
    ) -> None:
        self._clock = clock
        self._limit = limit
        self._window = window
        self._max_tracked = max_tracked
        self._seen: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Count one request from ``key``; False once it has made ``limit`` in the window."""
        now = self._clock()
        times = self._seen.get(key)
        if times is None:
            if len(self._seen) >= self._max_tracked:
                self._forget(now)
            times = self._seen.setdefault(key, deque())
        while times and now - times[0] >= self._window:
            times.popleft()
        if len(times) >= self._limit:
            return False
        times.append(now)
        return True

    def _forget(self, now: float) -> None:
        """Drop addresses with nothing in the window; if all are busy, drop the oldest half."""
        for key in [k for k, t in self._seen.items() if not t or now - t[-1] >= self._window]:
            del self._seen[key]
        if len(self._seen) >= self._max_tracked:
            oldest = sorted(self._seen, key=lambda k: self._seen[k][-1])
            for key in oldest[: len(oldest) // 2]:
                del self._seen[key]
