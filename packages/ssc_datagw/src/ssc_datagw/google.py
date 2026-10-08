"""A Google service account's JSON key, and the self-signed JWTs it makes (GA-5 B3, B5).

A Google API takes a JWT the service account signs itself, with the API's own URL as its
``aud`` in place of an OAuth scope: no token endpoint, no cache. The Google Sheets and BigQuery
connectors share this.

The key, the email, the JSON and each JWT are never in a repr, an error or a log line: a key
that does not parse is refused with :data:`NOT_A_KEY`, which quotes none of it.
"""

import json
import time
from dataclasses import dataclass
from typing import Annotated, Final, cast

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import AfterValidator, SecretStr

TOKEN_SECONDS: Final = 3600
NOT_A_KEY: Final = (
    "the service account is not a JSON key with client_email, private_key and private_key_id"
)


@dataclass(frozen=True, slots=True, repr=False)
class Signer:
    """The three parts of the key a read needs. No repr: each part is the credential's."""

    email: str
    kid: str
    key: rsa.RSAPrivateKey

    def token(self, audience: str) -> str:
        """A JWT for the API at ``audience``, valid :data:`TOKEN_SECONDS`."""
        now = int(time.time())
        claims = {
            "iss": self.email,
            "sub": self.email,
            "aud": audience,
            "iat": now,
            "exp": now + TOKEN_SECONDS,
        }
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})


def _parsed(raw: str) -> Signer | None:
    try:
        account = json.loads(raw)
        if not isinstance(account, dict):
            return None
        fields = cast("dict[str, object]", account)
        email, pem, kid = (
            fields.get("client_email"),
            fields.get("private_key"),
            fields.get("private_key_id"),
        )
        if not (isinstance(email, str) and isinstance(pem, str) and isinstance(kid, str)):
            return None
        if not (email and pem and kid):
            return None
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except ValueError, TypeError, RecursionError, UnsupportedAlgorithm:
        return None
    if not isinstance(key, rsa.RSAPrivateKey):
        return None
    return Signer(email, kid, key)


def signer(service_account: SecretStr) -> Signer:
    """The parsed key, or a ``ValueError`` that quotes nothing of it (raised outside the
    ``except`` so it carries no parser error, which holds the text)."""
    parsed = _parsed(service_account.get_secret_value())
    if parsed is None:
        raise ValueError(NOT_A_KEY)
    return parsed


def _is_a_key(value: SecretStr) -> SecretStr:
    signer(value)
    return value


type ServiceAccount = Annotated[SecretStr, AfterValidator(_is_a_key)]
"""A target's ``service_account``: the key file's JSON text, refused unless it is a key."""
