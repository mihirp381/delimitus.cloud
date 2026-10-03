"""API settings: a frozen dataclass read once from the environment. No framework, no magic."""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from ssc_control.metrics.pseudonym import parse_master_key
from ssc_control.storage import signing_keys
from ssc_shared.hosts import check_apps_domain

MIB: Final = 1024 * 1024
USER_AUDIENCE: Final = "https://api.delimitus.com"
INTERNAL_AUDIENCE: Final = "https://api.delimitus.com/internal"
APPS_DOMAIN: Final = "delimitusapps.com"


@dataclass(frozen=True, slots=True)
class Settings:
    database_dsn: str
    """DSN of the control database, authenticating as ``ssc_app`` (never the migrator)."""
    jwks: Mapping[str, Any]
    """Public keys that API credentials are signed with: ``{"keys": [...]}``."""
    issuer: str
    """Expected ``iss`` of every API credential."""
    user_audience: str = USER_AUDIENCE
    internal_audience: str = INTERNAL_AUDIENCE
    rate_capacity: int = 60
    """Requests a credential may burst before waiting."""
    rate_refill_per_second: float = 1.0
    max_body_bytes: int = 1_048_576
    metrics_key: bytes | None = field(default=None, repr=False)
    """Master key for metrics pseudonyms (``SSC_METRICS_KEY``, base64 of 32 bytes). Unset: no
    metrics events are recorded. Held outside the database."""
    public_url: str = USER_AUDIENCE
    """Where clients reach this API. The agent interface is served at ``{public_url}/mcp``."""
    environment: str = "prod"
    """``SSC_ENV``. The filesystem blob store is refused unless this is ``dev`` or ``test``."""
    blob_backend: str = "none"
    """``none`` (bundle endpoints refuse), ``fs`` (dev and test: signed URLs served here) or
    ``gcs`` (``blob_bucket``, URLs signed as ``blob_signer``)."""
    blob_root: str = ""
    """Directory of the ``fs`` blob store."""
    blob_signing_keys: Mapping[str, bytes] = field(default_factory=dict[str, bytes], repr=False)
    """``SSC_BLOB_SIGNING_KEYS``: JSON ``{"kid": "<base64 of 32+ bytes>"}`` for ``fs`` URLs."""
    blob_signing_kid: str = ""
    """The key in ``blob_signing_keys`` that signs; the others still verify."""
    blob_bucket: str = ""
    """``SSC_BLOB_BUCKET``: the ``gcs`` store's bucket."""
    blob_signer: str = ""
    """``SSC_BLOB_SIGNER``: the service account the ``gcs`` store signs URLs as (IAM signBlob)."""
    bundle_max_bytes: int = 100 * MIB
    bundle_max_unpacked_bytes: int = 500 * MIB
    bundle_max_files: int = 20_000
    apps_domain: str = APPS_DOMAIN
    """``SSC_APPS_DOMAIN``: the registrable domain apps are served on (decision 004)."""
    cell_agent_url: str = ""
    """``SSC_CELL_AGENT_URL``: the cell agent, which prepares each secret (SSC-026). One cell
    until placement has its ticket, as for the worker."""
    secret_intake_url: str = ""
    """``SSC_SECRET_INTAKE_URL``: the cell's secret intake origin, where ``ssc secret set`` sends
    the value. Unset, with or without the agent: secret writes refuse ``SECRETS_UNAVAILABLE``."""

    def __post_init__(self) -> None:
        check_apps_domain(self.apps_domain)
        for name, url in (
            ("SSC_CELL_AGENT_URL", self.cell_agent_url),
            ("SSC_SECRET_INTAKE_URL", self.secret_intake_url),
        ):
            if url and not url.startswith("https://"):
                raise ValueError(f"{name} must be an https URL")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        return cls(
            database_dsn=e["SSC_DATABASE_DSN"],
            jwks=json.loads(e["SSC_API_JWKS"]),
            issuer=e["SSC_API_ISSUER"],
            user_audience=e.get("SSC_API_USER_AUDIENCE", USER_AUDIENCE),
            internal_audience=e.get("SSC_API_INTERNAL_AUDIENCE", INTERNAL_AUDIENCE),
            rate_capacity=int(e.get("SSC_API_RATE_CAPACITY", "60")),
            rate_refill_per_second=float(e.get("SSC_API_RATE_REFILL_PER_SECOND", "1.0")),
            metrics_key=parse_master_key(e["SSC_METRICS_KEY"]) if "SSC_METRICS_KEY" in e else None,
            public_url=e.get("SSC_API_PUBLIC_URL", USER_AUDIENCE),
            environment=e.get("SSC_ENV", "prod"),
            blob_backend=e.get("SSC_BLOB_BACKEND", "none"),
            blob_root=e.get("SSC_BLOB_ROOT", ""),
            blob_signing_keys=signing_keys(e.get("SSC_BLOB_SIGNING_KEYS", "{}")),
            blob_signing_kid=e.get("SSC_BLOB_SIGNING_KID", ""),
            blob_bucket=e.get("SSC_BLOB_BUCKET", ""),
            blob_signer=e.get("SSC_BLOB_SIGNER", ""),
            bundle_max_bytes=int(e.get("SSC_BUNDLE_MAX_BYTES", str(100 * MIB))),
            bundle_max_unpacked_bytes=int(e.get("SSC_BUNDLE_MAX_UNPACKED_BYTES", str(500 * MIB))),
            bundle_max_files=int(e.get("SSC_BUNDLE_MAX_FILES", "20000")),
            apps_domain=e.get("SSC_APPS_DOMAIN", APPS_DOMAIN),
            cell_agent_url=e.get("SSC_CELL_AGENT_URL", ""),
            secret_intake_url=e.get("SSC_SECRET_INTAKE_URL", ""),
        )

    @classmethod
    def for_spec(cls) -> Settings:
        """Enough to build the app and its OpenAPI document. Never connects to anything."""
        return cls(
            database_dsn="postgresql://ssc_app@localhost/ssc",
            jwks={"keys": []},
            issuer="https://auth.delimitus.com",
        )
