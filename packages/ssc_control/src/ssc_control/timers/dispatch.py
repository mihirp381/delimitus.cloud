"""How a timer run reaches the app: the ``ScheduleDispatcher`` port and its fake.

The runner calls ``dispatch`` once per run, under ``asyncio.timeout(timeout_seconds)``, and never
retries it: a run is dispatched at most once. An implementation must honour cancellation and make
no call after it is cancelled. The HTTPS dispatcher through the cell, with the schedule
principal's token, is SSC-018's; ``FakeScheduleDispatcher`` serves dev and tests.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

DispatchError = Literal["dispatch_error"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TimerCall:
    org_id: str
    environment_id: str
    schedule_id: str
    run_id: str
    method: Literal["GET", "POST"]
    path: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatchResult:
    """What the app answered: an HTTP status, or ``error`` when no answer came back."""

    http_status: int | None = None
    error: DispatchError | None = None


class ScheduleDispatcher(Protocol):
    async def dispatch(self, call: TimerCall) -> DispatchResult: ...


@dataclass(frozen=True, slots=True)
class Scripted:
    """One scripted answer: wait ``delay`` seconds, then raise ``raises`` or answer ``status``."""

    status: int = 200
    delay: float = 0.0
    raises: Exception | None = None


class FakeScheduleDispatcher(ScheduleDispatcher):
    """Answers from a script, one entry per call, repeating the last; records every call, and
    every call cancelled before it answered."""

    def __init__(self, script: Sequence[Scripted] = (Scripted(),)) -> None:
        if not script:
            raise ValueError("a script needs at least one answer")
        self._script = list(script)
        self.calls: list[TimerCall] = []
        self.answered: list[TimerCall] = []
        self.cancelled: list[TimerCall] = []

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
