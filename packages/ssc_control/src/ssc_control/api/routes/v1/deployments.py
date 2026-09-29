"""Deployments as long-running operations, and polling them."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.uow import UserUoW

router = APIRouter()


class DeploymentCreate(Strict):
    release_id: str = Field(pattern=r"^rel_[a-z0-9]{20}$")
    kind: Literal["deploy", "rollback"] = "deploy"


class OperationAccepted(Strict):
    operation_id: str
    state: Literal["pending"]


class OperationOut(Strict):
    operation_id: str
    kind: Literal["deploy", "rollback"]
    state: Literal["pending", "running", "healthy", "failed", "superseded"]
    app_id: str
    environment_id: str
    release_id: str
    started_at: datetime
    finished_at: datetime | None


_SELECT_ENV_VERSIONS = text(
    "select config_version, grants_version from ssc.environment "
    "where org_id = :org and app_id = :app and id = :env"
)
_INSERT_DEPLOYMENT = text(
    "insert into ssc.deployment (id, org_id, app_id, environment_id, release_id, kind, state, "
    "config_version, grants_version, actor_kind, actor_id, actor_via_agent, actor_client_id) "
    "values (:id, :org, :app, :env, :rel, :kind, 'pending', :cv, :gv, :actor_kind, :actor_id, "
    ":via_agent, :client_id)"
)
_SELECT_DEPLOYMENT = text(
    "select id as operation_id, kind, state, app_id, environment_id, release_id, started_at, "
    "finished_at from ssc.deployment where org_id = :org and id = :id"
)


@router.post(
    "/apps/{app_id}/environments/{environment_id}/deployments",
    status_code=202,
    response_model=OperationAccepted,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.NOT_FOUND,
        ErrorCode.DEPLOYMENT_IN_FLIGHT,
        ErrorCode.REFERENCE_NOT_FOUND,
    ),
)
async def create_deployment(
    app_id: Id, environment_id: Id, body: DeploymentCreate, uow: UserUoW
) -> Response:
    """Start a deployment: 202 plus a ``Location`` to poll. Running it is the reconciler's job."""
    env = (
        await uow.conn.execute(
            _SELECT_ENV_VERSIONS, {"org": uow.org_id, "app": app_id, "env": environment_id}
        )
    ).first()
    if env is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    dep_id = new_id("dep")
    p = uow.principal
    await uow.conn.execute(
        _INSERT_DEPLOYMENT,
        {
            "id": dep_id,
            "org": uow.org_id,
            "app": app_id,
            "env": environment_id,
            "rel": body.release_id,
            "kind": body.kind,
            "cv": int(env[0]),
            "gv": int(env[1]),
            "actor_kind": p.kind.value,
            "actor_id": p.subject,
            "via_agent": p.is_agent,
            "client_id": p.client_id,
        },
    )
    action = AuditAction.DEPLOY_STARTED if body.kind == "deploy" else AuditAction.ROLLBACK_STARTED
    await uow.audit(
        action,
        target_kind="deployment",
        target_id=dep_id,
        after={"environment_id": environment_id, "release_id": body.release_id},
    )
    return uow.reply(
        OperationAccepted(operation_id=dep_id, state="pending"),
        status=202,
        headers={"Location": f"/v1/operations/{dep_id}"},
    )


@router.get(
    "/operations/{operation_id}",
    response_model=OperationOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.NOT_FOUND),
)
async def get_operation(operation_id: Id, uow: UserUoW) -> OperationOut:
    row = (
        (await uow.conn.execute(_SELECT_DEPLOYMENT, {"org": uow.org_id, "id": operation_id}))
        .mappings()
        .first()
    )
    if row is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"operation_id": operation_id})
    return OperationOut(**dict(row))
