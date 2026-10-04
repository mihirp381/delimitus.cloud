"""The warm option (SSC-092): production environments, and optionally the cell's gateway, kept at
one instance so their first load has no cold start. Everyone else pays nothing for idle.

Active org admins only, and never in an agent session: an agent cannot spend the customer's money
(SSC-048). The change carries the monthly cost the console showed, which must be what the setting
costs; the audit row (``org.updated`` on ``warm``) records who set it and that cost. The figures
are not a bill and nothing charges for the option (A6).

Each production environment says whether the usage events suggest it (``metrics.warm_hint``).
A warm environment behind a gateway that is not warm still waits for the gateway to start; the
console says so and offers the gateway in the same step.
"""

from datetime import UTC, datetime
from typing import Final, Literal

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.cells import WARM_ENVIRONMENT_MONTHLY_USD, WARM_GATEWAY_MONTHLY_USD
from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_admin
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Strict
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.cell import warm
from ssc_control.metrics.warm_hint import HINT_DAYS, hint_working_days, warm_hints

router = APIRouter()

_PROD = text(
    "select e.id, e.app_id, a.slug from ssc.environment e "
    "join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "where e.org_id = :org and e.name = 'prod' order by a.slug"
)
_CHANGE: Final = (
    *AUTHENTICATED,
    ErrorCode.FORBIDDEN,
    ErrorCode.AGENT_SESSION_REFUSED,
    ErrorCode.VALIDATION_FAILED,
    ErrorCode.REFERENCE_NOT_FOUND,
)


class WarmEnvironmentOut(Strict):
    environment_id: str
    app_id: str
    app_slug: str
    warm: bool
    suggested: bool = Field(
        description="Opened on most working days lately, with users meeting cold starts."
    )
    opened_days: int = Field(description=f"Working days in the last {HINT_DAYS} it was used.")
    cold_start_days: int = Field(description="Of those days, how many had a cold start.")


class WarmGatewayOut(Strict):
    warm: bool = Field(description="Whether an admin asked for the gateway to be kept warm.")
    state: Literal["off", "on", "turning_on", "turning_off", "failed"] = Field(
        description="Where the cell's gateway is: `turning_on` until the cell deployer has set it."
    )
    failure_code: str | None


class WarmOut(Strict):
    environments: list[WarmEnvironmentOut] = Field(
        description="Every production environment. Preview environments are never warm."
    )
    gateway: WarmGatewayOut
    working_days: int = Field(description=f"Working days in the last {HINT_DAYS}.")
    environment_monthly_usd: int = Field(description="About what each warm environment adds.")
    gateway_monthly_usd: int = Field(description="About what the warm gateway adds.")
    monthly_usd: int = Field(description="About what the option adds a month as set now.")


class WarmIn(Strict):
    environment_ids: list[str] = Field(
        max_length=warm.MAX_ENVIRONMENTS,
        description="The production environments to keep warm; every other is not.",
    )
    gateway: bool = Field(description="Keep the cell's gateway warm too.")
    monthly_usd_shown: int = Field(
        ge=0, description="The monthly cost shown for this setting; refused if it is not that."
    )


async def _warm(uow: UnitOfWork) -> WarmOut:
    now = datetime.now(UTC)
    setting = await warm.read(uow.conn, uow.org_id)
    hints = await warm_hints(uow.conn, uow.org_id, now)
    named = set(setting.environment_ids)
    environments: list[WarmEnvironmentOut] = []
    for env_id, app_id, slug in (await uow.conn.execute(_PROD, {"org": uow.org_id})).all():
        hint = hints.get(str(env_id))
        environments.append(
            WarmEnvironmentOut(
                environment_id=env_id,
                app_id=app_id,
                app_slug=slug,
                warm=env_id in named,
                suggested=env_id not in named and hint is not None and hint.suggested,
                opened_days=0 if hint is None else hint.opened_days,
                cold_start_days=0 if hint is None else hint.cold_start_days,
            )
        )
    return WarmOut(
        environments=environments,
        gateway=WarmGatewayOut(
            warm=setting.gateway.wanted,
            state=setting.gateway.state,
            failure_code=setting.gateway.failure_code,
        ),
        working_days=hint_working_days(now),
        environment_monthly_usd=WARM_ENVIRONMENT_MONTHLY_USD,
        gateway_monthly_usd=WARM_GATEWAY_MONTHLY_USD,
        monthly_usd=setting.monthly_usd,
    )


@router.get(
    "/warm",
    response_model=WarmOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def get_warm(uow: UserUoW) -> WarmOut:
    """Active org admins only (``FORBIDDEN``). Which production environments and whether the
    gateway are kept warm, what that costs, and where the usage events suggest it."""
    await require_admin(uow)
    return await _warm(uow)


@router.put(
    "/warm",
    response_model=WarmOut,
    responses=problem_responses(*_CHANGE),
)
async def set_warm(body: WarmIn, uow: UserUoW) -> WarmOut:
    """Set the warm option. Active org admins only, never in an agent session. Names every
    production environment to keep warm (the others go back to zero on the runtime's next pass)
    and whether the gateway is kept warm. ``REFERENCE_NOT_FOUND`` for an environment the org does
    not have; ``VALIDATION_FAILED`` for a preview environment or a ``monthly_usd_shown`` that is
    not what the setting costs. Setting what is already set changes nothing; a change is audited
    as ``org.updated`` on ``warm`` with the cost shown."""
    await require_admin(uow)
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    try:
        await warm.set_warm(
            uow.conn,
            org_id=uow.org_id,
            environment_ids=body.environment_ids,
            gateway=body.gateway,
            monthly_usd_shown=body.monthly_usd_shown,
            actor=actor_of(uow.principal),
        )
    except warm.WarmError as exc:
        raise Refusal(exc.code, evidence=exc.evidence) from None
    return await _warm(uow)
