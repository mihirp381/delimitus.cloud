"""What every worker task gets: the ``Ports``, passed through Procrastinate's
``additional_context`` so task modules never build their own clients (decision 014).

A task declared with ``pass_context=True`` calls ``ports_of(context)``. ``worker.run`` builds the
production ``Ports``; tests build their own. Lanes add a field here, with a safe default, when
they first need one (``build_driver`` arrives with B4).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, cast

from procrastinate import JobContext
from sqlalchemy.ext.asyncio import AsyncEngine

from ssc_control.ports import (
    NullSnapshotPort,
    NullTimersPort,
    ProdGate,
    RefusingProdGate,
    SnapshotPort,
    TimersPort,
)
from ssc_control.runtime.driver import RuntimeDriver
from ssc_control.runtime.specs import NoReleaseSpecs, ReleaseSpecs
from ssc_shared.blobstore import BlobStore

PORTS_KEY: Final = "ssc_ports"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class Ports:
    """``runtime_driver`` None means no runtime is configured: the reconciler defers nothing.
    ``blob_store`` None until the first lane that stores blobs from a job wires one (A1b)."""

    engine: AsyncEngine
    runtime_driver: RuntimeDriver | None = None
    release_specs: ReleaseSpecs = field(default_factory=NoReleaseSpecs)
    blob_store: BlobStore | None = None
    snapshot: SnapshotPort = field(default_factory=NullSnapshotPort)
    timers: TimersPort = field(default_factory=NullTimersPort)
    prod_gate: ProdGate = field(default_factory=RefusingProdGate)
    clock: Callable[[], datetime] = _utcnow


class PortsMissingError(RuntimeError):
    pass


def ports_of(context: JobContext) -> Ports:
    extra = cast("dict[str, object]", context.additional_context)  # pyright: ignore[reportUnknownMemberType]
    ports = extra.get(PORTS_KEY)
    if not isinstance(ports, Ports):
        raise PortsMissingError(
            f"run the worker with additional_context={{{PORTS_KEY!r}: Ports(...)}}"
        )
    return ports
