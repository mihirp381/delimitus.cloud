"""Read the identity note the SSC gateway attaches to every request as ``X-SSC-Identity``.

    from ssc_app.identity import IdentityVerifier

    verifier = IdentityVerifier(audience="https://quiet-river-7f3k.delimitusapps.com",
                                keys="https://keys.delimitus.com/cell-01/jwks.json")
    note = verifier.from_headers(request.headers)
    who = note.sub          # key on this
    label = note.name       # display only

Two rules:

* **Key on ``sub``, never on ``email``.** ``sub`` is a stable ``usr_`` or ``sch_`` id. ``email`` and
  ``name`` are display strings the company directory can change, and a schedule note has neither.
* **Verify once, when the request arrives.** Do not re-verify inside a long WebSocket or event
  stream; the note expires after five minutes and ending open streams is the platform kill
  switch's job. Re-verifying mid-stream only breaks the stream.

Every refusal is an :class:`IdentityRefused` carrying one code from :data:`REFUSAL_CODES`. Treat
all of them the same way: the request is not from a signed-in user of this app.
"""

import base64
import json
import time
from collections.abc import Mapping
from typing import Any, Final, Literal, cast
from urllib.parse import unquote_to_bytes

import jwt
from pydantic import ValidationError

from ssc_contracts.identity import (
    IDENTITY_ALG,
    IDENTITY_HEADER,
    IDENTITY_TYP,
    MAX_TTL_SECONDS,
    IdentityNote,
)

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
REFUSAL_CODES: Final[frozenset[str]] = frozenset(
    (
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
    )
)
DEFAULT_LEEWAY_SECONDS: Final = 30

KeySource = Mapping[str, Any] | jwt.PyJWKSet | jwt.PyJWKClient


class IdentityRefused(Exception):  # noqa: N818  (public name; refusal, not a bug)
    """The note was absent or did not verify. ``code`` says why; the message is for logs."""

    code: RefusalCode

    def __init__(self, code: RefusalCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _signing_key(keys: KeySource, kid: str) -> jwt.PyJWK:
    try:
        if isinstance(keys, jwt.PyJWKClient):
            return keys.get_signing_key(kid)
        key_set = keys if isinstance(keys, jwt.PyJWKSet) else jwt.PyJWKSet.from_dict(dict(keys))
        return key_set[kid]
    except (KeyError, jwt.PyJWKClientError, jwt.PyJWKSetError, jwt.PyJWKError) as e:
        raise IdentityRefused("unknown_key", f"no key {kid!r} in the JWKS") from e


def _int_claim(claims: Mapping[str, Any], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise IdentityRefused("bad_claims", f"{name} is not an integer")
    return value


def verify(  # noqa: PLR0913  (keyword-only)
    token: str | None,
    *,
    audience: str,
    keys: KeySource,
    issuer: str | None = None,
    now: int | None = None,
    leeway: int = DEFAULT_LEEWAY_SECONDS,
) -> IdentityNote:
    """Verify one note and return its claims, or raise :class:`IdentityRefused`.

    ``audience`` is this app's exact origin, ``https://host`` with no path. A note minted for
    another app is refused with ``wrong_audience`` even though the same cell signed it.
    ``keys`` is the cell JWKS as a dict, a :class:`jwt.PyJWKSet`, or a :class:`jwt.PyJWKClient`
    pointed at ``<issuer>/jwks.json``. ``now`` (Unix seconds) is for tests; default is the clock.
    """
    if token is None or token == "":
        raise IdentityRefused("missing", f"no {IDENTITY_HEADER} header")
    if not isinstance(token, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise IdentityRefused("malformed", "token is not a string")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.DecodeError as e:
        raise IdentityRefused("malformed", str(e)) from e
    if header.get("typ") != IDENTITY_TYP:
        raise IdentityRefused("wrong_type", f"typ is {header.get('typ')!r}, not {IDENTITY_TYP!r}")
    if header.get("alg") != IDENTITY_ALG:
        raise IdentityRefused("wrong_algorithm", f"alg is {header.get('alg')!r}")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise IdentityRefused("unknown_key", "no kid in the header")
    key = _signing_key(keys, kid)
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=[IDENTITY_ALG],
            options={
                "verify_signature": True,
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "verify_aud": False,
                "verify_iss": False,
                "verify_sub": False,
            },
        )
    except jwt.InvalidSignatureError as e:
        raise IdentityRefused("bad_signature", "signature does not verify") from e
    except jwt.InvalidAlgorithmError as e:
        raise IdentityRefused("wrong_algorithm", str(e)) from e
    except jwt.PyJWTError as e:
        raise IdentityRefused("malformed", str(e)) from e
    return check_claims(claims, audience=audience, issuer=issuer, now=now, leeway=leeway)


def check_claims(
    claims: Mapping[str, Any],
    *,
    audience: str,
    issuer: str | None,
    now: int | None,
    leeway: int,
) -> IdentityNote:
    """The claim checks, after the signature. Split out so the order is readable and testable."""
    if claims.get("aud") != audience:
        raise IdentityRefused("wrong_audience", f"note is for {claims.get('aud')!r}")
    if issuer is not None and claims.get("iss") != issuer:
        raise IdentityRefused("wrong_issuer", f"note is from {claims.get('iss')!r}")
    iat = _int_claim(claims, "iat")
    exp = _int_claim(claims, "exp")
    if exp - iat > MAX_TTL_SECONDS:
        raise IdentityRefused("ttl_too_long", f"exp - iat is {exp - iat}s")
    at = _now() if now is None else now
    if at >= exp + leeway:
        raise IdentityRefused("expired", f"expired {at - exp}s ago")
    if iat > at + leeway:
        raise IdentityRefused("not_yet_valid", f"issued {iat - at}s in the future")
    try:
        return IdentityNote.model_validate(dict(claims))
    except ValidationError as e:
        raise IdentityRefused("bad_claims", "; ".join(err["msg"] for err in e.errors())) from e


def _now() -> int:
    return int(time.time())


def token_from_headers(headers: Mapping[str, str]) -> str | None:
    """Pull the note from a request's headers, whatever the case of the header name."""
    wanted = IDENTITY_HEADER.lower()
    for name, value in headers.items():
        if name.lower() == wanted:
            return value
    return None


def _inline_jwks(url: str) -> dict[str, Any]:
    header, _, payload = url.removeprefix("data:").partition(",")
    raw = base64.b64decode(payload) if header.endswith(";base64") else unquote_to_bytes(payload)
    return cast(dict[str, Any], json.loads(raw))


class IdentityVerifier:
    """One verifier per app process. Holds the audience and the key source.

    ``keys`` is the JWKS URL (``<issuer>/jwks.json``; fetched and cached, refreshed when an
    unknown ``kid`` shows up during a key rotation), the JWKS inline as a ``data:`` URL (how the
    platform hands it to an app, which has no internet), or an already loaded JWKS dict.
    """

    def __init__(
        self,
        *,
        audience: str,
        keys: str | Mapping[str, Any],
        issuer: str | None = None,
        leeway: int = DEFAULT_LEEWAY_SECONDS,
    ) -> None:
        self.audience = audience
        self.issuer = issuer
        self.leeway = leeway
        if isinstance(keys, str) and keys.startswith("data:"):
            keys = _inline_jwks(keys)
        self._keys: KeySource = (
            jwt.PyJWKClient(keys, cache_keys=True, lifespan=300)
            if isinstance(keys, str)
            else jwt.PyJWKSet.from_dict(dict(keys))
        )

    def verify(self, token: str | None, *, now: int | None = None) -> IdentityNote:
        return verify(
            token,
            audience=self.audience,
            keys=self._keys,
            issuer=self.issuer,
            now=now,
            leeway=self.leeway,
        )

    def from_headers(self, headers: Mapping[str, str], *, now: int | None = None) -> IdentityNote:
        """Verify the note in ``headers``; ``missing`` when the gateway did not attach one."""
        return self.verify(token_from_headers(headers), now=now)


def refusal_code(error: BaseException) -> str | None:
    """The code of an :class:`IdentityRefused`, or None for anything else."""
    return cast(str, error.code) if isinstance(error, IdentityRefused) else None
