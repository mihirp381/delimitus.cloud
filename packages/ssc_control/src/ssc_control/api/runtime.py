"""What one running API process holds: settings, the engine, the verifier, the limiter, the
metrics recorder, the blob store and the cell buckets, the production gate, the timers, each
org's cell (its secret grants, app databases, logs and egress proxy) and the GitHub App."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import Request

from ssc_contracts.errors import ErrorCode
from ssc_control.api.problems import Refusal
from ssc_control.deploy.gates import approvals_prod_gate
from ssc_control.ports import MetricsPort, NullMetricsPort, NullTimersPort, ProdGate, TimersPort
from ssc_control.runtime.cell_datagw import SchemaCache
from ssc_control.runtime.cells import CellPorts, CellUnavailableError, OrgCell

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from ssc_control.api.auth import Verifier
    from ssc_control.api.ratelimit import RateLimiter
    from ssc_control.api.settings import Settings
    from ssc_control.github.client import GitHubApp
    from ssc_control.storage import CellStores
    from ssc_shared.blobstore import BlobStore


@dataclass(frozen=True, slots=True)
class Runtime:
    settings: Settings
    engine: AsyncEngine
    verifier: Verifier
    limiter: RateLimiter
    owns_engine: bool
    metrics: MetricsPort = field(default_factory=NullMetricsPort)
    blob_store: BlobStore | None = None
    """Where bundles go without ``cell_stores``; ``None`` when ``blob_backend`` is ``none``."""
    cell_stores: CellStores | None = None
    """Each cell's bucket, where its org's bundles go (``storage.org_bundle_store``, decision
    015); ``None`` without ``SSC_CELL_BUCKET_TEMPLATE``."""
    prod_gate: ProdGate = field(default_factory=approvals_prod_gate)
    """Checked when a ``prod`` deployment is posted; the deploy job checks it again."""
    timers: TimersPort = field(default_factory=NullTimersPort)
    """Resumes the schedules the kill switch paused when an app is enabled."""
    cells: CellPorts | None = None
    """Each org's cell (decision 030): through its agent, secret grants (SSC-026), app database
    rotation and reads (SSC-040), app logs and health (SSC-024) and where its proxy is
    (SSC-053). ``None`` when no cell is configured, and each of those answers its own
    ``*_UNAVAILABLE`` (the console shows no proxy). See :func:`cell_of`."""
    github: GitHubApp | None = None
    """The GitHub App (SSC-047); ``None`` when it is not configured, and connecting a
    repository refuses."""
    schema_cache: SchemaCache = field(default_factory=SchemaCache)
    """Connections' tables and columns as the data gateway last showed them, for five minutes
    (GA-5.8)."""


async def cell_of(request: Request, org_id: str) -> OrgCell | None:
    """``org_id``'s cell, or None when no cell is configured. An org whose cell this process
    cannot reach is refused ``CELL_UNAVAILABLE``."""
    cells = runtime_of(request).cells
    if cells is None:
        return None
    try:
        return await cells.for_org(org_id)
    except CellUnavailableError as exc:
        raise Refusal(ErrorCode.CELL_UNAVAILABLE, evidence={"cell_label": exc.label}) from None


def runtime_of(request: Request) -> Runtime:
    rt = request.app.state.runtime
    if not isinstance(rt, Runtime):
        raise RuntimeError("app.state.runtime is not set; build the app with create_app()")
    return rt
