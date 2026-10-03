"""App secrets (SSC-026, decision 022): references and versions, never values.

A value never passes through this API. ``ssc secret set NAME`` runs three steps:

1. ``POST .../secrets/{name}/grants``: the cell agent creates the secret ``ssc-a-<env>-<NAME>``
   if missing, readable only by the environment's own identity, and this answers with a
   single-purpose write grant for the cell's secret intake (``runtime.secret_grants``).
2. The command line PUTs the value to the intake, which adds it as a new version and answers
   with the version's number.
3. ``PUT .../secrets/{name}`` records that number: ``secret.bound`` the first time,
   ``secret.rotated`` after, and the environment's ``config_version`` goes up. When the
   environment has a live deployment, a deployment of the same release starts at once, which
   pins the new version (``deployment.secret_refs``); rotation is that new deployment.

Every route needs an org admin, the app's owner or a builder on the environment, and refuses an
agent credential (``AGENT_SESSION_REFUSED``): no AI agent handles a secret. There is no route
that reads a value, and none may be added: ``test_secrets`` checks every response schema.
"""

import logging
from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Path, Request, Response
from pydantic import Field
from sqlalchemy import RowMapping, text

from ssc_contracts.app_env import secret_name_problem
from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.authz import require_builder
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.bundles import UploadTarget
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.routes.v1.deployments import start_deployment
from ssc_control.api.runtime import runtime_of
from ssc_control.api.uow import UnitOfWork, UserUoW
from ssc_control.runtime.secret_grants import SecretGrantError
from ssc_shared.runtime import SECRET_VERSION, secret_id, service_name

log = logging.getLogger(__name__)
router = APIRouter()

VERSION_PATTERN: Final = f"^{SECRET_VERSION.pattern}$"

SecretName = Annotated[
    str,
    Path(
        pattern=r"^[A-Z][A-Z0-9_]{0,63}$",
        description="The secret's name, which is also the environment variable it arrives in.",
    ),
]


class SecretOut(Strict):
    name: str
    version: str = Field(description="The version set last. A reference, never the value.")
    live_version: str | None = Field(
        description="The version the live deployment runs; null when it runs none."
    )
    updated_at: datetime


class SecretList(Strict):
    environment_id: str
    items: list[SecretOut] = Field(description="By name.")


class SecretGrantOut(Strict):
    name: str
    upload: UploadTarget = Field(
        description="PUT the value here, straight to the cell. The answer names the new version."
    )


class SecretSet(Strict):
    version: str = Field(
        pattern=VERSION_PATTERN, description="The version number the secret intake answered."
    )


class SecretSetOut(Strict):
    name: str
    version: str
    changed: bool = Field(description="False when the secret already had this version.")
    operation_id: str | None = Field(
        description="The deployment that puts the version live; null when nothing changed or "
        "the environment has no live deployment yet (its next deployment takes it)."
    )


_LOCK_ENV = text(
    "select e.name, e.config_version, e.grants_version, e.current_deployment_id, a.status "
    "from ssc.environment e join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "where e.org_id = :org and e.app_id = :app and e.id = :env for update of e"
)
_SELECT_ENV = text(_LOCK_ENV.text.removesuffix(" for update of e"))
_SELECT_SECRETS = text(
    "select s.name, s.secret_version as version, s.updated_at, d.secret_refs ->> s.name "
    "as live_version from ssc.secret_ref s "
    "join ssc.environment e on e.org_id = s.org_id and e.id = s.environment_id "
    "left join ssc.deployment d on d.org_id = e.org_id and d.id = e.current_deployment_id "
    "where s.org_id = :org and s.environment_id = :env order by s.name"
)
_SELECT_SECRET = text(
    "select id, secret_version from ssc.secret_ref "
    "where org_id = :org and environment_id = :env and name = :name"
)
_IN_FLIGHT = text(
    "select id from ssc.deployment where org_id = :org and environment_id = :env "
    "and state in ('pending', 'running') limit 1"
)
_INSERT_SECRET = text(
    "insert into ssc.secret_ref (id, org_id, environment_id, name, secret_version) "
    "values (:id, :org, :env, :name, :version)"
)
_UPDATE_SECRET = text(
    "update ssc.secret_ref set secret_version = :version, updated_at = now() "
    "where org_id = :org and id = :id"
)
_BUMP_CONFIG = text(
    "update ssc.environment set config_version = config_version + 1 "
    "where org_id = :org and id = :env returning config_version"
)
_LIVE_RELEASE = text("select release_id from ssc.deployment where org_id = :org and id = :id")


async def _environment(
    uow: UnitOfWork, app_id: str, environment_id: str, *, lock: bool = False
) -> RowMapping:
    """``NOT_FOUND``, then ``FORBIDDEN`` for a caller who may not change the environment."""
    params = {"org": uow.org_id, "app": app_id, "env": environment_id}
    query = _LOCK_ENV if lock else _SELECT_ENV
    env = (await uow.conn.execute(query, params)).mappings().first()
    if env is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    await require_builder(uow, environment_id)
    return env


def _writable(uow: UnitOfWork, env: RowMapping, app_id: str, name: str) -> None:
    """A person, an active app and a name that may be a secret."""
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    if env["status"] != "active":
        raise Refusal(ErrorCode.APP_NOT_ACTIVE, evidence={"app_id": app_id})
    if (problem := secret_name_problem(name)) is not None:
        raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"name": name, "problem": problem})


@router.get(
    "/apps/{app_id}/environments/{environment_id}/secrets",
    response_model=SecretList,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def list_secrets(app_id: Id, environment_id: Id, uow: UserUoW) -> SecretList:
    """The environment's secrets: names and versions only."""
    await _environment(uow, app_id, environment_id)
    rows = await uow.conn.execute(_SELECT_SECRETS, {"org": uow.org_id, "env": environment_id})
    return SecretList(
        environment_id=environment_id, items=[SecretOut(**r) for r in rows.mappings()]
    )


@router.post(
    "/apps/{app_id}/environments/{environment_id}/secrets/{name}/grants",
    status_code=201,
    response_model=SecretGrantOut,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.APP_NOT_ACTIVE,
        ErrorCode.AGENT_SESSION_REFUSED,
        ErrorCode.SECRETS_UNAVAILABLE,
    ),
)
async def grant_secret_upload(
    app_id: Id, environment_id: Id, name: SecretName, request: Request, uow: UserUoW
) -> Response:
    """Prepare the secret in the cell and grant one upload of its next value, straight to the
    cell's secret intake, for a few minutes. Nothing is recorded until ``PUT``."""
    env = await _environment(uow, app_id, environment_id)
    _writable(uow, env, app_id, name)
    grants = runtime_of(request).secret_grants
    if grants is None:
        raise Refusal(ErrorCode.SECRETS_UNAVAILABLE, evidence={"reason": "not_configured"})
    try:
        grant = await grants.grant(secret_id(service_name(environment_id), name))
    except SecretGrantError as exc:
        raise Refusal(
            ErrorCode.SECRETS_UNAVAILABLE, evidence={"reason": "cell", "error": str(exc)}
        ) from None
    upload = UploadTarget(
        method="PUT",
        url=grant.url,
        headers={
            "Authorization": f"Bearer {grant.token}",
            "Content-Type": "application/octet-stream",
        },
        expires_at=grant.expires_at,
    )
    return uow.reply(SecretGrantOut(name=name, upload=upload), status=201)


@router.put(
    "/apps/{app_id}/environments/{environment_id}/secrets/{name}",
    response_model=SecretSetOut,
    responses={
        202: {
            "model": SecretSetOut,
            "description": "Recorded; a deployment of the live release started to put it live. "
            "Poll the `Location`.",
        },
        **problem_responses(
            *AUTHENTICATED,
            ErrorCode.FORBIDDEN,
            ErrorCode.NOT_FOUND,
            ErrorCode.VALIDATION_FAILED,
            ErrorCode.APP_NOT_ACTIVE,
            ErrorCode.AGENT_SESSION_REFUSED,
            ErrorCode.DEPLOYMENT_IN_FLIGHT,
        ),
    },
)
async def set_secret(  # noqa: PLR0913  (FastAPI maps each parameter to the request)
    *,
    app_id: Id,
    environment_id: Id,
    name: SecretName,
    body: SecretSet,
    request: Request,
    uow: UserUoW,
) -> Response:
    """Record the version the intake added. The same version again changes nothing (200). A new
    one while a deployment is in flight is ``DEPLOYMENT_IN_FLIGHT``; otherwise it is recorded and,
    when the environment has a live deployment, a deployment of its release starts (202)."""
    env = await _environment(uow, app_id, environment_id, lock=True)
    _writable(uow, env, app_id, name)
    params = {"org": uow.org_id, "env": environment_id, "name": name, "version": body.version}
    current = (await uow.conn.execute(_SELECT_SECRET, params)).first()
    if current is not None and current.secret_version == body.version:
        unchanged = SecretSetOut(name=name, version=body.version, changed=False, operation_id=None)
        return uow.reply(unchanged)
    in_flight = (await uow.conn.execute(_IN_FLIGHT, params)).scalar()
    if in_flight is not None:
        raise Refusal(ErrorCode.DEPLOYMENT_IN_FLIGHT, evidence={"operation_id": in_flight})
    after = {"environment_id": environment_id, "name": name, "version": body.version}
    if current is None:
        ref_id = new_id("sec")
        await uow.conn.execute(_INSERT_SECRET, {**params, "id": ref_id})
        await uow.audit(
            AuditAction.SECRET_BOUND, target_kind="secret_ref", target_id=ref_id, after=after
        )
    else:
        await uow.conn.execute(_UPDATE_SECRET, {**params, "id": current.id})
        await uow.audit(
            AuditAction.SECRET_ROTATED,
            target_kind="secret_ref",
            target_id=str(current.id),
            before={**after, "version": current.secret_version},
            after=after,
        )
    config_version = (await uow.conn.execute(_BUMP_CONFIG, params)).scalar_one()
    live = env["current_deployment_id"]
    if live is None:
        out = SecretSetOut(name=name, version=body.version, changed=True, operation_id=None)
        return uow.reply(out)
    release_id = (await uow.conn.execute(_LIVE_RELEASE, {**params, "id": live})).scalar_one()
    dep_id = await start_deployment(
        uow,
        request,
        app_id=app_id,
        environment_id=environment_id,
        env_name=str(env["name"]),
        versions=(int(config_version), int(env["grants_version"])),
        release_id=str(release_id),
        kind="deploy",
    )
    out = SecretSetOut(name=name, version=body.version, changed=True, operation_id=dep_id)
    return uow.reply(out, status=202, headers={"Location": f"/v1/operations/{dep_id}"})
