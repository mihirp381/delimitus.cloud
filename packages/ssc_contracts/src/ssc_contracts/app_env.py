"""The environment variables the platform sets inside every app container (decision 014).

Names only; the values are per app environment. The CLI's agent pack, ``ssc doctor`` and the
control plane read the names from here so that they cannot drift apart. A manifest can never set
any of them: ``PORT``, ``HOME`` and ``DATABASE_URL`` are reserved and every ``SSC_*`` name belongs
to the platform (``ssc_contracts.manifest``).
"""

import re
from typing import Final

PORT: Final = "PORT"
"""The port the app must listen on, on 0.0.0.0. ``[runtime] port`` in ``ssc.toml``, default 8080."""

HOME: Final = "HOME"
HOME_VALUE: Final = "/tmp"  # noqa: S108  (in-memory in the container; the corpus fix-it)
"""Always ``/tmp``: the root filesystem is read-only and ``/tmp`` is memory, lost on restart."""

DATABASE_URL: Final = "DATABASE_URL"
"""The app's own Postgres, only when ``[state] postgres = true`` is granted (SSC-026)."""

APP_ORIGIN: Final = "SSC_APP_ORIGIN"
"""The app's exact origin, ``https://<host>`` with no path: the identity note's audience."""

IDENTITY_KEYS_URL: Final = "SSC_IDENTITY_KEYS_URL"
"""The JWKS address that verifies identity notes, ``https://keys.delimitus.com/<cell>/jwks.json``."""

PLATFORM_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {PORT, HOME, DATABASE_URL, APP_ORIGIN, IDENTITY_KEYS_URL}
)

SECRET_NAME: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
"""An app secret's name, which is also the environment variable it arrives in (SSC-026)."""

_NOT_SECRET_NAMES: Final = PLATFORM_ENV_NAMES | {"PATH"}
_NOT_SECRET_PREFIXES: Final = ("SSC_", "K_", "X_GOOGLE_")


def secret_name_problem(name: str) -> str | None:
    """Why ``name`` cannot name a secret, or None. Names the platform or Cloud Run sets are
    refused, so a secret can never shadow one."""
    if SECRET_NAME.fullmatch(name) is None:
        return "must be an upper-case name of A-Z, 0-9 and _, at most 64 characters"
    if name in _NOT_SECRET_NAMES or name.startswith(_NOT_SECRET_PREFIXES):
        return "is set by the platform"
    return None
