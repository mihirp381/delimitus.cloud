"""A cell's lazy resources (SSC-087): what each is, what turns it on, what it adds a month, and
what a builder is told while it is created. The one place these are stated: the control plane,
the API, the console (SSC-057) and the cost report (SSC-028) read them from here, and the cell
stack's flags (``ssc_infra.naming.LAZY_FLAGS``) are checked against :class:`CellResource`.

The warm option (SSC-092) is stated here too: what each warm production environment and the warm
gateway add a month, and the two settings of the stack's ``warm`` flag the cell deployer takes
(``ssc_infra.naming.WARM_ARGS``, checked against :class:`WarmGateway`). The figures are not a
bill (A6)."""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class CellResource(StrEnum):
    """Each is a flag in the cell's stack config and is never turned off by code: ``database``
    the Cloud SQL instance, ``egress`` the egress proxy group, ``connections`` the data gateway."""

    DATABASE = "database"
    EGRESS = "egress"
    CONNECTIONS = "connections"


class CellResourceState(StrEnum):
    REQUESTED = "requested"
    CREATING = "creating"
    READY = "ready"
    FAILED = "failed"


class CellResourceCause(StrEnum):
    """``deploy``: an environment whose manifest has ``[state] postgres = true``. ``file_use`` is
    a seam for SSC-046, which settles what the control plane sees of it."""

    DEPLOY = "deploy"
    EGRESS_APPROVED = "egress_approved"
    CONNECTION_GRANTED = "connection_granted"
    FILE_USE = "file_use"
    ADMIN = "admin"


class WarmGateway(StrEnum):
    """The cell deployer's argument that sets the stack's ``warm`` flag: ``warm=true`` keeps the
    gateway at one instance or more, ``warm=false`` lets it scale to zero again."""

    ON = "warm=true"
    OFF = "warm=false"


WARM_ENVIRONMENT_MONTHLY_USD: Final = 10
"""About what one production environment kept at one instance adds a month."""
WARM_GATEWAY_MONTHLY_USD: Final = 10
"""About what the cell's gateway kept at one instance adds a month."""


def warm_monthly_usd(environments: int, *, gateway: bool) -> int:
    """About what the warm option adds a month for ``environments`` warm environments and,
    with ``gateway``, the warm gateway."""
    gateway_usd = WARM_GATEWAY_MONTHLY_USD if gateway else 0
    return environments * WARM_ENVIRONMENT_MONTHLY_USD + gateway_usd


MONTHLY_USD: Final[Mapping[CellResource, int]] = MappingProxyType(
    {CellResource.DATABASE: 13, CellResource.EGRESS: 7, CellResource.CONNECTIONS: 0}
)

NOTICE: Final[Mapping[CellResource, str]] = MappingProxyType(
    {
        CellResource.DATABASE: (
            "Creating your company's database, about ten minutes, this happens once."
        ),
        CellResource.EGRESS: (
            "Setting up your company's outbound internet access, a few minutes, this happens once."
        ),
        CellResource.CONNECTIONS: (
            "Setting up your company's data connections, a few minutes, this happens once."
        ),
    }
)
