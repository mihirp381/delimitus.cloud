"""``/v1``: the API people, the command line, the GitHub Action and the console use.

Enough real endpoints to prove the conventions end to end: apps (create, list, read), sharing
rules behind ``If-Match``, deployments as long-running operations, ``whoami``. Sharing-rule
semantics belong to SSC-021, running a deployment to SSC-016/SSC-017; both keep these shapes.
"""

import re
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Header, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.auth import PrincipalKind, UserPrincipal
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.ratelimit import limit
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.uow import UnitOfWork, UserUoW

router = APIRouter(prefix="/v1", tags=["v1"])

IF_MATCH: Final = "If-Match"
ETAG: Final = "ETag"
_ETAG_RE: Final = re.compile(r'^(?:W/)?"?(\d{1,18})"?$')

Id = Annotated[str, Path(pattern=r"^[a-z]{3}_[a-z0-9]{20}$")]
Slug = Annotated[
    str,
    Field(
        pattern=r"^[a-z]([a-z0-9-]{0,38}[a-z0-9])?$",
        description="Host label of the app. Lower-case, no leading digit, no `--`.",
    ),
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Whoami(Strict):
    org_id: str
    subject: str
    kind: PrincipalKind
    credential_id: str
    is_agent: bool
    client_id: str | None


class AppCreate(Strict):
    slug: Slug


class EnvironmentOut(Strict):
    id: str
    name: Literal["prod", "preview"]
    config_version: int
    grants_version: int
    current_deployment_id: str | None


class AppOut(Strict):
    id: str
    slug: str
    owner_user_id: str
    status: Literal["active", "disabled", "quarantined"]
    created_at: datetime
    environments: list[EnvironmentOut]


class AppSummary(Strict):
    id: str
    slug: str
    owner_user_id: str
    status: Literal["active", "disabled", "quarantined"]


class AppList(Strict):
    apps: list[AppSummary]


class GrantIn(Strict):
    role: Literal["builder", "user"]
    subject_kind: Literal["user", "group", "org"]
    subject_id: str | None = Field(
        default=None, description="A `usr_` or `grp_` id; absent when subject_kind is `org`."
    )


class GrantsIn(Strict):
    grants: list[GrantIn] = Field(max_length=200)


class GrantOut(Strict):
    id: str
    role: Literal["builder", "user"]
    subject_kind: Literal["user", "group", "org"]
    subject_id: str | None


class GrantsOut(Strict):
    environment_id: str
    grants_version: int
    grants: list[GrantOut]


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


# ── whoami ───────────────────────────────────────────────────────────────────


@router.get("/whoami", response_model=Whoami, responses=AUTHENTICATED)
def whoami(request: Request, principal: UserPrincipal) -> Whoami:
    limit(request, principal)
    return Whoami(
        org_id=principal.org_id,
        subject=principal.subject,
        kind=principal.kind,
        credential_id=principal.credential_id,
        is_agent=principal.is_agent,
        client_id=principal.client_id,
    )


# ── apps ─────────────────────────────────────────────────────────────────────

_INSERT_APP = text(
    "insert into ssc.app (id, org_id, slug, owner_user_id) values (:id, :org, :slug, :owner) "
    "returning id, slug, owner_user_id, status, created_at"
)
_INSERT_ENV = text(
    "insert into ssc.environment (id, org_id, app_id, name) values (:id, :org, :app, :name)"
)
_SELECT_APP = text(
    "select id, slug, owner_user_id, status, created_at from ssc.app "
    "where org_id = :org and id = :id"
)
_SELECT_APPS = text(
    "select id, slug, owner_user_id, status from ssc.app where org_id = :org order by slug"
)
_SELECT_ENVS = text(
    "select id, name, config_version, grants_version, current_deployment_id "
    "from ssc.environment where org_id = :org and app_id = :app order by name"
)


def _require_user(uow: UnitOfWork) -> str:
    if uow.principal.kind is not PrincipalKind.USER:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"kind": uow.principal.kind.value})
    return uow.principal.subject


async def _environments(uow: UnitOfWork, app_id: str) -> list[EnvironmentOut]:
    rows = (await uow.conn.execute(_SELECT_ENVS, {"org": uow.org_id, "app": app_id})).mappings()
    return [EnvironmentOut(**dict(r)) for r in rows]


@router.post(
    "/apps",
    status_code=201,
    response_model=AppOut,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        ErrorCode.FORBIDDEN, ErrorCode.ALREADY_EXISTS, ErrorCode.OWNER_NOT_ACTIVE
    )
    | POST_COMMON,
)
async def create_app(body: AppCreate, uow: UserUoW) -> Response:
    owner = _require_user(uow)
    app_id = new_id("app")
    row = (
        (
            await uow.conn.execute(
                _INSERT_APP, {"id": app_id, "org": uow.org_id, "slug": body.slug, "owner": owner}
            )
        )
        .mappings()
        .one()
    )
    for name in ("prod", "preview"):
        await uow.conn.execute(
            _INSERT_ENV, {"id": new_id("env"), "org": uow.org_id, "app": app_id, "name": name}
        )
    out = AppOut(**dict(row), environments=await _environments(uow, app_id))
    await uow.audit(
        AuditAction.APP_CREATED,
        target_kind="app",
        target_id=app_id,
        after={"slug": body.slug, "owner_user_id": owner},
    )
    return uow.reply(out, status=201)


@router.get("/apps", response_model=AppList, responses=AUTHENTICATED)
async def list_apps(uow: UserUoW) -> AppList:
    rows = (await uow.conn.execute(_SELECT_APPS, {"org": uow.org_id})).mappings()
    return AppList(apps=[AppSummary(**dict(r)) for r in rows])


@router.get(
    "/apps/{app_id}",
    response_model=AppOut,
    responses=AUTHENTICATED | problem_responses(ErrorCode.NOT_FOUND),
)
async def get_app(app_id: Id, uow: UserUoW) -> AppOut:
    row = (
        (await uow.conn.execute(_SELECT_APP, {"org": uow.org_id, "id": app_id})).mappings().first()
    )
    if row is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"app_id": app_id})
    return AppOut(**dict(row), environments=await _environments(uow, app_id))


# ── sharing rules (If-Match) ─────────────────────────────────────────────────

_LOCK_ENV = text(
    "select id, grants_version from ssc.environment "
    "where org_id = :org and app_id = :app and id = :env for update"
)
_SELECT_ENV = text(
    "select id, grants_version from ssc.environment "
    "where org_id = :org and app_id = :app and id = :env"
)
_SELECT_GRANTS = text(
    "select id, role, subject_kind, user_id, group_id from ssc.app_grant "
    "where org_id = :org and environment_id = :env order by role, subject_kind, user_id, group_id"
)
_DELETE_GRANT = text("delete from ssc.app_grant where org_id = :org and id = :id")
_INSERT_GRANT = text(
    "insert into ssc.app_grant (id, org_id, environment_id, role, subject_kind, user_id, group_id, "
    "granted_by_user_id) values (:id, :org, :env, :role, :kind, :user, :group, :by)"
)
_BUMP_GRANTS = text(
    "update ssc.environment set grants_version = grants_version + 1 "
    "where org_id = :org and id = :env returning grants_version"
)


def etag(version: int) -> str:
    return f'"{version}"'


def parse_if_match(value: str | None) -> int:
    if value is None:
        raise Refusal(ErrorCode.PRECONDITION_REQUIRED)
    m = _ETAG_RE.match(value.strip())
    if m is None:
        raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"if_match": value})
    return int(m.group(1))


def _grant_out(row: dict[str, Any]) -> GrantOut:
    return GrantOut(
        id=str(row["id"]),
        role=row["role"],
        subject_kind=row["subject_kind"],
        subject_id=row["user_id"] or row["group_id"],
    )


def _grant_key(g: GrantIn | GrantOut) -> tuple[str, str, str | None]:
    return (g.role, g.subject_kind, g.subject_id)


async def _grants_out(uow: UnitOfWork, env_id: str, version: int) -> GrantsOut:
    rows = (await uow.conn.execute(_SELECT_GRANTS, {"org": uow.org_id, "env": env_id})).mappings()
    return GrantsOut(
        environment_id=env_id, grants_version=version, grants=[_grant_out(dict(r)) for r in rows]
    )


@router.get(
    "/apps/{app_id}/environments/{environment_id}/grants",
    response_model=GrantsOut,
    responses=AUTHENTICATED | problem_responses(ErrorCode.NOT_FOUND),
)
async def get_grants(app_id: Id, environment_id: Id, uow: UserUoW, response: Response) -> GrantsOut:
    env = (
        await uow.conn.execute(
            _SELECT_ENV, {"org": uow.org_id, "app": app_id, "env": environment_id}
        )
    ).first()
    if env is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    version = int(env[1])
    response.headers[ETAG] = etag(version)
    return await _grants_out(uow, environment_id, version)


@router.put(
    "/apps/{app_id}/environments/{environment_id}/grants",
    response_model=GrantsOut,
    responses=AUTHENTICATED
    | problem_responses(
        ErrorCode.NOT_FOUND,
        ErrorCode.FORBIDDEN,
        ErrorCode.PRECONDITION_REQUIRED,
        ErrorCode.PRECONDITION_STALE,
        ErrorCode.REFERENCE_NOT_FOUND,
        ErrorCode.VALIDATION_FAILED,
    ),
)
async def put_grants(
    app_id: Id,
    environment_id: Id,
    body: GrantsIn,
    uow: UserUoW,
    if_match: Annotated[str | None, Header(alias=IF_MATCH)] = None,
) -> Response:
    """Replace the sharing rules of one environment. Requires ``If-Match`` with the current ETag."""
    expected = parse_if_match(if_match)
    by = _require_user(uow)
    for g in body.grants:
        if (g.subject_kind == "org") != (g.subject_id is None):
            raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"grant": g.model_dump()})
    env = (
        await uow.conn.execute(_LOCK_ENV, {"org": uow.org_id, "app": app_id, "env": environment_id})
    ).first()
    if env is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    current = int(env[1])
    if current != expected:
        raise Refusal(
            ErrorCode.PRECONDITION_STALE, evidence={"expected": expected, "current": current}
        )
    existing = {_grant_key(g): g for g in (await _grants_out(uow, environment_id, current)).grants}
    desired = {_grant_key(g): g for g in body.grants}
    for key, old in existing.items():
        if key not in desired:
            await uow.conn.execute(_DELETE_GRANT, {"org": uow.org_id, "id": old.id})
            await uow.audit(
                AuditAction.GRANT_REMOVED,
                target_kind="app_grant",
                target_id=old.id,
                before=old.model_dump(),
            )
    for key, new in desired.items():
        if key not in existing:
            gid = new_id("gnt")
            await uow.conn.execute(
                _INSERT_GRANT,
                {
                    "id": gid,
                    "org": uow.org_id,
                    "env": environment_id,
                    "role": new.role,
                    "kind": new.subject_kind,
                    "user": new.subject_id if new.subject_kind == "user" else None,
                    "group": new.subject_id if new.subject_kind == "group" else None,
                    "by": by,
                },
            )
            await uow.audit(
                AuditAction.GRANT_ADDED,
                target_kind="app_grant",
                target_id=gid,
                after={"environment_id": environment_id, **new.model_dump()},
            )
    bumped = (
        await uow.conn.execute(_BUMP_GRANTS, {"org": uow.org_id, "env": environment_id})
    ).scalar_one()
    version = int(bumped)
    return uow.reply(await _grants_out(uow, environment_id, version), headers={ETAG: etag(version)})


# ── deployments: long-running operations ─────────────────────────────────────

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
    responses=POST_COMMON
    | problem_responses(
        ErrorCode.NOT_FOUND, ErrorCode.DEPLOYMENT_IN_FLIGHT, ErrorCode.REFERENCE_NOT_FOUND
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
    responses=AUTHENTICATED | problem_responses(ErrorCode.NOT_FOUND),
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
