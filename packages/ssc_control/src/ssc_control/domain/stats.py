"""Small-sample statistics for the metrics report (SSC-028), after Delimitus `corpus/src/stats.py`.

Wilson score intervals are for proportions only. Below ``NO_RATE_BELOW_N`` a rate is not
reported: the report prints ``insufficient data (n=…, need 20)`` instead.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

Z95: Final = 1.959963984540054
NO_RATE_BELOW_N: Final = 20

Direction = Literal["above", "below", "indistinguishable"]


@dataclass(frozen=True, slots=True)
class Interval:
    """A proportion with its 95% Wilson interval. NaN throughout when ``n`` is 0."""

    successes: int
    n: int
    point: float
    low: float
    high: float


def wilson(successes: int, n: int, z: float = Z95) -> Interval:
    """The Wilson score interval for ``successes`` out of ``n``."""
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"need 0 <= successes <= n, got {successes}/{n}")
    if n == 0:
        nan = math.nan
        return Interval(successes=0, n=0, point=nan, low=nan, high=nan)
    point = successes / n
    z2 = z * z
    denominator = 1 + z2 / n
    centre = (point + z2 / (2 * n)) / denominator
    spread = z * math.sqrt(point * (1 - point) / n + z2 / (4 * n * n)) / denominator
    return Interval(
        successes=successes,
        n=n,
        point=point,
        low=max(0.0, centre - spread),
        high=min(1.0, centre + spread),
    )


def is_reportable(n: int) -> bool:
    """Whether a rate or a summary over ``n`` observations is reported at all."""
    return n >= NO_RATE_BELOW_N


def insufficient(n: int) -> str:
    return f"insufficient data (n={n}, need {NO_RATE_BELOW_N})"


def format_rate(interval: Interval) -> str:
    """``40.0% [16.8%–68.7%] n=10``, or the insufficient-data flag below the threshold."""
    if not is_reportable(interval.n):
        return insufficient(interval.n)
    return f"{interval.point:.1%} [{interval.low:.1%}–{interval.high:.1%}] n={interval.n}"


def versus(interval: Interval, target: float) -> Direction:
    """Whether the whole interval lies above or below ``target``."""
    if math.isnan(interval.low):
        return "indistinguishable"
    if interval.low > target:
        return "above"
    if interval.high < target:
        return "below"
    return "indistinguishable"


def percentile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default); ``q`` in [0, 1]."""
    if not values:
        raise ValueError("percentile of no values")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be in [0, 1], got {q}")
    ordered = sorted(values)
    rank = q * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)
