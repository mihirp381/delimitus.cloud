"""What every worker task gets: the ``Ports``, passed through Procrastinate's
``additional_context`` so task modules never build their own clients (decision 014).

A task declared with ``pass_context=True`` calls ``ports_of(context)``. ``worker.run`` builds the
production ``Ports``; tests build their own. Lanes add a field here, with a safe default, when
they first need one.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, cast

from procrastinate import JobContext
from sqlalchemy.ext.asyncio import AsyncEngine

from ssc_control.cell.deployer import CellDeployer
from ssc_control.deploy.build_driver import BuildDriver
from ssc_control.ports import (
    MetricsPort,
    NullMetricsPort,
    NullSnapshotPort,
    NullTimersPort,
    ProdGate,
    RefusingProdGate,
    SnapshotPort,
    TimersPort,
)
from ssc_control.runtime.driver import RuntimeDriver
from ssc_control.runtime.specs import NoReleaseSpecs, ReleaseSpecs
from ssc_control.timers.dispatch import ScheduleDispatcher
from ssc_shared.blobstore import BlobStore

PORTS_KEY: Final = "ssc_ports"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class Ports:
    """``runtime_driver`` None means no runtime is configured: the reconciler defers nothing
    and deployments fail with ``RUNTIME_UNAVAILABLE``. ``build_driver`` None fails builds with
    ``BUILD_DRIVER_UNAVAILABLE``. ``blob_store`` None means ``SSC_BLOB_BACKEND=none``: the
    snapshot and anchor ticks defer nothing and a compile does nothing. ``cell_stores`` set sends
    each org's snapshots to its cell's bucket instead. ``timer_dispatcher`` None fails timer runs
    with ``dispatch_unavailable``. ``cell_deployer`` None fails a lazy cell resource with
    ``CELL_DEPLOYER_UNAVAILABLE`` (SSC-087)."""

    engine: AsyncEngine
    runtime_driver: RuntimeDriver | None = None
    release_specs: ReleaseSpecs = field(default_factory=NoReleaseSpecs)
    blob_store: BlobStore | None = None
    cell_stores: Callable[[str], BlobStore] | None = None
    snapshot: SnapshotPort = field(default_factory=NullSnapshotPort)
    timers: TimersPort = field(default_factory=NullTimersPort)
    prod_gate: ProdGate = field(default_factory=RefusingProdGate)
    clock: Callable[[], datetime] = _utcnow
    build_driver: BuildDriver | None = None
    metrics: MetricsPort = field(default_factory=NullMetricsPort)
    timer_dispatcher: ScheduleDispatcher | None = None
    cell_deployer: CellDeployer | None = None


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
