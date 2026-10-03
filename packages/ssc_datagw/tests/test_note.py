"""The data gateway's note verifier against the shared identity-note vectors (SSC-050), the same
ones the Python and Node app helpers run."""

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from jwt import PyJWKSet

from ssc_datagw.note import NoteRefusedError, RefusalCode, verify_note

ROOT = Path(__file__).resolve().parents[3]
VECTORS = json.loads((ROOT / "conformance" / "identity_note" / "vectors.json").read_text())
CASES = {c["name"]: c for c in VECTORS["cases"]}
KEYS = PyJWKSet.from_dict(VECTORS["jwks"])


def _verify(case: dict[str, Any], issuer: str | None = None) -> Any:
    return verify_note(
        case["token"],
        audience=case["audience"],
        keys=KEYS,
        issuer=issuer,
        now=VECTORS["now"],
        leeway=VECTORS["leeway"],
    )


@pytest.mark.parametrize("name", sorted(CASES))
def test_vector(name: str) -> None:
    case = CASES[name]
    expect = case["expect"]
    if "ok" in expect:
        got = _verify(case).model_dump(exclude_none=True)
        got["groups"] = list(got["groups"])
        assert got == expect["ok"]
    else:
        with pytest.raises(NoteRefusedError) as e:
            _verify(case)
        assert e.value.code == expect["refused"]


def test_the_vectors_cover_every_code_but_the_pinned_issuer() -> None:
    seen = {c["expect"]["refused"] for c in VECTORS["cases"] if "refused" in c["expect"]}
    assert seen == set(get_args(RefusalCode)) - {"wrong_issuer"}


def test_a_pinned_issuer_refuses_another() -> None:
    case = CASES["user note"]
    assert _verify(case, VECTORS["issuer"]).sub == case["expect"]["ok"]["sub"]
    with pytest.raises(NoteRefusedError) as e:
        _verify(case, "https://keys.delimitus.com/other")
    assert e.value.code == "wrong_issuer"
