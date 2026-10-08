"""What every worker task gets: the ``Ports``, passed through Procrastinate's
``additional_context`` so task modules never build their own clients (decision 014).

A task declared with ``pass_context=True`` calls ``ports_of(context)``. ``worker.run`` builds the
production ``Ports``; tests build their own. Lanes add a field here, with a safe default, when
they first need one.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, cast

from procrastinate import JobContext
from sqlalchemy.ext.asyncio import AsyncEngine

from ssc_control.cell.deployer import CellDeployer
from ssc_control.github.client import GitHubApp
from ssc_control.identity.workos import WorkOSClient
from ssc_control.notifications.mailer import Mailer
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
from ssc_control.runtime.cells import CellPorts, CellUnavailableError
from ssc_control.runtime.specs import NoReleaseSpecs, ReleaseSpecs
from ssc_control.timers.dispatch import ScheduleDispatcher
from ssc_shared.blobstore import BlobStore

log = logging.getLogger(__name__)

PORTS_KEY: Final = "ssc_ports"
APPS_DOMAIN: Final = "delimitusapps.com"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class Ports:
    """``cells`` None means no cell is configured: the reconciler defers nothing, deployments
    fail with ``RUNTIME_UNAVAILABLE`` and builds with ``BUILD_DRIVER_UNAVAILABLE``. Set, it gives
    each org its own cell's ports (``runtime.cells``, decision 030); an org whose cell is not
    configured fails its jobs with ``CELL_UNAVAILABLE`` and is skipped by the ticks. Within a
    cell, an app database port of None fails the first deployment of an environment that
    declares Postgres with ``DATABASE_UNAVAILABLE`` (SSC-040), an identity of None gives apps no
    identity keys and no origin (SSC-018), a usage port of None records no usage (SSC-028) and
    an egress port of None deploys an app that declares outbound hosts with no proxy credential,
    so it reaches none (SSC-053). ``blob_store`` and ``cell_stores`` both None: the snapshot and
    anchor ticks defer nothing and a compile does nothing. ``cell_stores`` set sends each org's
    snapshots and audit anchors to its cell's bucket instead of ``blob_store``
    (``storage.org_store``). ``timer_dispatcher`` None fails timer runs with
    ``dispatch_unavailable``. ``cell_deployer`` None fails a lazy cell resource with
    ``CELL_DEPLOYER_UNAVAILABLE`` (SSC-087). ``directory`` None skips the directory sync
    (SSC-064). ``github`` None leaves a push job with nothing to do (SSC-047); ``apps_domain``
    makes the preview address it reports. ``mailer`` None leaves queued approval mail waiting
    (SSC-049); ``console_url`` is the link those mails carry."""

    engine: AsyncEngine
    cells: CellPorts | None = None
    release_specs: ReleaseSpecs = field(default_factory=NoReleaseSpecs)
    blob_store: BlobStore | None = None
    cell_stores: Callable[[str], BlobStore] | None = None
    snapshot: SnapshotPort = field(default_factory=NullSnapshotPort)
    timers: TimersPort = field(default_factory=NullTimersPort)
    prod_gate: ProdGate = field(default_factory=RefusingProdGate)
    clock: Callable[[], datetime] = _utcnow
    metrics: MetricsPort = field(default_factory=NullMetricsPort)
    timer_dispatcher: ScheduleDispatcher | None = None
    cell_deployer: CellDeployer | None = None
    directory: WorkOSClient | None = None
    github: GitHubApp | None = None
    apps_domain: str = APPS_DOMAIN
    mailer: Mailer | None = None
    console_url: str = ""


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


async def cell_configured(ports: Ports, org_id: str, what: str) -> bool:
    """Whether a job that writes to the org's cell bucket may run: False, logged, when the org's
    cell label is not one of ``SSC_CELLS``. ``cell_stores`` names a bucket for any label, so an
    org made without ``--cell-label`` (its label generated, its cell never applied) would send
    the job to a bucket that does not exist, a 404 on every retry and on every sweep (seen
    2026-10-07 to 08). The reconcile tick already skips such an org the same way."""
    if ports.cells is None or ports.cell_stores is None:
        return True
    try:
        await ports.cells.for_org(org_id)
    except CellUnavailableError as exc:
        log.warning(
            "%s skipped an org: its cell is not configured",
            what,
            extra={"org_id": org_id, "cell_label": exc.label},
        )
        return False
    return True
