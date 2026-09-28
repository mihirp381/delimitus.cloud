from hypothesis import given
from hypothesis import strategies as st


@given(st.integers())
def test_planted_property_failure(n):
    assert n < 1000
