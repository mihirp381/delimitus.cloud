"""The Python verifier against the shared vectors, plus the live JWKS-over-HTTP path."""

import base64
import http.server
import importlib.util
import json
import threading
from pathlib import Path
from typing import Any

import jwt
import pytest

from ssc_app.identity import (
    REFUSAL_CODES,
    IdentityRefused,
    IdentityVerifier,
    token_from_headers,
    verify,
)
from ssc_contracts.identity import IDENTITY_HEADER

ROOT = Path(__file__).resolve().parents[3]
VECTORS_DIR = ROOT / "conformance" / "identity_note"
VECTORS = json.loads((VECTORS_DIR / "vectors.json").read_text())
CASES = {c["name"]: c for c in VECTORS["cases"]}


def _load_generator() -> Any:
    spec = importlib.util.spec_from_file_location("identity_vectors", VECTORS_DIR / "generate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check(case: dict[str, Any], **kw: Any) -> None:
    expect = case["expect"]
    if "ok" in expect:
        note = verify(case["token"], audience=case["audience"], now=VECTORS["now"], **kw)
        got = note.model_dump(exclude_none=True)
        got["groups"] = list(note.groups)
        assert got == expect["ok"], case["name"]
    else:
        with pytest.raises(IdentityRefused) as e:
            verify(case["token"], audience=case["audience"], now=VECTORS["now"], **kw)
        assert e.value.code == expect["refused"], f"{case['name']}: {e.value}"


@pytest.mark.parametrize("name", sorted(CASES))
def test_vector(name: str) -> None:
    _check(CASES[name], keys=VECTORS["jwks"], leeway=VECTORS["leeway"])


def test_vectors_cover_every_refusal_code() -> None:
    seen = {c["expect"]["refused"] for c in VECTORS["cases"] if "refused" in c["expect"]}
    assert seen == REFUSAL_CODES - {"wrong_issuer"}  # issuer is optional; tested below


def test_wrong_issuer_when_pinned() -> None:
    case = CASES["user note"]
    verify(
        case["token"],
        audience=case["audience"],
        keys=VECTORS["jwks"],
        issuer=VECTORS["issuer"],
        now=VECTORS["now"],
    )
    with pytest.raises(IdentityRefused) as e:
        verify(
            case["token"],
            audience=case["audience"],
            keys=VECTORS["jwks"],
            issuer="https://keys.delimitus.com/other",
            now=VECTORS["now"],
        )
    assert e.value.code == "wrong_issuer"


def test_app_a_token_refused_by_app_b_verifier() -> None:
    a = IdentityVerifier(audience=VECTORS["audiences"]["app_a"], keys=VECTORS["jwks"])
    b = IdentityVerifier(audience=VECTORS["audiences"]["app_b"], keys=VECTORS["jwks"])
    token = CASES["user note"]["token"]
    assert a.verify(token, now=VECTORS["now"]).sub.startswith("usr_")
    with pytest.raises(IdentityRefused) as e:
        b.verify(token, now=VECTORS["now"])
    assert e.value.code == "wrong_audience"


def test_from_headers_any_case_and_missing() -> None:
    v = IdentityVerifier(audience=VECTORS["audiences"]["app_a"], keys=VECTORS["jwks"])
    token = CASES["user note"]["token"]
    assert v.from_headers({"x-ssc-identity": token}, now=VECTORS["now"]).email == "ada@example.com"
    assert token_from_headers({IDENTITY_HEADER: token}) == token
    with pytest.raises(IdentityRefused) as e:
        v.from_headers({"Cookie": "x"}, now=VECTORS["now"])
    assert e.value.code == "missing"


def test_schedule_note_has_no_display_fields() -> None:
    note = verify(
        CASES["schedule note"]["token"],
        audience=VECTORS["audiences"]["app_a"],
        keys=VECTORS["jwks"],
        now=VECTORS["now"],
    )
    assert note.is_schedule and note.role == "schedule" and note.name is None and note.email is None


def test_default_clock_refuses_the_fixed_vectors_as_expired() -> None:
    with pytest.raises(IdentityRefused) as e:
        verify(
            CASES["user note"]["token"],
            audience=VECTORS["audiences"]["app_a"],
            keys=VECTORS["jwks"],
        )
    assert e.value.code == "expired"


def test_regenerated_vectors_match_the_committed_ones() -> None:
    """Signatures are randomised (ECDSA), so compare everything but the token bytes, then verify."""
    fresh = _load_generator().build_vectors()

    def strip(doc: dict[str, Any]) -> dict[str, Any]:
        return {
            **doc,
            "cases": [{k: v for k, v in c.items() if k != "token"} for c in doc["cases"]],
        }

    assert strip(fresh) == strip(VECTORS)
    for case in fresh["cases"]:
        _check(case, keys=fresh["jwks"], leeway=fresh["leeway"])


def test_jwks_over_http_and_rotation_to_second_key() -> None:
    body = json.dumps(VECTORS["jwks"]).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/jwks.json"
        v = IdentityVerifier(audience=VECTORS["audiences"]["app_a"], keys=url)
        assert v.verify(CASES["user note"]["token"], now=VECTORS["now"]).sub.startswith("usr_")
        assert v.verify(CASES["user note signed by the second key"]["token"], now=VECTORS["now"])
        with pytest.raises(IdentityRefused) as e:
            v.verify(CASES["unknown kid"]["token"], now=VECTORS["now"])
        assert e.value.code == "unknown_key"
    finally:
        server.shutdown()


def test_jwks_inline_as_a_data_url_needs_no_network() -> None:
    """Apps have no internet (SSC-027), so the cell hands them the JWKS inline in
    ``SSC_IDENTITY_KEYS_URL`` (docs/contracts/identity-note.md)."""
    url = (
        "data:application/json;base64,"
        + base64.b64encode(json.dumps(VECTORS["jwks"]).encode()).decode()
    )
    v = IdentityVerifier(audience=VECTORS["audiences"]["app_a"], keys=url)
    assert v.verify(CASES["user note"]["token"], now=VECTORS["now"]).sub.startswith("usr_")
    assert v.verify(CASES["user note signed by the second key"]["token"], now=VECTORS["now"])
    with pytest.raises(IdentityRefused) as e:
        v.verify(CASES["unknown kid"]["token"], now=VECTORS["now"])
    assert e.value.code == "unknown_key"


def test_key_sources_accept_pyjwkset() -> None:
    key_set = jwt.PyJWKSet.from_dict(VECTORS["jwks"])
    assert verify(
        CASES["user note"]["token"],
        audience=VECTORS["audiences"]["app_a"],
        keys=key_set,
        now=VECTORS["now"],
    )
