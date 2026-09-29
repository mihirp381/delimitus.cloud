"""Apps: create, list, read."""

from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Query, Request, Response
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.authz import buildable_app_ids
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id, Slug, Strict, require_user
from ssc_control.api.runtime import runtime_of
from ssc_control.api.uow import UnitOfWork, UserUoW
from ssc_control.snapshot.service import mark_dirty
from ssc_shared.hosts import Environment, app_origin, slug_problem

router = APIRouter()


class AppCreate(Strict):
    slug: Slug


class EnvironmentOut(Strict):
    id: str
    name: Literal["prod", "preview"]
    config_version: int
    grants_version: int
    current_deployment_id: str | None
    url: str | None = Field(
        description="Where the environment is served: `https://<slug>.<cell label>.<apps domain>`"
        ", with `--preview` after the slug for preview (decision 004). Null while the org has no "
        "cell label."
    )


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
    "select e.id, e.name, e.config_version, e.grants_version, e.current_deployment_id, a.slug, "
    "o.cell_label from ssc.environment e "
    "join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "join ssc.org o on o.id = e.org_id "
    "where e.org_id = :org and e.app_id = :app order by e.name"
)


def _url(slug: str, environment: str, cell_label: str | None, apps_domain: str) -> str | None:
    """No address without a cell label, nor for a slug stored before the host rule refused it."""
    if cell_label is None or slug_problem(slug) is not None:
        return None
    return app_origin(slug, cast("Environment", environment), cell_label, apps_domain)


async def _environments(uow: UnitOfWork, app_id: str, apps_domain: str) -> list[EnvironmentOut]:
    rows = (await uow.conn.execute(_SELECT_ENVS, {"org": uow.org_id, "app": app_id})).mappings()
    return [
        EnvironmentOut(
            id=r["id"],
            name=r["name"],
            config_version=r["config_version"],
            grants_version=r["grants_version"],
            current_deployment_id=r["current_deployment_id"],
            url=_url(r["slug"], r["name"], r["cell_label"], apps_domain),
        )
        for r in rows
    ]


@router.post(
    "/apps",
    status_code=201,
    response_model=AppOut,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON, ErrorCode.FORBIDDEN, ErrorCode.ALREADY_EXISTS, ErrorCode.OWNER_NOT_ACTIVE
    ),
)
async def create_app(body: AppCreate, uow: UserUoW, request: Request) -> Response:
    owner = require_user(uow)
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
    apps_domain = runtime_of(request).settings.apps_domain
    out = AppOut(**dict(row), environments=await _environments(uow, app_id, apps_domain))
    await uow.audit(
        AuditAction.APP_CREATED,
        target_kind="app",
        target_id=app_id,
        after={"slug": body.slug, "owner_user_id": owner},
    )
    await mark_dirty(uow.conn, uow.org_id)
    return uow.reply(out, status=201)


@router.get(
    "/apps",
    response_model=AppList,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def list_apps(
    uow: UserUoW,
    builder: Annotated[
        Literal["me"] | None,
        Query(
            description="`me`: only the apps the caller may ship source to, as an active org "
            "admin (every app), the owner, or a builder on any environment."
        ),
    ] = None,
) -> AppList:
    """Every app of the org, by slug. ``builder=me`` needs a user credential (``FORBIDDEN``)."""
    rows = (await uow.conn.execute(_SELECT_APPS, {"org": uow.org_id})).mappings()
    apps = [AppSummary(**dict(r)) for r in rows]
    if builder == "me":
        mine = await buildable_app_ids(uow)
        apps = [a for a in apps if a.id in mine]
    return AppList(apps=apps)


@router.get(
    "/apps/{app_id}",
    response_model=AppOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.NOT_FOUND),
)
async def get_app(app_id: Id, uow: UserUoW, request: Request) -> AppOut:
    row = (
        (await uow.conn.execute(_SELECT_APP, {"org": uow.org_id, "id": app_id})).mappings().first()
    )
    if row is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"app_id": app_id})
    apps_domain = runtime_of(request).settings.apps_domain
    return AppOut(**dict(row), environments=await _environments(uow, app_id, apps_domain))
