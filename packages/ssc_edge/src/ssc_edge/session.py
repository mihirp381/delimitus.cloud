"""The platform session cookie on an app host, and the one place a ``Set-Cookie`` is built.

``__Host-ssc-session`` is host-only (the ``__Host-`` prefix makes the browser refuse a
``Domain``), ``Secure``, ``HttpOnly``, ``SameSite=Lax`` and ``Path=/``. Its value is sealed with
AES-256-GCM under a key id, and the host is the associated data, so a value copied to another
app's host does not open. A session lives at most 12 hours.

Every app of every org shares one site (``delimitusapps.com``), so ``SameSite`` does not separate
apps; Fetch-Metadata does (``ssc_edge.gate``). An app may not set a cookie with a platform name:
Envoy drops it from the app's response (``ssc_edge.envoy``), using :data:`PLATFORM_PREFIXES`.
"""

import base64
import binascii
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

COOKIE_NAME: Final = "__Host-ssc-session"
PLATFORM_PREFIXES: Final = ("__host-ssc", "__secure-ssc")
"""Lower-cased name prefixes an app's ``Set-Cookie`` may not use (browsers compare prefixes
without case)."""
MAX_SESSION_SECONDS: Final = 12 * 3600
KEY_BYTES: Final = 32
VERSION: Final = "v1"
_NONCE: Final = 12
_KID: Final = re.compile(r"[a-z0-9]{1,16}")
_USER: Final = re.compile(r"usr_[a-z0-9]{20}")
_ORG: Final = re.compile(r"org_[a-z0-9]{20}")
_SID: Final = re.compile(r"[A-Za-z0-9_-]{22,64}")
_MAX_DISPLAY: Final = 320


@dataclass(frozen=True, slots=True, kw_only=True)
class Session:
    """Who is signed in on one app host. ``name`` and ``email`` are display strings only."""

    sid: str
    sub: str
    org: str
    name: str
    email: str
    iat: int
    exp: int

    def __post_init__(self) -> None:
        ids = (_SID.fullmatch(self.sid), _USER.fullmatch(self.sub), _ORG.fullmatch(self.org))
        if not all(ids):
            raise ValueError("a session names a sid, a usr_ id and an org_ id")
        if not 0 < self.exp - self.iat <= MAX_SESSION_SECONDS:
            raise ValueError(f"a session lives at most {MAX_SESSION_SECONDS} seconds")
        if len(self.name) > _MAX_DISPLAY or len(self.email) > _MAX_DISPLAY:
            raise ValueError("display strings are at most 320 characters")


def new_sid() -> str:
    return secrets.token_urlsafe(24)


class SessionCodec:
    """Seals and opens session values. Holds every accepted key; seals with ``active``."""

    def __init__(self, keys: Mapping[str, bytes], *, active: str) -> None:
        for kid, key in keys.items():
            if not _KID.fullmatch(kid) or len(key) != KEY_BYTES:
                raise ValueError("session keys are 32 bytes under a 1-16 character [a-z0-9] id")
        if active not in keys:
            raise ValueError(f"no session key {active!r}")
        self._keys = {kid: AESGCM(key) for kid, key in keys.items()}
        self._active = active

    def seal(self, session: Session, host: str) -> str:
        nonce = secrets.token_bytes(_NONCE)
        body = json.dumps(asdict(session), separators=(",", ":")).encode()
        sealed = self._keys[self._active].encrypt(nonce, body, _aad(host))
        return f"{VERSION}.{self._active}.{_b64(nonce + sealed)}"

    def open(self, value: str, host: str, *, now: int) -> Session | None:
        """The session in ``value`` for ``host``, or None: unknown key, tampered, another host,
        another shape, or expired. Never raises."""
        version, _, rest = value.partition(".")
        kid, _, blob = rest.partition(".")
        aead = self._keys.get(kid)
        if version != VERSION or aead is None:
            return None
        try:
            raw = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
            body = aead.decrypt(raw[:_NONCE], raw[_NONCE:], _aad(host))
            session = Session(**json.loads(body))
        except InvalidTag, ValueError, TypeError, binascii.Error:
            return None
        return session if session.iat <= now < session.exp else None


def _aad(host: str) -> bytes:
    return f"ssc-session/{VERSION}\n{host}".encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def set_cookie(value: str, *, max_age: int) -> str:
    """The only ``Set-Cookie`` the gateway sends. Never a ``Domain``."""
    if not 0 <= max_age <= MAX_SESSION_SECONDS or not re.fullmatch(r"[A-Za-z0-9._-]*", value):
        raise ValueError("a session cookie value is URL-safe and lives at most 12 hours")
    return f"{COOKIE_NAME}={value}; Path=/; Max-Age={max_age}; Secure; HttpOnly; SameSite=Lax"


def clear_cookie() -> str:
    return set_cookie("", max_age=0)


def cookie_values(header: str, name: str = COOKIE_NAME) -> list[str]:
    """Every value of cookie ``name`` in a ``Cookie`` header, in order."""
    found: list[str] = []
    for part in header.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            found.append(value.strip())
    return found


def is_platform_cookie(name: str) -> bool:
    return name.strip().lower().startswith(PLATFORM_PREFIXES)
