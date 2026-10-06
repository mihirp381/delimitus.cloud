"""SSC-065: what the pilot request form accepts and what it refuses."""

import json
from datetime import UTC, datetime

import pytest

from ssc_landing.pilot import (
    MAX_FIELD,
    PilotRefused,
    is_trapped,
    parse_body,
    pilot_request_from,
)

AT = datetime(2026, 10, 1, 22, 15, 30, tzinfo=UTC)
GOOD = {
    "firstName": " Dana ",
    "lastName": "Ortiz",
    "email": "Dana.Ortiz@Example.com",
    "company": "Example Logistics",
    "tools": "Claude Code",
}


def test_a_good_request_is_trimmed_and_the_email_lowercased() -> None:
    request = pilot_request_from(GOOD, AT)
    assert request.first_name == "Dana"
    assert request.email == "dana.ortiz@example.com"
    assert request.tools == "Claude Code"
    stored = json.loads(request.to_json())
    assert stored == {
        "schema": "ssc-pilot-request/v1",
        "firstName": "Dana",
        "lastName": "Ortiz",
        "email": "dana.ortiz@example.com",
        "company": "Example Logistics",
        "tools": "Claude Code",
        "askedAt": "2026-10-01T22:15:30+00:00",
    }


def test_tools_are_optional() -> None:
    fields = {k: v for k, v in GOOD.items() if k != "tools"}
    assert pilot_request_from(fields, AT).tools == ""


@pytest.mark.parametrize(
    ("change", "bad"),
    [
        ({"firstName": ""}, ("firstName",)),
        ({"lastName": "   "}, ("lastName",)),
        ({"company": ""}, ("company",)),
        ({"email": ""}, ("email",)),
        ({"email": "dana"}, ("email",)),
        ({"email": "dana@example"}, ("email",)),
        ({"email": "a@b.co, c@d.co"}, ("email",)),
        ({"company": "x" * (MAX_FIELD + 1)}, ("company",)),
        ({"tools": "y" * (MAX_FIELD + 1)}, ("tools",)),
        ({"lastName": "Ortiz\r\nBcc: x@y.z"}, ("lastName",)),
        ({"firstName": "", "email": "nope"}, ("firstName", "email")),
    ],
)
def test_a_bad_field_is_named(change: dict[str, str], bad: tuple[str, ...]) -> None:
    with pytest.raises(PilotRefused) as caught:
        pilot_request_from({**GOOD, **change}, AT)
    assert caught.value.status == 422
    assert caught.value.code == "FIELDS_INVALID"
    assert caught.value.fields == bad


def test_json_and_plain_form_bodies_read_the_same() -> None:
    from_json = parse_body("application/json", json.dumps(GOOD).encode())
    from_form = parse_body(
        "application/x-www-form-urlencoded; charset=UTF-8",
        b"firstName=+Dana+&lastName=Ortiz&email=Dana.Ortiz%40Example.com"
        b"&company=Example+Logistics&tools=Claude+Code&website=",
    )
    assert pilot_request_from(from_json, AT) == pilot_request_from(from_form, AT)


def test_json_values_that_are_not_strings_are_dropped() -> None:
    fields = parse_body("application/json", b'{"firstName": 1, "email": ["a"], "company": "C"}')
    assert fields == {"company": "C"}


@pytest.mark.parametrize(
    ("content_type", "body", "status"),
    [
        ("application/json", b"[1, 2]", 400),
        ("application/json", b"{not json", 400),
        ("application/json", b"\xff\xfe", 400),
        ("text/plain", b"firstName=Dana", 415),
        ("multipart/form-data; boundary=x", b"--x--", 415),
        ("", b"", 415),
    ],
)
def test_unreadable_or_unexpected_bodies_are_refused(
    content_type: str, body: bytes, status: int
) -> None:
    with pytest.raises(PilotRefused) as caught:
        parse_body(content_type, body)
    assert caught.value.status == status


def test_the_honeypot_trips_on_any_value() -> None:
    assert not is_trapped(GOOD)
    assert not is_trapped({**GOOD, "website": "  "})
    assert is_trapped({**GOOD, "website": "https://spam.example"})
