"""SSC-028: the report's statistics (pure)."""

import math

import pytest

from ssc_control.domain.stats import (
    NO_RATE_BELOW_N,
    format_rate,
    insufficient,
    is_reportable,
    percentile,
    versus,
    wilson,
)


def test_wilson_matches_the_published_interval() -> None:
    i = wilson(8, 10)
    assert (round(i.low, 4), round(i.high, 4)) == (0.4902, 0.9433)
    assert i.point == 0.8


def test_wilson_edges() -> None:
    empty = wilson(0, 0)
    assert all(math.isnan(v) for v in (empty.point, empty.low, empty.high))
    assert wilson(0, 40).low == 0.0
    assert wilson(40, 40).high == 1.0
    for bad in ((3, 2), (-1, 2), (0, -1)):
        with pytest.raises(ValueError, match="successes"):
            wilson(*bad)


def test_small_samples_are_flagged_not_rated() -> None:
    assert NO_RATE_BELOW_N == 20
    assert not is_reportable(19)
    assert is_reportable(20)
    assert format_rate(wilson(10, 19)) == "insufficient data (n=19, need 20)"
    assert format_rate(wilson(0, 0)) == insufficient(0)
    assert format_rate(wilson(8, 20)) == "40.0% [21.9%–61.3%] n=20"


def test_versus_a_target() -> None:
    assert versus(wilson(35, 40), 0.3) == "above"
    assert versus(wilson(2, 40), 0.3) == "below"
    assert versus(wilson(4, 10), 0.4) == "indistinguishable"
    assert versus(wilson(0, 0), 0.4) == "indistinguishable"


def test_percentile_interpolates_between_ranks() -> None:
    assert percentile([3.0], 0.5) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert percentile([4.0, 1.0, 3.0, 2.0], 0.75) == 3.25
    with pytest.raises(ValueError, match="no values"):
        percentile([], 0.5)
    with pytest.raises(ValueError, match="q must"):
        percentile([1.0], 1.5)
