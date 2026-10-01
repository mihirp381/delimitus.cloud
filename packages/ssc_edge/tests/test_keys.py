"""The gateway keyring (SSC-018, decision 010 amended): one JSON document, KMS-wrapped."""

import base64
import json

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from ssc_edge.keys import KeyringError, kms_decrypt, new_keyring, parse_keyring

KEY = "projects/p/locations/us-central1/keyRings/r/cryptoKeys/gateway"


def test_a_new_keyring_parses() -> None:
    ring = parse_keyring(new_keyring())
    assert ring.session_kid == "s1" and len(ring.session["s1"]) == 32
    assert ring.identity_kid == "i1" and ring.signing_key.curve.name == "secp256r1"


def _doc(**changes: object) -> bytes:
    doc = json.loads(new_keyring())
    doc.update(changes)
    return json.dumps(doc).encode()


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        _doc(session_kid="nope"),
        _doc(identity_kid="nope"),
        _doc(session={"s1": base64.b64encode(b"short").decode()}),
        _doc(session={"s1": "%%%"}),
        _doc(identity={"i1": "not a pem"}),
        _doc(
            identity={
                "i1": ec.generate_private_key(ec.SECP384R1())
                .private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
                .decode()
            }
        ),
    ],
)
def test_a_bad_keyring_is_refused(raw: bytes) -> None:
    with pytest.raises(KeyringError):
        parse_keyring(raw)


async def test_kms_decrypt() -> None:
    plain = new_keyring()
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if json.loads(request.content) != {"ciphertext": "Y2lwaGVy"}:
            return httpx2.Response(400)
        return httpx2.Response(200, json={"plaintext": base64.b64encode(plain).decode()})

    got = await kms_decrypt(
        KEY, "Y2lwaGVy", access_token="tok", transport=httpx2.MockTransport(handler)
    )
    assert got == plain
    (request,) = seen
    assert str(request.url) == f"https://cloudkms.googleapis.com/v1/{KEY}:decrypt"
    assert request.headers["authorization"] == "Bearer tok"


async def test_a_kms_refusal_is_a_keyring_error() -> None:
    transport = httpx2.MockTransport(lambda _: httpx2.Response(403, json={"error": {}}))
    with pytest.raises(KeyringError, match="403"):
        await kms_decrypt(KEY, "Y2lwaGVy", access_token="tok", transport=transport)
