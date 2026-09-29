"""What one running API process holds: settings, the engine, the verifier, the limiter, the
metrics recorder, the blob store and the production gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import Request

from ssc_control.deploy.gates import approvals_prod_gate
from ssc_control.ports import MetricsPort, NullMetricsPort, ProdGate

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from ssc_control.api.auth import Verifier
    from ssc_control.api.ratelimit import RateLimiter
    from ssc_control.api.settings import Settings
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
    """Where bundles go; ``None`` when ``blob_backend`` is ``none``."""
    prod_gate: ProdGate = field(default_factory=approvals_prod_gate)
    """Checked when a ``prod`` deployment is posted; the deploy job checks it again."""


def runtime_of(request: Request) -> Runtime:
    rt = request.app.state.runtime
    if not isinstance(rt, Runtime):
        raise RuntimeError("app.state.runtime is not set; build the app with create_app()")
    return rt
