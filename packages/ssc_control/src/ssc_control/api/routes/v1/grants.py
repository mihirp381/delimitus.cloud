"""Sharing rules of one environment, behind ``If-Match``."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Response
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import (
    ETAG,
    IF_MATCH,
    Id,
    Strict,
    etag,
    parse_if_match,
    require_user,
)
from ssc_control.api.uow import UnitOfWork, UserUoW

router = APIRouter()


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
    responses=problem_responses(*AUTHENTICATED, ErrorCode.NOT_FOUND),
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
    responses=problem_responses(
        *AUTHENTICATED,
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
    by = require_user(uow)
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
