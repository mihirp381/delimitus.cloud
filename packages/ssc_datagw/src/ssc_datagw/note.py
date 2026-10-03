"""The user's identity note, forwarded by the app as ``X-SSC-Identity`` (SSC-050).

The same note the gateway attached to the request the app is answering
(``docs/contracts/identity-note.md``), checked as the app checks it: ES256 under the cell's
JWKS, ``typ`` ``ssc-id+jwt``, the calling app's origin as ``aud``, at most five minutes long.
``ssc_app.identity`` is the app's copy; a cell service may not import it, so this one runs the
same conformance vectors. The caller then binds the note to the calling environment.
"""

import time
from collections.abc import Mapping
from typing import Any, Final, Literal

import jwt
from jwt import PyJWKSet
from pydantic import ValidationError

from ssc_contracts.identity import IDENTITY_ALG, IDENTITY_TYP, MAX_TTL_SECONDS, IdentityNote

LEEWAY_SECONDS: Final = 30

RefusalCode = Literal[
    "missing",
    "malformed",
    "wrong_type",
    "wrong_algorithm",
    "unknown_key",
    "bad_signature",
    "wrong_audience",
    "wrong_issuer",
    "expired",
    "not_yet_valid",
    "ttl_too_long",
    "bad_claims",
]


class NoteRefusedError(Exception):
    """The note did not verify; ``code`` says why and the message is for logs."""

    def __init__(self, code: RefusalCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code: RefusalCode = code


def _int_claim(claims: Mapping[str, Any], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise NoteRefusedError("bad_claims", f"{name} is not an integer")
    return value


def _claims(token: str, keys: PyJWKSet) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.DecodeError as exc:
        raise NoteRefusedError("malformed", str(exc)) from exc
    if header.get("typ") != IDENTITY_TYP:
        raise NoteRefusedError("wrong_type", f"typ is {header.get('typ')!r}")
    if header.get("alg") != IDENTITY_ALG:
        raise NoteRefusedError("wrong_algorithm", f"alg is {header.get('alg')!r}")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise NoteRefusedError("unknown_key", "no kid in the header")
    try:
        key = keys[kid]
    except KeyError as exc:
        raise NoteRefusedError("unknown_key", f"no key {kid!r} in the JWKS") from exc
    try:
        return jwt.decode(
            token,
            key,
            algorithms=[IDENTITY_ALG],
            options={
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "verify_aud": False,
                "verify_iss": False,
                "verify_sub": False,
            },
        )
    except jwt.InvalidSignatureError as exc:
        raise NoteRefusedError("bad_signature", "signature does not verify") from exc
    except jwt.InvalidAlgorithmError as exc:
        raise NoteRefusedError("wrong_algorithm", str(exc)) from exc
    except jwt.PyJWTError as exc:
        raise NoteRefusedError("malformed", str(exc)) from exc


def verify_note(  # noqa: PLR0913  (keyword-only)
    token: str | None,
    *,
    audience: str,
    keys: PyJWKSet,
    issuer: str | None,
    now: int | None = None,
    leeway: int = LEEWAY_SECONDS,
) -> IdentityNote:
    """The note's claims, or :class:`NoteRefusedError`. ``now`` (Unix seconds) is for tests."""
    if not token:
        raise NoteRefusedError("missing", "no note")
    claims = _claims(token, keys)
    if claims.get("aud") != audience:
        raise NoteRefusedError("wrong_audience", f"note is for {claims.get('aud')!r}")
    if issuer is not None and claims.get("iss") != issuer:
        raise NoteRefusedError("wrong_issuer", f"note is from {claims.get('iss')!r}")
    iat, exp = _int_claim(claims, "iat"), _int_claim(claims, "exp")
    if exp - iat > MAX_TTL_SECONDS:
        raise NoteRefusedError("ttl_too_long", f"exp - iat is {exp - iat}s")
    at = int(time.time()) if now is None else now
    if at >= exp + leeway:
        raise NoteRefusedError("expired", f"expired {at - exp}s ago")
    if iat > at + leeway:
        raise NoteRefusedError("not_yet_valid", f"issued {iat - at}s in the future")
    try:
        return IdentityNote.model_validate(claims)
    except ValidationError as exc:
        raise NoteRefusedError("bad_claims", "; ".join(e["msg"] for e in exc.errors())) from exc
