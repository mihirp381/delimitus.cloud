"""A cell's lazy resources (SSC-087): what each is, what turns it on, what it adds a month, and
what a builder is told while it is created. The one place these are stated: the control plane,
the API, the console (SSC-057) and the cost report (SSC-028) read them from here, and the cell
stack's flags (``ssc_infra.naming.LAZY_FLAGS``) are checked against :class:`CellResource`."""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class CellResource(StrEnum):
    """Each is a flag in the cell's stack config and is never turned off by code: ``database``
    the Cloud SQL instance, ``egress`` the egress proxy group, ``connections`` the data gateway
    and its file broker."""

    DATABASE = "database"
    EGRESS = "egress"
    CONNECTIONS = "connections"


class CellResourceState(StrEnum):
    REQUESTED = "requested"
    CREATING = "creating"
    READY = "ready"
    FAILED = "failed"


class CellResourceCause(StrEnum):
    """``deploy``: an environment whose manifest has ``[state] postgres = true``. ``file_use``:
    one whose manifest asks for ``[files]`` (SSC-046), which needs the data gateway's broker."""

    DEPLOY = "deploy"
    EGRESS_APPROVED = "egress_approved"
    CONNECTION_GRANTED = "connection_granted"
    FILE_USE = "file_use"
    ADMIN = "admin"


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
            "Setting up your company's data connections and file storage, a few minutes, "
            "this happens once."
        ),
    }
)
