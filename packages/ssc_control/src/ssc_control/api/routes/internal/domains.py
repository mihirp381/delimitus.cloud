"""``/internal/v1/orgs/{org_id}/domain``: an operator retires an org's customer domain (CD-4.2).
Operator credentials only."""

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from ssc_contracts.errors import ErrorCode
from ssc_control.api.auth import PrincipalKind
from ssc_control.api.idempotency import InternalIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id
from ssc_control.api.routes.v1.domains import DelegationOut, agent_of, delegation_out
from ssc_control.api.uow import InternalUoW, actor_of
from ssc_control.db.domains import domain_of
from ssc_control.domains.lifecycle import RetireRefusedError, retire
from ssc_control.runtime.cell_domains import CellDomainsError

router = APIRouter(prefix="/orgs")

_APPS_SUFFIX = text("select apps_suffix from ssc.org where id = :org")


@router.post(
    "/{org_id}/domain/retire",
    response_model=DelegationOut,
    dependencies=[InternalIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.DOMAIN_STATE,
        ErrorCode.CELL_UNAVAILABLE,
    ),
)
async def retire_domain(request: Request, org_id: Id, uow: InternalUoW) -> Response:
    """Retire the org's customer domain once its parent no longer delegates it, or after 12
    months. ``DOMAIN_STATE`` names why not, and refuses the org's current suffix. Audited as
    ``domain.retired``."""
    if uow.principal.kind is not PrincipalKind.OPERATOR:
        raise Refusal(
            ErrorCode.FORBIDDEN,
            evidence={"reason": "domain_retire_is_operator_only", "kind": uow.principal.kind.value},
        )
    if org_id != uow.org_id:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"reason": "not_the_credential_org"})
    row = await domain_of(uow.conn, org_id)
    if row is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"org_id": org_id})
    apps_suffix = (await uow.conn.execute(_APPS_SUFFIX, {"org": org_id})).scalar_one()
    if row.suffix == apps_suffix:
        raise Refusal(
            ErrorCode.DOMAIN_STATE, evidence={"reason": "the organization uses the domain"}
        )
    agent = await agent_of(request, org_id)
    try:
        retired = await retire(uow.conn, agent, row, actor_of(uow.principal), datetime.now(UTC))
    except RetireRefusedError as exc:
        raise Refusal(ErrorCode.DOMAIN_STATE, evidence={"reason": str(exc)}) from None
    except CellDomainsError as exc:
        raise Refusal(ErrorCode.CELL_UNAVAILABLE, evidence={"error": str(exc)}) from None
    return uow.reply(delegation_out(retired))
