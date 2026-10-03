"""How a timer run reaches the app: the ``ScheduleDispatcher`` port and its fake.

A run is two requests through the app's public host. ``start`` asks for the app's
``health_path`` under ``asyncio.timeout(START_SECONDS)``, so a gateway and an app at zero start
before the run's own clock does; any answer below ``500`` means both are up. ``dispatch`` then
calls the declared path, once, under ``asyncio.timeout(timeout_seconds)``. Neither is retried: a
run is dispatched at most once. An implementation must honour cancellation and make no call
after it is cancelled. ``timers.https.HttpsScheduleDispatcher`` is the real one;
``FakeScheduleDispatcher`` serves dev and tests.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from ssc_shared.hosts import Environment

DispatchError = Literal["dispatch_error"]

START_SECONDS: Final = 150
"""How long a run waits for the gateway and the app to answer its start request: Cloud Run's
two-minute startup window for the app (``ssc_agent.cloud_run.STARTUP_FAILURES``) plus the
gateway's own start."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TimerCall:
    org_id: str
    environment_id: str
    schedule_id: str
    run_id: str
    method: Literal["GET", "POST"]
    path: str
    slug: str
    environment: Environment
    cell_label: str
    """With ``slug`` and ``environment``, the app's host (``ssc_shared.hosts.app_origin``)."""
    health_path: str
    """Where ``start`` asks: the manifest's ``runtime.health_path`` of the running release."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatchResult:
    """What the app answered: an HTTP status, or ``error`` when no answer came back."""

    http_status: int | None = None
    error: DispatchError | None = None


class ScheduleDispatcher(Protocol):
    async def start(self, call: TimerCall) -> DispatchResult: ...

    async def dispatch(self, call: TimerCall) -> DispatchResult: ...


@dataclass(frozen=True, slots=True)
class Scripted:
    """One scripted answer: wait ``delay`` seconds, then raise ``raises`` or answer ``status``."""

    status: int = 200
    delay: float = 0.0
    raises: Exception | None = None


class FakeScheduleDispatcher(ScheduleDispatcher):
    """Answers from a script, one entry per call, repeating the last; records every call, and
    every call cancelled before it answered. Every start answers as ``start`` says."""

    def __init__(
        self, script: Sequence[Scripted] = (Scripted(),), *, start: Scripted | None = None
    ) -> None:
        if not script:
            raise ValueError("a script needs at least one answer")
        self._script = list(script)
        self._start = start or Scripted()
        self.starts: list[TimerCall] = []
        self.calls: list[TimerCall] = []
        self.answered: list[TimerCall] = []
        self.cancelled: list[TimerCall] = []

    async def start(self, call: TimerCall) -> DispatchResult:
        self.starts.append(call)
        await asyncio.sleep(self._start.delay)
        if self._start.raises is not None:
            raise self._start.raises
        return DispatchResult(http_status=self._start.status)

    async def dispatch(self, call: TimerCall) -> DispatchResult:
        step = self._script[min(len(self.calls), len(self._script) - 1)]
        self.calls.append(call)
        try:
            await asyncio.sleep(step.delay)
        except asyncio.CancelledError:
            self.cancelled.append(call)
            raise
        if step.raises is not None:
            raise step.raises
        self.answered.append(call)
        return DispatchResult(http_status=step.status)
