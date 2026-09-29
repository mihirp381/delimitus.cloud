"""RFC 8785 canonical JSON: the RFC's own vectors, domain errors and the digest format."""

import json
import re

import pytest

from ssc_shared.canonical import canonical_bytes, canonical_digest

BS, EURO = chr(0x5C), chr(0x20AC)
RFC_VALUE = {
    "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 0.000000000000000000000000001],
    "string": EURO + "$" + chr(0x0F) + "\nA'B" + '"' + BS + BS + '"/',
    "literals": [None, True, False],
}
RFC_OUTPUT = (
    '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
    f'"string":"{EURO}${BS}u000f{BS}nA\'B{BS}"{BS}{BS}{BS}{BS}{BS}"/"}}'
)


def test_rfc8785_section_3_2_2_example() -> None:
    assert canonical_bytes(RFC_VALUE) == RFC_OUTPUT.encode()


def test_rfc8785_section_3_2_3_sorts_keys_by_utf16() -> None:
    keys = [chr(c) for c in (0x20AC, 0x0D, 0xFB33, 0x31, 0x1F600, 0x80, 0xF6)]
    order = list(json.loads(canonical_bytes(dict.fromkeys(keys, 0))))
    assert order == [chr(c) for c in (0x0D, 0x31, 0x80, 0xF6, 0x20AC, 0x1F600, 0xFB33)]


def test_key_order_and_whitespace_do_not_matter() -> None:
    a = json.loads('{"b": [1, {"y": 2, "x": 1}], "a": "t"}')
    b = json.loads('{"a":"t","b":[1,{"x":1,"y":2}]}')
    assert canonical_bytes(a) == canonical_bytes(b) == b'{"a":"t","b":[1,{"x":1,"y":2}]}'
    assert canonical_bytes((1, 2)) == canonical_bytes([1, 2])


@pytest.mark.parametrize("value", [2**53, -(2**53), float("nan"), float("inf")])
def test_values_json_cannot_carry_exactly_are_refused(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_bytes({"v": value})


def test_digest_format() -> None:
    digest = canonical_digest({"a": 1})
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    assert digest == canonical_digest({"a": 1}) != canonical_digest({"a": 2})
