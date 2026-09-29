"""Per-credential rate limit: a token bucket keyed by the credential id.

In-process for the skeleton. With more than one API instance each instance keeps its own
buckets, so the effective limit is N times the configured one; a shared limiter (Postgres or a
cell-local store) is a later step and changes nothing for callers, who already see ``429`` with
``Retry-After``.
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

    def take(self, key: str) -> float:
        """Take one token. Returns 0.0 when allowed, else the seconds until the next token."""
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.capacity), now))
            tokens = min(float(self.capacity), tokens + (now - last) * self.refill)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return 0.0
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / self.refill


def limit(request: Request, principal: Principal) -> None:
    wait = runtime_of(request).limiter.take(principal.credential_id)
    if wait > 0:
        raise Refusal(
            ErrorCode.RATE_LIMITED,
            evidence={"credential_id": principal.credential_id, "wait": wait},
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
