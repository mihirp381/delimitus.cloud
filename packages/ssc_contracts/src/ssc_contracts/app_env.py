"""The environment variables the platform sets inside every app container (decision 014).

Names only; the values are per app environment. The CLI's agent pack, ``ssc doctor`` and the
control plane read the names from here so that they cannot drift apart. A manifest can never set
any of them: ``PORT``, ``HOME``, ``DATABASE_URL`` and its ``PG*`` parts are reserved and every
``SSC_*`` name belongs to the platform (``ssc_contracts.manifest``).
"""

import re
from typing import Final

PORT: Final = "PORT"
"""The port the app must listen on, on 0.0.0.0. ``[runtime] port`` in ``ssc.toml``, default 8080."""

HOME: Final = "HOME"
HOME_VALUE: Final = "/tmp"  # noqa: S108  (in-memory in the container; the corpus fix-it)
"""Always ``/tmp``: the root filesystem is read-only and ``/tmp`` is memory, lost on restart."""

DATABASE_URL: Final = "DATABASE_URL"
"""The app's own Postgres, only with ``[state] postgres = true``: a pinned secret in the form of
decision 003, ``postgresql://...?sslmode=verify-full&sslrootcert=...`` (SSC-040)."""

PGHOST: Final = "PGHOST"
PGPORT: Final = "PGPORT"
PGDATABASE: Final = "PGDATABASE"
PGUSER: Final = "PGUSER"
PGPASSWORD: Final = "PGPASSWORD"  # noqa: S105  (a variable name, not a password)
PGSSLMODE: Final = "PGSSLMODE"
PGSSLROOTCERT: Final = "PGSSLROOTCERT"
DATABASE_PARTS: Final = (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD, PGSSLMODE, PGSSLROOTCERT)
"""``DATABASE_URL`` in parts, set with it, for frameworks with no URL parser (Django)."""

DATABASE_CA: Final = "DATABASE_CA"
"""Not a variable: the secret holding the database's CA certificates, a file at
``DATABASE_CA_PATH`` that ``sslrootcert`` names."""
DATABASE_CA_PATH: Final = "/etc/ssc/db-ca.crt"

APP_ORIGIN: Final = "SSC_APP_ORIGIN"
"""The app's exact origin, ``https://<host>`` with no path: the identity note's audience."""

IDENTITY_KEYS_URL: Final = "SSC_IDENTITY_KEYS_URL"
"""The cell's JWKS that verifies identity notes, inline as a ``data:`` URL (no internet needed)."""

HTTPS_PROXY: Final = "HTTPS_PROXY"
"""Only for an environment that declares ``[egress] hosts``: a pinned secret, the URL of the
cell's egress proxy with the environment's own credential in it (SSC-053)."""

NODE_USE_ENV_PROXY: Final = "NODE_USE_ENV_PROXY"
"""``1`` beside ``HTTPS_PROXY``, so Node's built-in ``fetch`` and ``https`` use the proxy."""

NO_PROXY: Final = "NO_PROXY"
"""Beside ``HTTPS_PROXY``: the names an app reaches without the proxy."""

EGRESS_NAMES: Final = (HTTPS_PROXY, NODE_USE_ENV_PROXY, NO_PROXY)

PLATFORM_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {PORT, HOME, DATABASE_URL, *DATABASE_PARTS, APP_ORIGIN, IDENTITY_KEYS_URL, *EGRESS_NAMES}
)

SECRET_NAME: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
"""An app secret's name, which is also the environment variable it arrives in (SSC-026)."""

_NOT_SECRET_NAMES: Final = PLATFORM_ENV_NAMES | {DATABASE_CA, "PATH"}
_NOT_SECRET_PREFIXES: Final = ("SSC_", "K_", "X_GOOGLE_")


def secret_name_problem(name: str) -> str | None:
    """Why ``name`` cannot name a secret, or None. Names the platform or Cloud Run sets are
    refused, so a secret can never shadow one."""
    if SECRET_NAME.fullmatch(name) is None:
        return "must be an upper-case name of A-Z, 0-9 and _, at most 64 characters"
    if name in _NOT_SECRET_NAMES or name.startswith(_NOT_SECRET_PREFIXES):
        return "is set by the platform"
    return None
