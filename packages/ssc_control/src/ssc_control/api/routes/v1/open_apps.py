"""``GET /v1/me/apps`` and ``GET /v1/users/{user_id}/apps``: every environment a person may open.

The second consumer of the one evaluator (decision 019): one live compile of the org, then
``ssc_shared.access.decide`` for every environment; only the allowed pairs are listed, and the
pairs refused only because the app needs an owner (``owner_state`` ``blocked``).
"""

from datetime import UTC, datetime
from typing import Final, Literal

from fastapi import APIRouter, Request
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.errors import ErrorCode
from ssc_contracts.identity import EnvironmentName
from ssc_contracts.snapshot import GrantRole
from ssc_control.api.authz import require_person
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.apps import app_url
from ssc_control.api.routes.v1.common import Strict
from ssc_control.api.runtime import runtime_of
from ssc_control.api.uow import UnitOfWork, UserUoW
from ssc_control.db.orgs import suffix_of
from ssc_control.names import REMOVED_GROUP, groups, people
from ssc_control.owners import OWNER_STATE, OwnerState
from ssc_control.snapshot.compiler import compile_document
from ssc_shared.access import RANK, AccessView, GrantRef, decide

router = APIRouter()

_ENV_ORDER: Final = {"prod": 0, "preview": 1}
_KIND_ORDER: Final = {"user": 0, "group": 1, "org": 2}


class AccessVia(Strict):
    kind: Literal["user", "group", "org"]
    name: str | None = Field(
        description="The person's or group's name: `Former member` for a person no longer "
        "active, `Removed group` for a group gone from the directory; null for the org."
    )


class UserAppEnvironment(Strict):
    environment_id: str
    name: Literal["prod", "preview"]
    url: str | None = Field(description="As `EnvironmentOut.url`.")
    role: Literal["builder", "user"] = Field(description="The best role granted.")
    via: list[AccessVia] = Field(description="The grants that count, one per subject.")


class UserApp(Strict):
    app_id: str
    slug: str
    status: Literal["active", "disabled", "quarantined", "deleting"]
    owner_name: str
    owner_email: str | None
    owner_state: OwnerState = Field(
        description="As `AppOut.owner_state`. A `blocked` app is listed with the environments "
        "the person would open, but the gateway refuses them until an owner is assigned."
    )
    environments: list[UserAppEnvironment] = Field(description="Production before preview.")


class UserApps(Strict):
    user_id: str
    user_name: str
    user_status: Literal["active", "invited", "deactivated"]
    published_version: int | None = Field(
        description="The org's newest published snapshot; null before the first."
    )
    apps: list[UserApp] = Field(description="By slug; only apps with an environment to open.")


RESPONSES: Final = problem_responses(
    *AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.REFERENCE_NOT_FOUND
)

_STATUS = text("select status from ssc.user_account where org_id = :org and id = :id")
_APPS = text(
    f"select a.id, a.slug, a.status, a.owner_user_id, {OWNER_STATE} as owner_state, "  # noqa: S608  (a constant SQL fragment)
    "o.cell_label, o.apps_suffix from ssc.app a "
    "join ssc.org o on o.id = a.org_id "
    "where a.org_id = :org and a.status <> 'deleted' order by a.slug collate \"C\""
)
_PUBLISHED = text("select max(version) from ssc.access_snapshot where org_id = :org")


def _subjects(via: tuple[GrantRef, ...]) -> list[GrantRef]:
    seen: dict[tuple[str, str | None], GrantRef] = {}
    for g in via:
        seen.setdefault((g.subject_kind, g.subject_id), g)
    return sorted(seen.values(), key=lambda g: (_KIND_ORDER[g.subject_kind], g.subject_id or ""))


async def user_apps(uow: UnitOfWork, user_id: str, apps_domain: str) -> UserApps:
    """``REFERENCE_NOT_FOUND`` for a person who is not in the org."""
    params = {"org": uow.org_id, "id": user_id}
    status = (await uow.conn.execute(_STATUS, params)).scalar_one_or_none()
    if status is None:
        raise Refusal(ErrorCode.REFERENCE_NOT_FOUND, evidence={"user_id": user_id})
    doc = await compile_document(uow.conn, uow.org_id, version=0, compiled_at=datetime.now(UTC))
    view = AccessView(doc)
    by_app: dict[str, list[tuple[str, EnvironmentName, GrantRole, list[GrantRef]]]] = {}
    for env_id, env in doc.environments.items():
        decision = decide(view, env_id, user_id)
        role = decision.role
        if decision.reason == "owner_needed":
            role = max(decision.via, key=lambda g: RANK[g.role]).role
        if (decision.allowed or decision.reason == "owner_needed") and role is not None:
            found = (env_id, env.name, role, _subjects(decision.via))
            by_app.setdefault(env.app_id, []).append(found)
    rows = (await uow.conn.execute(_APPS, {"org": uow.org_id})).mappings().all()
    rows = [r for r in rows if r["id"] in by_app]
    refs = [g for envs in by_app.values() for *_, via in envs for g in via]
    group_names = await groups(uow.conn, uow.org_id, [g.subject_id for g in refs])
    users = await people(
        uow.conn,
        uow.org_id,
        [user_id, *(r["owner_user_id"] for r in rows)]
        + [g.subject_id for g in refs if g.subject_kind == "user"],
    )

    def via_name(g: GrantRef) -> str | None:
        if g.subject_id is None:
            return None
        if g.subject_kind == "user":
            return users.of(g.subject_id).name
        return group_names.get(g.subject_id, REMOVED_GROUP)

    published = (await uow.conn.execute(_PUBLISHED, {"org": uow.org_id})).scalar_one()
    return UserApps(
        user_id=user_id,
        user_name=users.of(user_id).name,
        user_status=status,
        published_version=None if published is None else int(published),
        apps=[
            UserApp(
                app_id=r["id"],
                slug=r["slug"],
                status=r["status"],
                owner_name=users.of(r["owner_user_id"]).name,
                owner_email=users.of(r["owner_user_id"]).email,
                owner_state=r["owner_state"],
                environments=[
                    UserAppEnvironment(
                        environment_id=env_id,
                        name=name,
                        url=app_url(
                            r["slug"],
                            name,
                            suffix_of(r["cell_label"], r["apps_suffix"], apps_domain),
                        ),
                        role=role,
                        via=[AccessVia(kind=g.subject_kind, name=via_name(g)) for g in via],
                    )
                    for env_id, name, role, via in sorted(
                        by_app[r["id"]], key=lambda e: _ENV_ORDER[e[1]]
                    )
                ],
            )
            for r in rows
        ],
    )


@router.get("/me/apps", response_model=UserApps, responses=RESPONSES)
async def list_my_apps(uow: UserUoW, request: Request) -> UserApps:
    """The environments the caller may open, from the one access evaluator on a live compile.
    A person's own credential only: an agent's, a CI token and any other credential are
    ``FORBIDDEN``. Org admins see only what is shared with them, like everyone else."""
    caller = require_person(uow)
    return await user_apps(uow, caller, runtime_of(request).settings.apps_domain)
