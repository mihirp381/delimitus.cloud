"""Per-credential rate limit: a token bucket keyed by the credential id.

In-process for the skeleton. With more than one API instance each instance keeps its own
buckets, so the effective limit is N times the configured one; a shared limiter (Postgres or a
cell-local store) is a later step and changes nothing for callers, who already see ``429`` with
``Retry-After``.

A bucket left alone for ``capacity / refill`` seconds is full again, the same as no bucket, so
such buckets are dropped (at most once per that interval): the map holds only the credentials
used lately, not every session the process has seen.
"""

import math
import threading
import time
from collections.abc import Callable

from fastapi import Request

from ssc_contracts.errors import ErrorCode
from ssc_control.api.auth import Principal
from ssc_control.api.problems import Refusal
from ssc_control.api.runtime import runtime_of


class RateLimiter:
    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or refill_per_second <= 0:
            raise ValueError("capacity must be >= 1 and refill_per_second > 0")
        self.capacity = capacity
        self.refill = refill_per_second
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._full_after = capacity / refill_per_second
        self._swept = clock()

    def take(self, key: str) -> float:
        """Take one token. Returns 0.0 when allowed, else the seconds until the next token."""
        now = self._clock()
        with self._lock:
            if now - self._swept >= self._full_after:
                self._sweep(now)
            tokens, last = self._buckets.get(key, (float(self.capacity), now))
            tokens = min(float(self.capacity), tokens + (now - last) * self.refill)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return 0.0
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / self.refill

    def _sweep(self, now: float) -> None:
        """Drop the buckets that have refilled to capacity. Call holding the lock."""
        self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < self._full_after}
        self._swept = now

    def __len__(self) -> int:
        """How many buckets are kept."""
        return len(self._buckets)


def limit(request: Request, principal: Principal) -> None:
    wait = runtime_of(request).limiter.take(principal.credential_id)
    if wait > 0:
        raise Refusal(
            ErrorCode.RATE_LIMITED,
            evidence={"credential_id": principal.credential_id, "wait": wait},
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
