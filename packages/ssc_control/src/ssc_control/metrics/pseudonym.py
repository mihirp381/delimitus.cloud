"""End users in metrics appear only as a keyed pseudonym, one key per org.

``org_key = HMAC-SHA256(master, b"ssc-metrics-v1\\0" + org_id)`` and
``pseudonym = HMAC-SHA256(org_key, user_id)``, hex, first 32 characters. The master key is
``SSC_METRICS_KEY`` (base64 of 32 bytes), held outside the database (in the API's environment;
Secret Manager once SSC-013 binds it). Without it a pseudonym cannot be linked to a person.

Never rotate the master key without a window that writes both pseudonyms: a new key makes every
person look new, which breaks every longitudinal count.
"""

import base64
import binascii
import hashlib
import hmac
import re
from typing import Final, Protocol

KEY_BYTES: Final = 32
PSEUDONYM_CHARS: Final = 32
_CONTEXT: Final = b"ssc-metrics-v1\0"
_ORG_ID: Final = re.compile(r"org_[a-z0-9]{20}")
_USER_ID: Final = re.compile(r"usr_[a-z0-9]{20}")


class MetricsKeyError(ValueError):
    """The metrics key is missing or malformed. The message never contains the key."""


def parse_master_key(value: str | None) -> bytes:
    """``SSC_METRICS_KEY`` to bytes: standard base64 of exactly 32 bytes."""
    if value is None or not value.strip():
        raise MetricsKeyError("SSC_METRICS_KEY is not set")
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except binascii.Error, ValueError:
        raise MetricsKeyError("SSC_METRICS_KEY is not valid base64") from None
    if len(key) != KEY_BYTES:
        raise MetricsKeyError(f"SSC_METRICS_KEY must decode to {KEY_BYTES} bytes")
    return key


class PseudonymKeys(Protocol):
    def org_key(self, org_id: str) -> bytes:
        """The key that pseudonymises the users of ``org_id``."""
        ...


class DerivedKeys(PseudonymKeys):
    """Per-org keys derived from one master key."""

    def __init__(self, master: bytes) -> None:
        if len(master) != KEY_BYTES:
            raise MetricsKeyError(f"the metrics master key must be {KEY_BYTES} bytes")
        self._master = master

    def org_key(self, org_id: str) -> bytes:
        if not _ORG_ID.fullmatch(org_id):
            raise ValueError("not an org id")
        return hmac.new(self._master, _CONTEXT + org_id.encode(), hashlib.sha256).digest()

    def __repr__(self) -> str:
        return "DerivedKeys(<redacted>)"


def pseudonym(keys: PseudonymKeys, org_id: str, user_id: str) -> str:
    """The stable, org-scoped pseudonym of ``user_id`` (a ``usr_`` id)."""
    if not _USER_ID.fullmatch(user_id):
        raise ValueError("metrics pseudonymise usr_ ids only")
    digest = hmac.new(keys.org_key(org_id), user_id.encode(), hashlib.sha256).hexdigest()
    return digest[:PSEUDONYM_CHARS]
