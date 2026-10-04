"""What agents may do in the org beyond a person's own rules (SSC-048): today, whether an agent
credential may read logs. On by default; an org admin turns it off in a normal session, never
through an agent. Turned off, the logs route answers an agent with ``AGENT_LOGS_OFF`` and the MCP
``get_logs`` tool returns that refusal; the person's own credential still reads.
"""

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_admin
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Strict
from ssc_control.api.uow import UserUoW

router = APIRouter()

_READ = text("select agent_logs from ssc.org where id = :org")
_LOCK = text("select agent_logs from ssc.org where id = :org for update")
_SET = text("update ssc.org set agent_logs = :logs where id = :org")


class AgentPolicy(Strict):
    logs: bool = Field(
        description="Whether an agent credential may read logs. A person's own never is limited."
    )


@router.get(
    "/org/agent-policy",
    response_model=AgentPolicy,
    responses=problem_responses(*AUTHENTICATED),
)
async def get_agent_policy(uow: UserUoW) -> AgentPolicy:
    """Anyone in the org, agents included, may read it."""
    logs = (await uow.conn.execute(_READ, {"org": uow.org_id})).scalar_one()
    return AgentPolicy(logs=bool(logs))


@router.put(
    "/org/agent-policy",
    response_model=AgentPolicy,
    responses=problem_responses(
        *AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.AGENT_SESSION_REFUSED
    ),
)
async def set_agent_policy(body: AgentPolicy, uow: UserUoW) -> AgentPolicy:
    """Active org admins only, never in an agent session. Audited as ``org.updated`` when it
    changes; setting the value it already has changes nothing."""
    await require_admin(uow)
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    before = bool((await uow.conn.execute(_LOCK, {"org": uow.org_id})).scalar_one())
    if before != body.logs:
        await uow.conn.execute(_SET, {"org": uow.org_id, "logs": body.logs})
        await uow.audit(
            AuditAction.ORG_UPDATED,
            target_kind="org",
            target_id=uow.org_id,
            before={"agent_logs": before},
            after={"agent_logs": body.logs},
        )
    return AgentPolicy(logs=body.logs)
