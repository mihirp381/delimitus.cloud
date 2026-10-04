"""What one running API process holds: settings, the engine, the verifier, the limiter, the
metrics recorder, the blob store, the production gate, the timers, the secret grants, the app
databases, the cell's logs and egress proxy, and the GitHub App."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import Request

from ssc_control.deploy.gates import approvals_prod_gate
from ssc_control.ports import MetricsPort, NullMetricsPort, NullTimersPort, ProdGate, TimersPort

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from ssc_control.api.auth import Verifier
    from ssc_control.api.ratelimit import RateLimiter
    from ssc_control.api.settings import Settings
    from ssc_control.github.client import GitHubApp
    from ssc_control.runtime.app_databases import AppDatabases
    from ssc_control.runtime.cell_egress import CellEgress
    from ssc_control.runtime.secret_grants import SecretGrants
    from ssc_shared.blobstore import BlobStore
    from ssc_shared.logs import CellLogs


@dataclass(frozen=True, slots=True)
class Runtime:
    settings: Settings
    engine: AsyncEngine
    verifier: Verifier
    limiter: RateLimiter
    owns_engine: bool
    metrics: MetricsPort = field(default_factory=NullMetricsPort)
    blob_store: BlobStore | None = None
    """Where bundles go; ``None`` when ``blob_backend`` is ``none``."""
    prod_gate: ProdGate = field(default_factory=approvals_prod_gate)
    """Checked when a ``prod`` deployment is posted; the deploy job checks it again."""
    timers: TimersPort = field(default_factory=NullTimersPort)
    """Resumes the schedules the kill switch paused when an app is enabled."""
    secret_grants: SecretGrants | None = None
    """Prepares a secret in the cell and grants one upload of its value (SSC-026); ``None``
    when the cell is not configured, and secret writes refuse."""
    app_databases: AppDatabases | None = None
    """Rotates and reads app databases through the cell agent (SSC-040); ``None`` when the cell
    is not configured, and rotation refuses."""
    cell_logs: CellLogs | None = None
    """App logs and health through the cell agent (SSC-024); ``None`` when the cell is not
    configured, and log reads answer ``LOGS_UNAVAILABLE``."""
    github: GitHubApp | None = None
    """The GitHub App (SSC-047); ``None`` when it is not configured, and connecting a
    repository refuses."""
    cell_egress: CellEgress | None = None
    """Where the cell's proxy is and its fixed outbound address, through the cell agent
    (SSC-053); ``None`` when the cell is not configured, and the console shows neither."""


def runtime_of(request: Request) -> Runtime:
    rt = request.app.state.runtime
    if not isinstance(rt, Runtime):
        raise RuntimeError("app.state.runtime is not set; build the app with create_app()")
    return rt
