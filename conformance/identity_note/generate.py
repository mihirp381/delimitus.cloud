"""Write ``vectors.json``: the shared identity-note test vectors for the Python and Node helpers.

Run ``uv run python conformance/identity_note/generate.py``. Signatures are ECDSA, so the token
strings differ on every run; everything else (cases, claims, expected codes) is fixed, and every
regenerated token verifies the same way. The Python test checks exactly that.
"""

import base64
import json
import sys
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from jwt.algorithms import ECAlgorithm

from ssc_contracts.identity import IDENTITY_ALG, IDENTITY_TYP, MAX_GROUPS, IdentityNote
from ssc_edge.identity_note import TTL_SECONDS, compose_note, jwks, note_claims, sign_note

HERE = Path(__file__).parent
KEYS_FILE = HERE / "test_keys.json"
VECTORS_FILE = HERE / "vectors.json"

NOW = 1_790_000_000  # 2026-09-21T09:33:20Z, fixed so the vectors never age
ISSUER = "https://keys.delimitus.com/cell-test"
APP_A = "https://quiet-river-7f3k.delimitusapps.com"
APP_B = "https://brave-otter-2m9q.delimitusapps.com"
ORG = "org_0123456789abcdefghij"
APP_A_ID = "app_aaaaaaaaaaaaaaaaaaaa"
USER = "usr_11111111111111111111"
SCHEDULE = "sch_22222222222222222222"
GROUPS = tuple(f"grp_{i:020d}" for i in range(1, 4))
KID_A, KID_B, KID_FOREIGN = "test-2026-09-a", "test-2026-09-b", "test-foreign"


def _load_keys() -> dict[str, EllipticCurvePrivateKey]:
    raw = json.loads(KEYS_FILE.read_text())["keys"]
    out: dict[str, EllipticCurvePrivateKey] = {}
    for kid, jwk in raw.items():
        key = ECAlgorithm.from_jwk(jwk)
        assert isinstance(key, EllipticCurvePrivateKey)
        out[kid] = key
    return out


def _b64(obj: dict[str, Any]) -> str:
    data = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _user_note(**overrides: Any) -> IdentityNote:
    base: dict[str, Any] = {
        "issuer": ISSUER,
        "audience": APP_A,
        "subject": USER,
        "org": ORG,
        "app": APP_A_ID,
        "env": "prod",
        "role": "user",
        "now": NOW - 10,
        "groups": GROUPS,
        "name": "Ada Lovelace",
        "email": "ada@example.com",
    }
    base.update(overrides)
    return compose_note(**base)


def _raw_token(claims: dict[str, Any], key: EllipticCurvePrivateKey, header: dict[str, Any]) -> str:
    """Sign arbitrary claims with an arbitrary header (for notes the model would refuse)."""
    return jwt.api_jws.encode(
        json.dumps(claims, separators=(",", ":")).encode(),
        key,
        algorithm=IDENTITY_ALG,
        headers=header,
    )


def build_vectors() -> dict[str, Any]:  # noqa: PLR0915
    keys = _load_keys()
    key_a, key_b, foreign = keys[KID_A], keys[KID_B], keys[KID_FOREIGN]
    header = {"alg": IDENTITY_ALG, "typ": IDENTITY_TYP, "kid": KID_A}
    cases: list[dict[str, Any]] = []

    def ok(name: str, token: str, audience: str, note: IdentityNote) -> None:
        cases.append(
            {
                "name": name,
                "token": token,
                "audience": audience,
                "expect": {"ok": note_claims(note)},
            }
        )

    def refused(name: str, token: str, code: str, audience: str = APP_A) -> None:
        cases.append(
            {"name": name, "token": token, "audience": audience, "expect": {"refused": code}}
        )

    user = _user_note()
    ok("user note", sign_note(user, private_key=key_a, kid=KID_A), APP_A, user)
    ok(
        "user note signed by the second key",
        sign_note(user, private_key=key_b, kid=KID_B),
        APP_A,
        user,
    )
    builder = _user_note(role="builder", env="preview", groups=())
    ok(
        "builder note on preview, no groups",
        sign_note(builder, private_key=key_a, kid=KID_A),
        APP_A,
        builder,
    )
    sched = compose_note(
        issuer=ISSUER,
        audience=APP_A,
        subject=SCHEDULE,
        org=ORG,
        app=APP_A_ID,
        env="prod",
        role="schedule",
        now=NOW - 10,
    )
    ok("schedule note", sign_note(sched, private_key=key_a, kid=KID_A), APP_A, sched)
    many = _user_note(groups=tuple(f"grp_{i:020d}" for i in range(MAX_GROUPS)))
    ok("note with 50 groups", sign_note(many, private_key=key_a, kid=KID_A), APP_A, many)
    fresh = _user_note(now=NOW)
    ok("note issued this second", sign_note(fresh, private_key=key_a, kid=KID_A), APP_A, fresh)

    refused(
        "token for app A verified by app B",
        sign_note(user, private_key=key_a, kid=KID_A),
        "wrong_audience",
        APP_B,
    )
    refused(
        "expired",
        sign_note(_user_note(now=NOW - TTL_SECONDS - 60), private_key=key_a, kid=KID_A),
        "expired",
    )
    refused(
        "expired by one second past leeway",
        sign_note(_user_note(now=NOW - TTL_SECONDS - 31), private_key=key_a, kid=KID_A),
        "expired",
    )
    refused(
        "not yet valid",
        sign_note(_user_note(now=NOW + 120), private_key=key_a, kid=KID_A),
        "not_yet_valid",
    )
    refused(
        "wrong key claiming a known kid",
        sign_note(user, private_key=foreign, kid=KID_A),
        "bad_signature",
    )
    refused("unknown kid", sign_note(user, private_key=foreign, kid=KID_FOREIGN), "unknown_key")
    refused(
        "missing kid",
        _raw_token(note_claims(user), key_a, {"alg": IDENTITY_ALG, "typ": IDENTITY_TYP}),
        "unknown_key",
    )
    good = sign_note(user, private_key=key_a, kid=KID_A)
    h, _, s = good.split(".")
    tampered = dict(note_claims(user), role="builder")
    refused("tampered payload", f"{h}.{_b64(tampered)}.{s}", "bad_signature")
    refused(
        "alg none",
        f"{_b64({'alg': 'none', 'typ': IDENTITY_TYP, 'kid': KID_A})}.{_b64(note_claims(user))}.",
        "wrong_algorithm",
    )
    hs = jwt.api_jws.encode(
        json.dumps(note_claims(user)).encode(),
        "not-a-secret-just-thirty-two-bytes!",
        algorithm="HS256",
        headers={"typ": IDENTITY_TYP, "kid": KID_A},
    )
    refused("HS256", hs, "wrong_algorithm")
    refused(
        "wrong typ",
        _raw_token(note_claims(user), key_a, {"alg": IDENTITY_ALG, "typ": "JWT", "kid": KID_A}),
        "wrong_type",
    )
    refused(
        "no typ",
        _raw_token(note_claims(user), key_a, {"alg": IDENTITY_ALG, "kid": KID_A}),
        "wrong_type",
    )
    long_ttl = dict(note_claims(user), exp=NOW - 10 + TTL_SECONDS + 1)
    refused("ttl over 300 seconds", _raw_token(long_ttl, key_a, header), "ttl_too_long")
    refused(
        "51 groups",
        _raw_token(
            dict(note_claims(user), groups=[f"grp_{i:020d}" for i in range(MAX_GROUPS + 1)]),
            key_a,
            header,
        ),
        "bad_claims",
    )
    refused(
        "email as subject",
        _raw_token(dict(note_claims(user), sub="ada@example.com"), key_a, header),
        "bad_claims",
    )
    refused(
        "schedule note carrying email",
        _raw_token(dict(note_claims(sched), email="cron@example.com"), key_a, header),
        "bad_claims",
    )
    refused(
        "schedule note carrying name",
        _raw_token(dict(note_claims(sched), name="Nightly"), key_a, header),
        "bad_claims",
    )
    refused(
        "user note with role schedule",
        _raw_token(dict(note_claims(user), role="schedule"), key_a, header),
        "bad_claims",
    )
    refused(
        "unknown role",
        _raw_token(dict(note_claims(user), role="admin"), key_a, header),
        "bad_claims",
    )
    refused(
        "unknown claim",
        _raw_token(dict(note_claims(user), admin=True), key_a, header),
        "bad_claims",
    )
    refused(
        "group that is not a grp_ id",
        _raw_token(dict(note_claims(user), groups=["Finance"]), key_a, header),
        "bad_claims",
    )
    refused(
        "missing org claim",
        _raw_token({k: v for k, v in note_claims(user).items() if k != "org"}, key_a, header),
        "bad_claims",
    )
    refused(
        "iat as string",
        _raw_token(dict(note_claims(user), iat=str(NOW - 10)), key_a, header),
        "bad_claims",
    )
    refused(
        "audience with trailing slash",
        _raw_token(dict(note_claims(user), aud=APP_A + "/"), key_a, header),
        "wrong_audience",
    )
    refused("not a token", "hello", "malformed")
    refused("two segments", f"{h}.{_b64(note_claims(user))}", "malformed")
    refused("empty", "", "missing")

    return {
        "_comment": "Shared identity-note test vectors (SSC-020). Regenerate with "
        "`uv run python conformance/identity_note/generate.py`. "
        "Keys in test_keys.json are throwaway.",
        "now": NOW,
        "leeway": 30,
        "issuer": ISSUER,
        "audiences": {"app_a": APP_A, "app_b": APP_B},
        "jwks": jwks((key_a.public_key(), KID_A), (key_b.public_key(), KID_B)),
        "cases": cases,
    }


def main() -> int:
    VECTORS_FILE.write_text(json.dumps(build_vectors(), indent=2) + "\n")
    sys.stdout.write(f"wrote {VECTORS_FILE}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
