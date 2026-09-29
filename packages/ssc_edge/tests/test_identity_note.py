"""The minter: header, claim shape, five-minute life, no nulls on the wire."""

import json
from pathlib import Path

import jwt
import pytest
from jwt.algorithms import ECAlgorithm
from pydantic import ValidationError

from ssc_contracts.identity import IDENTITY_ALG, IDENTITY_TYP, MAX_GROUPS, IdentityNote
from ssc_edge.identity_note import compose_note, jwks, note_claims, sign_note

KEYS = json.loads(
    (Path(__file__).resolve().parents[3] / "conformance/identity_note/test_keys.json").read_text()
)["keys"]
KEY = ECAlgorithm.from_jwk(KEYS["test-2026-09-a"])
BASE = dict(
    issuer="https://keys.delimitus.com/cell-test",
    audience="https://quiet-river-7f3k.delimitusapps.com",
    subject="usr_11111111111111111111",
    org="org_0123456789abcdefghij",
    app="app_aaaaaaaaaaaaaaaaaaaa",
    env="prod",
    role="user",
    now=1_790_000_000,
)


def test_header_and_ttl() -> None:
    note = compose_note(**BASE, name="Ada", email="ada@example.com")
    token = sign_note(note, private_key=KEY, kid="k1")
    header = jwt.get_unverified_header(token)
    assert header == {"alg": IDENTITY_ALG, "typ": IDENTITY_TYP, "kid": "k1"}
    assert note.exp - note.iat == 300


def test_schedule_note_omits_display_claims_on_the_wire() -> None:
    note = compose_note(**{**BASE, "subject": "sch_22222222222222222222", "role": "schedule"})
    claims = note_claims(note)
    assert "name" not in claims and "email" not in claims and claims["groups"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"subject": "ada@example.com"},
        {"subject": "sch_22222222222222222222", "role": "schedule", "email": "a@b.co"},
        {"role": "schedule"},
        {"role": "admin"},
        {"groups": tuple(f"grp_{i:020d}" for i in range(MAX_GROUPS + 1))},
        {"groups": ("Finance",)},
        {"audience": "https://quiet-river-7f3k.delimitusapps.com/"},
        {"audience": "http://quiet-river-7f3k.delimitusapps.com"},
    ],
)
def test_model_refuses(bad: dict) -> None:
    with pytest.raises(ValidationError):
        compose_note(**{**BASE, **bad})


def test_model_refuses_long_ttl_directly() -> None:
    with pytest.raises(ValidationError):
        IdentityNote(
            **{k: v for k, v in note_claims(compose_note(**BASE)).items() if k != "exp"},
            exp=BASE["now"] + 301,
        )


def test_jwks_is_public_only() -> None:
    doc = jwks((KEY.public_key(), "k1"))
    (entry,) = doc["keys"]
    assert entry["kty"] == "EC" and entry["crv"] == "P-256" and entry["kid"] == "k1"
    assert "d" not in entry
