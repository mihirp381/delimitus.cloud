"""Asking for an environment to use a data connection (ticket 14).

On preview the connection's preview access rule may link it at once, with a ``policy_decision``
row as the record; otherwise, and always on prod, a ``connect_data_source`` approval is opened.
"""

from typing import Final, Literal

from fastapi import APIRouter, Response
from sqlalchemy import text

from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_builder
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import POST_COMMON, problem_responses
from ssc_control.api.routes.v1.approvals import Approval, approval_named
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.routes.v1.connections import EnvironmentConnectionOut, Name, environment_out
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.approvals import service as approvals
from ssc_control.approvals.policy import record_policy_decision
from ssc_control.connections import service
from ssc_control.domain.approval_rules import Requirement, RequirementKind

router = APIRouter()

_LOCK_ENV: Final = text(
    "select name from ssc.environment where org_id = :org and app_id = :app and id = :env "
    "for update"
)

Outcome = Literal["linked", "already_linked", "pending"]
LINKED_BY_RULE: Final = "linked by the preview access rule"


class ConnectionRequestIn(Strict):
    pass


class ConnectionRequestOut(Strict):
    outcome: Literal[Outcome, "approved_not_linked"]
    reason: str | None
    approval: Approval | None
    connections: list[EnvironmentConnectionOut]


async def _rule_allows(uow: UnitOfWork, connection: service.Connection, requester: str) -> bool:
    rule = connection.preview_access
    if connection.status != "active":
        return False
    if rule.who == "builders":
        return True
    if rule.who == "group" and rule.group_id is not None:
        return await service.active_member(uow.conn, uow.org_id, rule.group_id, requester)
    return False


async def _link_by_rule(
    uow: UnitOfWork, app_id: str, environment_id: str, connection: service.Connection, by: str
) -> None:
    rule = connection.preview_access
    pol = await record_policy_decision(
        uow.conn,
        org_id=uow.org_id,
        principal_kind="user",
        principal_id=by,
        action="connection.preview_access",
        target_kind="connection",
        target_id=connection.id,
        outcome="allow",
        reason=rule.who,
        inputs={
            "environment_id": environment_id,
            "app_id": app_id,
            "connection": connection.name,
            "group_id": rule.group_id,
            "via_agent": uow.principal.is_agent,
        },
    )
    await service.grant(
        uow.conn,
        org_id=uow.org_id,
        actor=actor_of(uow.principal),
        connection=connection,
        environment_id=environment_id,
        limits={},
        by_user_id=by,
        policy_decision_id=pol,
    )
    req = Requirement(RequirementKind.CONNECT_DATA_SOURCE, connection.name)
    found = (
        await approvals.newest(
            uow.conn, org_id=uow.org_id, environment_id=environment_id, requirements=[req]
        )
    ).get(req)
    if found is not None and found.state == "pending":
        await approvals.cancel(
            uow.conn,
            org_id=uow.org_id,
            approval_id=found.id,
            user_id=found.requested_by_user_id,
            channel=None,
            reason=LINKED_BY_RULE,
            actor=actor_of(uow.principal),
        )


@router.post(
    "/apps/{app_id}/environments/{environment_id}/connections/{name}/request",
    response_model=ConnectionRequestOut,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND, ErrorCode.APP_DELETING
    ),
)
async def request_connection(  # noqa: PLR0913  (FastAPI maps each parameter)
    app_id: Id, environment_id: Id, name: Name, body: ConnectionRequestIn, uow: UserUoW
) -> Response:
    """Ask for the environment to use a connection. For whoever may build the environment, agent
    sessions included. `already_linked` writes nothing. On preview, when the connection's
    preview access rule covers the caller (`builders`, or `group` and an active member) and the
    environment's audience is inside the ceiling, the link is made at once (`linked`, audited
    `connection.granted` with a `connection.preview_access` policy decision; a pending request
    for it is cancelled). A suspended connection is never linked by its rule. Otherwise, and
    always on prod, a `connect_data_source` request is opened (`pending`); one approved earlier
    whose link was revoked is spent, and a new one is opened. `approved_not_linked` is never
    answered and `reason` is always null."""
    del body
    params = {"org": uow.org_id, "app": app_id, "env": environment_id}
    env_name = (await uow.conn.execute(_LOCK_ENV, params)).scalar()
    if env_name is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    by = await require_builder(uow, environment_id)
    connection = await service.get(uow.conn, uow.org_id, name)
    if connection is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"connection": name})

    async def answer(outcome: Outcome, approval: Approval | None = None) -> Response:
        links = await environment_out(uow, environment_id)
        out = ConnectionRequestOut(
            outcome=outcome, reason=None, approval=approval, connections=links.connections
        )
        return uow.reply(out)

    held = await service.links(uow.conn, uow.org_id, environment_id)
    if any(link.connection.name == name for link in held):
        return await answer("already_linked")
    if env_name == "preview" and await _rule_allows(uow, connection, by):
        grants = await service.environment_grants(uow.conn, uow.org_id, environment_id)
        if not await service.over_ceiling(uow.conn, uow.org_id, connection, grants):
            await _link_by_rule(uow, app_id, environment_id, connection, by)
            return await answer("linked")
    row, _ = await approvals.request(
        uow.conn,
        org_id=uow.org_id,
        environment_id=environment_id,
        requirement=Requirement(RequirementKind.CONNECT_DATA_SOURCE, name),
        requested_by=by,
        via_agent=uow.principal.is_agent,
        payload={},
        actor=actor_of(uow.principal),
    )
    return await answer("pending", approval=await approval_named(uow, row))
