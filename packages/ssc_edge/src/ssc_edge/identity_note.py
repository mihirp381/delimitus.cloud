"""Mint identity notes. The gateway (SSC-018) is the only legitimate caller.

The signing key is an EC P-256 private key that lives in the cell's Secret Manager and is loaded
into the gateway process at start; it is never written to a snapshot, a log or the control
database. Its public half is published as a JWKS at ``<issuer>/jwks.json`` (SSC-013), which is
what ``ssc_app.identity`` and the Node helper read.
"""

from typing import Any, Final

import jwt
from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurvePrivateKey,
    EllipticCurvePublicKey,
)
from jwt.algorithms import ECAlgorithm

from ssc_contracts.identity import (
    IDENTITY_ALG,
    IDENTITY_TYP,
    MAX_TTL_SECONDS,
    EnvironmentName,
    IdentityNote,
    Role,
)

TTL_SECONDS: Final = MAX_TTL_SECONDS


def compose_note(  # noqa: PLR0913  (keyword-only, one per claim)
    *,
    issuer: str,
    audience: str,
    subject: str,
    org: str,
    app: str,
    env: EnvironmentName,
    role: Role,
    now: int,
    groups: tuple[str, ...] = (),
    name: str | None = None,
    email: str | None = None,
) -> IdentityNote:
    """Build the claim set for one request. ``exp`` is always ``iat + 300``."""
    return IdentityNote(
        iss=issuer,
        aud=audience,
        sub=subject,
        iat=now,
        exp=now + TTL_SECONDS,
        org=org,
        app=app,
        env=env,
        role=role,
        groups=groups,
        name=name,
        email=email,
    )


def note_claims(note: IdentityNote) -> dict[str, Any]:
    """The JSON claim set: absent optional claims are left out, never sent as null."""
    claims = note.model_dump(exclude_none=True)
    claims["groups"] = list(note.groups)
    return claims


def sign_note(note: IdentityNote, *, private_key: EllipticCurvePrivateKey, kid: str) -> str:
    """Return the compact JWS for ``note``: ``alg`` ES256, ``typ`` ssc-id+jwt, ``kid`` set."""
    return jwt.encode(
        note_claims(note),
        private_key,
        algorithm=IDENTITY_ALG,
        headers={"kid": kid, "typ": IDENTITY_TYP},
    )


def public_jwk(public_key: EllipticCurvePublicKey, *, kid: str) -> dict[str, Any]:
    """One JWKS entry for a signing key: public half only, tagged with its ``kid``."""
    jwk = ECAlgorithm.to_jwk(public_key, as_dict=True)
    jwk.update({"kid": kid, "use": "sig", "alg": IDENTITY_ALG})
    return jwk


def jwks(*keys: tuple[EllipticCurvePublicKey, str]) -> dict[str, Any]:
    """The JWKS document to publish. Holds up to two keys during a rotation."""
    return {"keys": [public_jwk(key, kid=kid) for key, kid in keys]}
