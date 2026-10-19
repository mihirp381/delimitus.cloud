"""The per-credential token bucket: limits as before, and forgets credentials gone quiet."""

from ssc_control.api.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_bucket_runs_dry_and_refills() -> None:
    clock = Clock()
    limiter = RateLimiter(capacity=2, refill_per_second=1.0, clock=clock)
    assert [limiter.take("a"), limiter.take("a")] == [0.0, 0.0]
    assert limiter.take("a") == 1.0
    clock.now += 1.0
    assert limiter.take("a") == 0.0


def test_buckets_of_quiet_credentials_are_dropped() -> None:
    clock = Clock()
    limiter = RateLimiter(capacity=10, refill_per_second=5.0, clock=clock)
    for n in range(1000):
        limiter.take(f"session-{n}")
    assert len(limiter) == 1000
    clock.now += 2.0  # capacity / refill: every bucket is full again
    assert limiter.take("fresh") == 0.0
    assert len(limiter) == 1


def test_a_bucket_not_yet_full_is_kept_and_still_limits() -> None:
    clock = Clock()
    limiter = RateLimiter(capacity=3, refill_per_second=1.0, clock=clock)
    for _ in range(3):
        assert limiter.take("busy") == 0.0
    assert limiter.take("busy") > 0.0
    clock.now += 2.5  # not yet full: kept, and still limited where it was
    limiter.take("other")
    assert len(limiter) == 2
    assert [limiter.take("busy"), limiter.take("busy")] == [0.0, 0.0]
    assert limiter.take("busy") > 0.0
