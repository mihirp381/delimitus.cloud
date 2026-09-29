import pytest
from hypothesis import given
from hypothesis import strategies as st

from ssc_contracts.ids import ID_LENGTH, PREFIXES, new_id, prefix_of


@given(st.sampled_from(PREFIXES))
def test_round_trip(prefix):
    value = new_id(prefix)
    assert len(value) == len(prefix) + 1 + ID_LENGTH
    assert prefix_of(value) == prefix


@given(st.text(max_size=40))
def test_garbage_is_rejected_or_well_formed(text):
    try:
        head = prefix_of(text)
    except ValueError:
        return
    assert text.startswith(head + "_")


def test_rejects_uppercase():
    with pytest.raises(ValueError):
        prefix_of("usr_ABCDEFGHIJKLMNOPQRST")
