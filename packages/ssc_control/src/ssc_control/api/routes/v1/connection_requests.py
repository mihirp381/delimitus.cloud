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
from ssc_control.api.routes.v1.approvals import Approval, approval_out
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.routes.v1.connections import EnvironmentConnectionOut, Name, environment_out
from ssc_control.api.routes.v1.grants import link_approved
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
_APP_STATUS: Final = text("select status from ssc.app where org_id = :org and id = :app for share")

Outcome = Literal["linked", "already_linked", "pending", "approved_not_linked"]


class ConnectionRequestIn(Strict):
    pass


class ConnectionRequestOut(Strict):
    outcome: Outcome
    reason: str | None
    approval: Approval | None
    connections: list[EnvironmentConnectionOut]


async def _rule_allows(uow: UnitOfWork, connection: service.Connection, requester: str) -> bool:
    rule = connection.preview_access
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
    `connection.granted` with a `connection.preview_access` policy decision). Otherwise, and
    always on prod, a `connect_data_source` request is opened (`pending`); one approved earlier
    links now (`linked`) or says why not (`approved_not_linked`, `reason` `ceiling` or
    `connection_missing`)."""
    del body
    params = {"org": uow.org_id, "app": app_id, "env": environment_id}
    env_name = (await uow.conn.execute(_LOCK_ENV, params)).scalar()
    status = (await uow.conn.execute(_APP_STATUS, params)).scalar()
    if env_name is None or status is None or status == "deleted":
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    if status == "deleting":
        raise Refusal(ErrorCode.APP_DELETING, evidence={"app_id": app_id})
    by = await require_builder(uow, environment_id)
    connection = await service.get(uow.conn, uow.org_id, name)
    if connection is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"connection": name})

    async def answer(
        outcome: Outcome, reason: str | None = None, approval: Approval | None = None
    ) -> Response:
        links = await environment_out(uow, environment_id, None)
        out = ConnectionRequestOut(
            outcome=outcome, reason=reason, approval=approval, connections=links.connections
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
    if row.state != "approved":
        return await answer("pending", approval=approval_out(row))
    applied, reason = await link_approved(uow, row)
    if applied == "applied":
        return await answer("linked", approval=approval_out(row))
    return await answer("approved_not_linked", reason, approval_out(row))
