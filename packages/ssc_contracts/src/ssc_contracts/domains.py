"""Customer domains (CD-3): the names the cell agent gives a suffix's zone and certificate."""

import hashlib
import re
from typing import Final, Literal

from ssc_contracts.snapshot import check_suffix

RESOURCE_PREFIX: Final = "ssc-d-"
RESOURCE_NAME: Final = re.compile(r"ssc-d-[0-9a-f]{12}")
CertificateState = Literal["provisioning", "active", "failed"]
CERTIFICATE_STATES: Final[tuple[CertificateState, ...]] = ("provisioning", "active", "failed")
DomainState = Literal["pending_dns", "issuing", "active", "failed", "retiring", "retired"]
DOMAIN_STATES: Final[tuple[DomainState, ...]] = (
    "pending_dns",
    "issuing",
    "active",
    "failed",
    "retiring",
    "retired",
)
_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
NAME_SERVER: Final = re.compile(rf"{_LABEL}(\.{_LABEL})+")


def resource_name(suffix: str) -> str:
    """``ssc-d-`` and the first 12 hex of the suffix's SHA-256: its zone and every certificate
    resource."""
    digest = hashlib.sha256(check_suffix(suffix).encode()).hexdigest()
    return f"{RESOURCE_PREFIX}{digest[:12]}"


def name_server(value: str) -> str:
    """A name server host, lower-case without the trailing dot; ``ValueError`` otherwise."""
    host = value.lower().removesuffix(".")
    if len(host) > 253 or NAME_SERVER.fullmatch(host) is None:  # noqa: PLR2004
        raise ValueError(f"not a name server: {value!r}")
    return host
