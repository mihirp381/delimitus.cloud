"""Who is calling: an ES256 credential of type ``ssc-api+jwt``, verified against our own keys.

Issuing credentials is SSC-018 (people, through the auth host) and SSC-022 (the command line).
This module only verifies. Every failure is ``UNAUTHENTICATED`` with the reason in the log, never
in the body: telling an attacker *why* a token failed is telling them what to fix.

Claims we require: ``iss``, ``aud``, ``sub``, ``iat``, ``exp``, ``jti`` (the credential id, which
rate limits and idempotency keys are scoped to), ``org`` (an ``org_…`` id) and ``kind``. Optional:
``agent`` (true when an agent acts for the subject), ``client_id`` (which agent) and ``scope``
(``preview``: the credential never touches production, enforced in ``uow``; any other value is
refused) and ``sid`` (the auth-host session the credential was issued from, SSC-019: ``uow``
refuses it once that session is revoked or its person deactivated).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Final

import jwt
from fastapi import Depends, Request
from jwt import PyJWKSet

from ssc_contracts.errors import ErrorCode
from ssc_control.api.problems import Refusal
from ssc_control.api.runtime import runtime_of
from ssc_control.db.bind import check_org_id

API_TOKEN_TYP: Final = "ssc-api+jwt"  # noqa: S105  (a JOSE typ, not a secret)
ALGORITHM: Final = "ES256"
REQUIRED_CLAIMS: Final = ("iss", "aud", "sub", "iat", "exp", "jti", "org", "kind")


class PrincipalKind(StrEnum):
    USER = "user"
    WORKLOAD = "workload"
    OPERATOR = "operator"


class CredentialScope(StrEnum):
    PREVIEW = "preview"


@dataclass(frozen=True, slots=True)
class Principal:
    org_id: str
    subject: str
    kind: PrincipalKind
    credential_id: str
    is_agent: bool = False
    client_id: str | None = None
    scope: CredentialScope | None = None
    """``None``: everything the subject may do. ``PREVIEW``: never production."""
    session_id: str | None = None
    """The ``ses_`` session the credential came from; checked live on every request."""


class Verifier:
    def __init__(self, jwks: dict[str, Any], issuer: str) -> None:
        self._keys: PyJWKSet | None = PyJWKSet.from_dict(jwks) if jwks.get("keys") else None
        self._issuer = issuer

    def verify(self, token: str, audience: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise Refusal(ErrorCode.UNAUTHENTICATED, evidence={"reason": type(e).__name__}) from e
        if header.get("typ") != API_TOKEN_TYP or header.get("alg") != ALGORITHM:
            raise Refusal(
                ErrorCode.UNAUTHENTICATED,
                evidence={
                    "reason": "typ_or_alg",
                    "typ": header.get("typ"),
                    "alg": header.get("alg"),
                },
            )
        kid = header.get("kid")
        if self._keys is None or not isinstance(kid, str):
            raise Refusal(ErrorCode.UNAUTHENTICATED, evidence={"reason": "no_key", "kid": kid})
        try:
            key = self._keys[kid]
        except KeyError as e:
            raise Refusal(ErrorCode.UNAUTHENTICATED, evidence={"reason": "unknown_kid"}) from e
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=[ALGORITHM],
                audience=audience,
                issuer=self._issuer,
                options={"require": list(REQUIRED_CLAIMS)},
                leeway=5,
            )
        except jwt.PyJWTError as e:
            raise Refusal(ErrorCode.UNAUTHENTICATED, evidence={"reason": type(e).__name__}) from e
        return principal_from_claims(claims)


def principal_from_claims(claims: dict[str, Any]) -> Principal:
    try:
        org_id = check_org_id(str(claims["org"]))
        kind = PrincipalKind(str(claims["kind"]))
        scope = None if claims.get("scope") is None else CredentialScope(claims["scope"])
    except ValueError as e:
        raise Refusal(ErrorCode.UNAUTHENTICATED, evidence={"reason": "bad_claim"}) from e
    client_id = claims.get("client_id")
    sid = claims.get("sid")
    if sid is not None and (not isinstance(sid, str) or not sid.startswith("ses_")):
        raise Refusal(ErrorCode.UNAUTHENTICATED, evidence={"reason": "bad_claim"})
    return Principal(
        org_id=org_id,
        subject=str(claims["sub"]),
        kind=kind,
        credential_id=str(claims["jti"]),
        is_agent=bool(claims.get("agent", False)),
        client_id=str(client_id) if client_id is not None else None,
        scope=scope,
        session_id=sid,
    )


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Refusal(ErrorCode.UNAUTHENTICATED, evidence={"reason": "no_bearer"})
    return token.strip()


def _verifier(request: Request) -> tuple[Verifier, str, str]:
    rt = runtime_of(request)
    return rt.verifier, rt.settings.user_audience, rt.settings.internal_audience


def user_principal(request: Request) -> Principal:
    verifier, audience, _ = _verifier(request)
    return verifier.verify(bearer_token(request), audience)


def internal_principal(request: Request) -> Principal:
    verifier, _, audience = _verifier(request)
    principal = verifier.verify(bearer_token(request), audience)
    if principal.kind is PrincipalKind.USER:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"reason": "user_on_internal"})
    return principal


UserPrincipal = Annotated[Principal, Depends(user_principal)]
InternalPrincipal = Annotated[Principal, Depends(internal_principal)]
