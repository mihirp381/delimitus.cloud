"""Result values as JSON (SSC-050): exact, never through a float."""

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from ssc_datagw.connectors import encoded_size, jsonable

CASES = [
    (None, None),
    (True, True),
    (7, 7),
    (2**63, 2**63),
    ("text", "text"),
    (1.5, 1.5),
    (float("nan"), "nan"),
    (float("inf"), "inf"),
    (Decimal("12345678901234567890.123456789"), "12345678901234567890.123456789"),
    (Decimal("0.10"), "0.10"),
    (datetime(2026, 10, 3, 12, 0, 1, 500, tzinfo=UTC), "2026-10-03T12:00:01.000500+00:00"),
    (date(2026, 10, 3), "2026-10-03"),
    (time(23, 59, 59), "23:59:59"),
    (timedelta(days=1, seconds=2, microseconds=500_000), "PT86402.5S"),
    (timedelta(0), "PT0S"),
    (b"\x00\xff", "AP8="),
    (UUID(int=1), "00000000-0000-0000-0000-000000000001"),
    ({"a": Decimal("1.0"), 2: [b"x"]}, {"a": "1.0", "2": ["eA=="]}),
    ((1, Decimal(2)), [1, "2"]),
]


@pytest.mark.parametrize(("value", "expect"), CASES, ids=[repr(c[0]) for c in CASES])
def test_jsonable(value: object, expect: object) -> None:
    got = jsonable(value)
    assert got == expect
    json.dumps(got, allow_nan=False)


def test_the_size_is_the_compact_json_and_its_comma() -> None:
    assert encoded_size([1, "é"]) == len('[1,"é"]'.encode()) + 1
