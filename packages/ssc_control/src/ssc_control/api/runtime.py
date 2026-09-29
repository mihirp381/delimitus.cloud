"""What one running API process holds: settings, the engine, the verifier and the limiter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from ssc_control.api.auth import Verifier
    from ssc_control.api.ratelimit import RateLimiter
    from ssc_control.api.settings import Settings


@dataclass(frozen=True, slots=True)
class Runtime:
    settings: Settings
    engine: AsyncEngine
    verifier: Verifier
    limiter: RateLimiter
    owns_engine: bool


def runtime_of(request: Request) -> Runtime:
    rt = request.app.state.runtime
    if not isinstance(rt, Runtime):
        raise RuntimeError("app.state.runtime is not set; build the app with create_app()")
    return rt
