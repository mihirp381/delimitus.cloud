"""Cross-lane ports: the calls one part of the control plane makes into another that may not
exist yet. Frozen in W0; each owner implements its Protocol and callers take the Protocol.

Every stub is safe to run with: the production gate refuses (fail closed), the snapshot port
never confirms, and timers and metrics do nothing.

Owners: ``ProdGate`` A3 (called by B4 and C3), ``SnapshotPort`` A4 (called by B5),
``TimersPort`` A5 (called by B4 and B5), ``MetricsPort`` A2 (called by B4 and the API).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_control.audit import Actor

# ── production gate (A3) ─────────────────────────────────────────────────────

GateOutcome = Literal["clear", "waiting", "refused"]


@dataclass(frozen=True, slots=True, kw_only=True)
class GateResult:
    """``clear``: deploy may proceed. ``waiting``: approvals are open (``approval_ids``).
    ``refused``: never, whatever is approved. ``policy_decision_id`` is the ``pol_`` row."""

    outcome: GateOutcome
    approval_ids: tuple[str, ...] = ()
    policy_decision_id: str | None = None


class ProdGate(Protocol):
    async def check(
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        app_id: str,
        environment_id: str,
        release_id: str,
    ) -> GateResult: ...


class RefusingProdGate(ProdGate):
    """For tests: nothing reaches production. The real gate is
    ``ssc_control.approvals.gate.ApprovalsProdGate``."""

    async def check(
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        app_id: str,
        environment_id: str,
        release_id: str,
    ) -> GateResult:
        return GateResult(outcome="refused")


# ── access snapshot (A4) ─────────────────────────────────────────────────────


class SnapshotPort(Protocol):
    async def request(self, conn: AsyncConnection, org_id: str) -> int:
        """Mark the org's snapshot dirty in the caller's transaction; the version to wait for."""
        ...

    async def confirmed(self, org_id: str, version: int) -> bool:
        """Whether the org's cell has ``version`` or later: its ``latest.json`` names it, or its
        heartbeat reported it."""
        ...


class NullSnapshotPort(SnapshotPort):
    async def request(self, conn: AsyncConnection, org_id: str) -> int:
        return 0

    async def confirmed(self, org_id: str, version: int) -> bool:
        return False


# ── timers (A5) ──────────────────────────────────────────────────────────────


class DeclaredSchedule(Protocol):
    """One ``[[schedules]]`` entry of a release manifest (B1's model satisfies this)."""

    @property
    def name(self) -> str: ...
    @property
    def cron(self) -> str: ...
    @property
    def timezone(self) -> str: ...
    @property
    def path(self) -> str: ...
    @property
    def method(self) -> str: ...
    @property
    def timeout_seconds(self) -> int: ...


KillReason = Literal["disable", "quarantine"]


class TimersPort(Protocol):
    async def sync_schedules(  # noqa: PLR0913  (keyword-only)
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        environment_id: str,
        declared: Sequence[DeclaredSchedule],
        declared_by_user_id: str,
        actor: Actor,
    ) -> None:
        """Upsert by name after a forward deploy or promote; never called on rollback."""
        ...

    async def pause_for_kill(
        self, conn: AsyncConnection, *, org_id: str, app_id: str, reason: KillReason, actor: Actor
    ) -> list[str]:
        """Pause the app's active schedules; the ``sch_`` ids this call paused."""
        ...

    async def resume_after_kill(  # noqa: PLR0913  (keyword-only)
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        app_id: str,
        schedule_ids: Sequence[str],
        reason: KillReason,
        actor: Actor,
    ) -> None:
        """Resume exactly the schedules ``pause_for_kill`` returned."""
        ...


class NullTimersPort(TimersPort):
    async def sync_schedules(  # noqa: PLR0913  (keyword-only)
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        environment_id: str,
        declared: Sequence[DeclaredSchedule],
        declared_by_user_id: str,
        actor: Actor,
    ) -> None:
        return None

    async def pause_for_kill(
        self, conn: AsyncConnection, *, org_id: str, app_id: str, reason: KillReason, actor: Actor
    ) -> list[str]:
        return []

    async def resume_after_kill(  # noqa: PLR0913  (keyword-only)
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        app_id: str,
        schedule_ids: Sequence[str],
        reason: KillReason,
        actor: Actor,
    ) -> None:
        return None


# ── metrics (A2) ─────────────────────────────────────────────────────────────


class MetricKind(StrEnum):
    """Mirrors the ``ssc.metrics_event.kind`` CHECK."""

    FIRST_URL = "first_url"
    DEPLOY = "deploy"
    SHARE = "share"
    APP_OPENED = "app_opened"
    DATA_QUERY = "data_query"
    DATABASE_USE = "database_use"
    TIMER_RUN = "timer_run"
    USAGE_HOUR = "usage_hour"
    COLD_START = "cold_start"
    FIXED_RESOURCE = "fixed_resource"


MetricValue = str | int | float | bool | None


class MetricsPort(Protocol):
    async def record_event(  # noqa: PLR0913  (keyword-only)
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        kind: MetricKind,
        app_id: str | None = None,
        user_id: str | None = None,
        source_tool: str | None = None,
        properties: Mapping[str, MetricValue] | None = None,
        at: datetime | None = None,
    ) -> None:
        """Insert one event in the caller's transaction. ``properties`` are flat scalars."""
        ...


class NullMetricsPort(MetricsPort):
    async def record_event(  # noqa: PLR0913  (keyword-only)
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        kind: MetricKind,
        app_id: str | None = None,
        user_id: str | None = None,
        source_tool: str | None = None,
        properties: Mapping[str, MetricValue] | None = None,
        at: datetime | None = None,
    ) -> None:
        return None
