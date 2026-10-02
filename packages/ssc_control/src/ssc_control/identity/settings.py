"""Auth-host settings, read once from the environment (SSC-019).

Secrets (``SSC_WORKOS_API_KEY``, ``SSC_AUTH_SIGNING_KEY``, ``SSC_AUTH_STATE_KEY``,
``SSC_AUTH_DEV_CELL_SECRET``) come from the environment only; the deploy ticket puts them in
Secret Manager.
"""

import base64
import binascii
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from ssc_control.api.settings import APPS_DOMAIN, USER_AUDIENCE
from ssc_control.identity.workos import DEFAULT_BASE
from ssc_shared.hosts import check_apps_domain

AUTH_URL: Final = "https://auth.delimitus.com"
DEV_ENVIRONMENTS: Final = frozenset({"dev", "test"})


def _key(raw: str) -> bytes:
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("SSC_AUTH_STATE_KEY must be base64") from e
    if len(key) < 32:  # noqa: PLR2004
        raise ValueError("SSC_AUTH_STATE_KEY must be at least 32 bytes")
    return key


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthSettings:
    database_dsn: str
    workos_api_key: str = field(repr=False)
    workos_client_id: str
    signing_pem: bytes = field(repr=False)
    signing_kid: str
    state_key: bytes = field(repr=False)
    """Signs the login cookie and the browser session cookie (domain-separated)."""
    auth_url: str = AUTH_URL
    """This host's public origin; also the ``iss`` of every credential it signs."""
    api_audience: str = USER_AUDIENCE
    apps_domain: str = APPS_DOMAIN
    workos_base: str = DEFAULT_BASE
    environment: str = "prod"
    dev_cell_secret: str = field(default="", repr=False)
    """Dev and test only: the rig gateway's shared secret for ``/internal/redeem``."""

    def __post_init__(self) -> None:
        check_apps_domain(self.apps_domain)
        if self.auth_url.endswith("/") or not self.auth_url.startswith(("https://", "http://")):
            raise ValueError("SSC_AUTH_URL is an origin: scheme and host, no trailing slash")
        if self.auth_url.startswith("http://") and self.environment not in DEV_ENVIRONMENTS:
            raise ValueError("SSC_AUTH_URL must be https outside dev and test")
        if self.dev_cell_secret and self.environment not in DEV_ENVIRONMENTS:
            raise ValueError("SSC_AUTH_DEV_CELL_SECRET is refused outside dev and test")

    @property
    def secure_cookies(self) -> bool:
        return self.auth_url.startswith("https://")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AuthSettings:
        e = os.environ if env is None else env
        return cls(
            database_dsn=e["SSC_DATABASE_DSN"],
            workos_api_key=e["SSC_WORKOS_API_KEY"],
            workos_client_id=e["SSC_WORKOS_CLIENT_ID"],
            signing_pem=e["SSC_AUTH_SIGNING_KEY"].encode(),
            signing_kid=e["SSC_AUTH_SIGNING_KID"],
            state_key=_key(e["SSC_AUTH_STATE_KEY"]),
            auth_url=e.get("SSC_AUTH_URL", AUTH_URL),
            api_audience=e.get("SSC_API_USER_AUDIENCE", USER_AUDIENCE),
            apps_domain=e.get("SSC_APPS_DOMAIN", APPS_DOMAIN),
            workos_base=e.get("SSC_WORKOS_BASE", DEFAULT_BASE),
            environment=e.get("SSC_ENV", "prod"),
            dev_cell_secret=e.get("SSC_AUTH_DEV_CELL_SECRET", ""),
        )
