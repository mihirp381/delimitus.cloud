"""The gateway's keys: session keys and the identity-note signing key, in one JSON keyring.

Cell identities may not read Secret Manager values (decision 022's deny rules), so the keyring
reaches the gateway as Cloud KMS ciphertext in ``SSC_GATEWAY_KEYRING`` (base64), decrypted once
at start with ``SSC_GATEWAY_KMS_KEY``, a key only the gateway may decrypt with. The plain
keyring is accepted only when ``SSC_ENV`` is ``dev`` or ``test``.

Keyring JSON: ``{"session": {kid: base64 32 bytes}, "session_kid": kid,
"identity": {kid: PKCS#8 PEM, EC P-256}, "identity_kid": kid}``. Two keys of a kind are held
during a rotation; the ``*_kid`` one is used for new values.
"""

import base64
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

import httpx2
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

KMS_API: Final = "https://cloudkms.googleapis.com/v1"
KEY_BYTES: Final = 32


class KeyringError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Keyring:
    session: Mapping[str, bytes]
    session_kid: str
    identity: Mapping[str, ec.EllipticCurvePrivateKey]
    identity_kid: str

    @property
    def signing_key(self) -> ec.EllipticCurvePrivateKey:
        return self.identity[self.identity_kid]


def parse_keyring(raw: bytes) -> Keyring:
    try:
        doc = cast(dict[str, object], json.loads(raw))
        session = {
            str(k): base64.b64decode(str(v), validate=True)
            for k, v in cast(dict[str, object], doc["session"]).items()
        }
        identity = {
            str(k): serialization.load_pem_private_key(str(v).encode(), password=None)
            for k, v in cast(dict[str, object], doc["identity"]).items()
        }
        session_kid, identity_kid = str(doc["session_kid"]), str(doc["identity_kid"])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise KeyringError("the gateway keyring is not valid JSON of the documented shape") from exc
    if session_kid not in session or identity_kid not in identity:
        raise KeyringError("the active key ids must name keys in the keyring")
    if any(len(k) != KEY_BYTES for k in session.values()):
        raise KeyringError("session keys are 32 bytes")
    p256: dict[str, ec.EllipticCurvePrivateKey] = {}
    for kid, key in identity.items():
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise KeyringError("identity keys are EC P-256")
        p256[kid] = key
    return Keyring(session, session_kid, p256, identity_kid)


def new_keyring(*, session_kid: str = "s1", identity_kid: str = "i1") -> bytes:
    """A fresh keyring, for bootstrap tooling and tests."""
    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    doc = {
        "session": {session_kid: base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode()},
        "session_kid": session_kid,
        "identity": {identity_kid: pem},
        "identity_kid": identity_kid,
    }
    return json.dumps(doc).encode()


async def kms_decrypt(
    key_name: str,
    ciphertext_b64: str,
    *,
    access_token: str,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> bytes:
    """Cloud KMS ``decrypt`` of one ciphertext under ``key_name`` (a full CryptoKey name)."""
    async with httpx2.AsyncClient(timeout=10, transport=transport) as client:
        r = await client.post(
            f"{KMS_API}/{key_name}:decrypt",
            json={"ciphertext": ciphertext_b64},
            headers={"authorization": f"Bearer {access_token}"},
        )
    if r.status_code != 200:  # noqa: PLR2004
        raise KeyringError(f"KMS decrypt failed with HTTP {r.status_code}")
    return base64.b64decode(cast(dict[str, str], r.json())["plaintext"])
