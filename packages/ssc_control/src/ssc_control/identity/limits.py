"""Abuse limits on the auth host's unauthenticated steps and on CI tokens, counted in this
instance.

``/register`` takes ten clients an hour per address. The work-email step of ``/authorize`` takes
twenty tries an hour per address and twenty per email domain, because each try asks WorkOS and
looks the organisation up across every org (decision 029). ``/device/authorize`` takes thirty
command-line logins an hour per address, as each one stores a device code. ``/ci-tokens`` takes
ten CI tokens an hour per person (GA-7.7).

Sign-in (decision 024 amendment of 2026-10-19), each counted before the route reads the
database or calls WorkOS:

- ``GET /login``: sixty an hour per address, then six hundred per org.
- ``GET /callback``: sixty an hour per address, and once twenty sign-ins from an address were
  refused in the hour (``full``), the next is answered without WorkOS or an audit row.
- ``POST /device`` (the user code, RFC 8628's brute-force target): ten an hour per address and
  sixty per org.
- ``POST /token``: two thousand an hour per address (device polling is 120 calls a login), and
  after thirty ``invalid_grant`` answers from an address in the hour, none until the hour slides.
- ``POST /revoke``: sixty an hour per address.
- ``POST /internal/redeem``: after sixty codes an org's gateway could not redeem in the hour,
  none for that org; the gateway is the caller, so the key is the org, never an address.

Cloud Run may run more than one instance, so a limit is per instance. The address is the
client's, as the control load balancer appends it to ``X-Forwarded-For``: ``<anything the client
sent>, <client>, <load balancer>``, so with two trusted hops it is the second entry from the
right. Entries a client writes itself come first and are never read. The same shape as the
public site's form limit (``ssc_landing.limits``), which this package may not import.
"""

from collections import deque
from collections.abc import Callable
from typing import Final

HOUR: Final = 3600.0
MAX_TRACKED: Final = 10_000
LOAD_BALANCER_HOPS: Final = 2
"""The client is the second entry from the right of ``X-Forwarded-For``."""
REGISTER_PER_HOUR: Final = 10
EMAIL_PER_HOUR: Final = 20
DEVICE_PER_HOUR: Final = 30
"""Command-line logins (``/device/authorize``) an address may start an hour."""
CI_TOKENS_PER_HOUR: Final = 10
"""CI tokens a person may create an hour (GA-7.7), counted per person, not per address."""
LOGIN_PER_HOUR: Final = 60
LOGIN_PER_ORG_PER_HOUR: Final = 600
CALLBACK_PER_HOUR: Final = 60
CALLBACK_FAILURES_PER_HOUR: Final = 20
"""Refused sign-ins an address may reach ``/callback`` with an hour."""
USER_CODE_PER_HOUR: Final = 10
USER_CODE_PER_ORG_PER_HOUR: Final = 60
TOKEN_PER_HOUR: Final = 2000
TOKEN_FAILURES_PER_HOUR: Final = 30
"""``invalid_grant`` answers ``/token`` gives an address an hour."""
REVOKE_PER_HOUR: Final = 60
REDEEM_FAILURES_PER_HOUR: Final = 60
"""Codes an org's gateway may fail to redeem an hour."""


def client_address(forwarded_for: str | None, peer: str | None, hops: int) -> str:
    """The address to count against; ``hops`` 0 means no proxy is trusted (dev and tests)."""
    if hops > 0 and forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return peer or "unknown"


class RateLimit:
    """A sliding window per key, tracking at most ``max_tracked`` keys."""

    def __init__(
        self,
        clock: Callable[[], float],
        limit: int,
        window: float = HOUR,
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

    def full(self, key: str) -> bool:
        """Whether ``key`` has made ``limit`` requests in the window; counts nothing."""
        times = self._seen.get(key)
        if not times:
            return False
        now = self._clock()
        return sum(1 for t in times if now - t < self._window) >= self._limit

    def _forget(self, now: float) -> None:
        """Drop keys with nothing in the window; if all are busy, drop the oldest half."""
        for key in [k for k, t in self._seen.items() if not t or now - t[-1] >= self._window]:
            del self._seen[key]
        if len(self._seen) >= self._max_tracked:
            oldest = sorted(self._seen, key=lambda k: self._seen[k][-1])
            for key in oldest[: len(oldest) // 2]:
                del self._seen[key]
