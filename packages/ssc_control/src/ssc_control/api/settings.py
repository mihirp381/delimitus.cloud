"""API settings: a frozen dataclass read once from the environment. No framework, no magic."""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from ssc_control.metrics.pseudonym import parse_master_key

USER_AUDIENCE: Final = "https://api.delimitus.com"
INTERNAL_AUDIENCE: Final = "https://api.delimitus.com/internal"


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
        )

    @classmethod
    def for_spec(cls) -> Settings:
        """Enough to build the app and its OpenAPI document. Never connects to anything."""
        return cls(
            database_dsn="postgresql://ssc_app@localhost/ssc",
            jwks={"keys": []},
            issuer="https://auth.delimitus.com",
        )
