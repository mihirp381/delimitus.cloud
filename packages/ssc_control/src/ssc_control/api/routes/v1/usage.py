"""Usage (SSC-028): how much each app environment ran in a month, from the cell's usage events.

- ``GET .../environments/{environment_id}/usage``: one environment's month. Anyone who may see
  the app may ask, as for its health.
- ``GET /usage``: every environment with usage that month and when the cell's fixed resources
  were created. Active org admins only.

``month`` is ``YYYY-MM`` (UTC), this month by default. The numbers are for metrics and the cost
view only; nothing bills from them. Each reads the control database alone, never the cell or an
app.
"""

from datetime import UTC, date, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Query
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_admin
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.uow import UserUoW
from ssc_control.metrics.usage import (
    EnvironmentUsage,
    UsageType,
    environment_usage,
    fixed_resources,
    month_of,
    parse_month,
)

router = APIRouter()

MONTH_PATTERN: Final = r"^[0-9]{4}-(0[1-9]|1[0-2])$"

_SELECT_ENV = text(
    "select e.id from ssc.environment e where e.org_id = :org and e.app_id = :app and e.id = :env"
)


class UsageQuery(Strict):
    month: str | None = Field(
        default=None, pattern=MONTH_PATTERN, description="`YYYY-MM` (UTC); this month if left out."
    )


class UsageOut(Strict):
    environment_id: str
    app_id: str | None
    month: str = Field(description="`YYYY-MM` (UTC).")
    usage_type: UsageType | None = Field(
        description="`rare`, `daily`, `session` or `heavy`, read from this month's usage after "
        "the fact; null with no usage. Never used to size or bill anything."
    )
    session_hours: float = Field(description="Hours with at least one session open.")
    instance_hours: float = Field(description="Hours of running instance the cell counted.")
    cold_starts: int = Field(description="How many times an instance started.")
    cold_start_p50_seconds: float | None = Field(
        description="Median start time; null under 20 cold starts (`small_sample`)."
    )
    cold_start_p95_seconds: float | None = Field(
        description="95th percentile start time; null under 20 cold starts (`small_sample`)."
    )
    small_sample: bool = Field(description="Fewer than 20 cold starts: no percentiles.")
    active_days: int = Field(description="Days with any running instance.")


class FixedResourceOut(Strict):
    resource: str = Field(description="`database`, `egress` or `connections`.")
    created_at: datetime


class CellUsageOut(Strict):
    month: str
    environments: list[UsageOut]
    fixed_resources: list[FixedResourceOut]


def _month(query: UsageQuery) -> date:
    return month_of(datetime.now(UTC)) if query.month is None else parse_month(query.month)


def _out(month: date, found: EnvironmentUsage) -> UsageOut:
    return UsageOut(
        environment_id=found.environment_id,
        app_id=found.app_id,
        month=month.strftime("%Y-%m"),
        usage_type=found.usage_type,
        session_hours=found.session_hours,
        instance_hours=found.instance_hours,
        cold_starts=found.cold_starts,
        cold_start_p50_seconds=found.cold_start_p50_seconds,
        cold_start_p95_seconds=found.cold_start_p95_seconds,
        small_sample=found.small_sample,
        active_days=found.active_days,
    )


@router.get(
    "/apps/{app_id}/environments/{environment_id}/usage",
    response_model=UsageOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.NOT_FOUND),
)
async def get_usage(
    app_id: Id, environment_id: Id, params: Annotated[UsageQuery, Query()], uow: UserUoW
) -> UsageOut:
    """One environment's usage this month, or in ``month``. Zero when none was recorded."""
    found_env = {"org": uow.org_id, "app": app_id, "env": environment_id}
    if (await uow.conn.execute(_SELECT_ENV, found_env)).first() is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    month = _month(params)
    found = await environment_usage(uow.conn, uow.org_id, month, [environment_id])
    if found:
        return _out(month, found[0])
    return _out(
        month,
        EnvironmentUsage(
            environment_id=environment_id,
            app_id=app_id,
            session_hours=0.0,
            instance_hours=0.0,
            cold_starts=0,
            cold_start_p50_seconds=None,
            cold_start_p95_seconds=None,
            small_sample=True,
            active_days=0,
            usage_type=None,
        ),
    )


@router.get(
    "/usage",
    response_model=CellUsageOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def get_cell_usage(params: Annotated[UsageQuery, Query()], uow: UserUoW) -> CellUsageOut:
    """Every environment's usage in the month and the cell's fixed resources. Admins only."""
    await require_admin(uow)
    month = _month(params)
    found = await environment_usage(uow.conn, uow.org_id, month)
    fixed = await fixed_resources(uow.conn, uow.org_id)
    return CellUsageOut(
        month=month.strftime("%Y-%m"),
        environments=[_out(month, f) for f in found],
        fixed_resources=[
            FixedResourceOut(resource=f.resource, created_at=f.created_at) for f in fixed
        ],
    )
