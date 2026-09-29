"""``/internal/v1/directory``: the org's directory pushes users, groups and memberships
(SSC-021). Operator credentials only: an app's workload credential never manages people."""

from typing import Annotated, Literal

from fastapi import APIRouter, Response
from pydantic import Field

from ssc_contracts.errors import ErrorCode
from ssc_control.api.auth import PrincipalKind
from ssc_control.api.idempotency import InternalIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.uow import InternalUoW, UnitOfWork, actor_of
from ssc_control.audit.chain import Actor
from ssc_control.directory import (
    DirectoryError,
    DirectoryUser,
    set_group_members,
    upsert_group,
    upsert_user,
)

router = APIRouter(prefix="/directory")

UserId = Annotated[str, Field(pattern=r"^usr_[a-z0-9]{20}$")]


class DirectoryUserIn(Strict):
    issuer: str = Field(min_length=1, max_length=300)
    subject: str = Field(min_length=1, max_length=300, description="The provider's stable id.")
    display_name: str = Field(min_length=1, max_length=200)
    email: str = Field(max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: Literal["admin", "member"]
    status: Literal["active", "deactivated"]


class DirectoryUserOut(Strict):
    user_id: str
    created: bool


class DirectoryGroupIn(Strict):
    directory_ref: str = Field(
        min_length=1, max_length=300, description="The provider's group id: the key grants use."
    )
    display_name: str = Field(min_length=1, max_length=200)


class DirectoryGroupOut(Strict):
    group_id: str
    created: bool


class GroupMembersIn(Strict):
    user_ids: list[UserId] = Field(max_length=10000)


class GroupMembersOut(Strict):
    group_id: str
    added: list[str]
    removed: list[str]


def _operator(uow: UnitOfWork) -> Actor:
    if uow.principal.kind is not PrincipalKind.OPERATOR:
        raise Refusal(
            ErrorCode.FORBIDDEN,
            evidence={"reason": "directory_is_operator_only", "kind": uow.principal.kind.value},
        )
    return actor_of(uow.principal)


@router.post(
    "/users",
    response_model=DirectoryUserOut,
    dependencies=[InternalIdempotent],
    responses=problem_responses(
        *POST_COMMON, ErrorCode.FORBIDDEN, ErrorCode.ALREADY_EXISTS, ErrorCode.LAST_ORG_ADMIN
    ),
)
async def sync_user(body: DirectoryUserIn, uow: InternalUoW) -> Response:
    """Create or update the person with this ``(issuer, subject)``. ``LAST_ORG_ADMIN`` when the
    change would leave the org without an active admin."""
    actor = _operator(uow)
    result = await upsert_user(
        uow.conn, uow.org_id, DirectoryUser(**body.model_dump()), actor=actor
    )
    return uow.reply(DirectoryUserOut(user_id=result.user_id, created=result.created))


@router.post(
    "/groups",
    response_model=DirectoryGroupOut,
    dependencies=[InternalIdempotent],
    responses=problem_responses(*POST_COMMON, ErrorCode.FORBIDDEN),
)
async def sync_group(body: DirectoryGroupIn, uow: InternalUoW) -> Response:
    """Create the group with this ``directory_ref``, or refresh its cached name."""
    actor = _operator(uow)
    result = await upsert_group(
        uow.conn,
        uow.org_id,
        directory_ref=body.directory_ref,
        display_name=body.display_name,
        actor=actor,
    )
    return uow.reply(DirectoryGroupOut(group_id=result.group_id, created=result.created))


@router.put(
    "/groups/{group_id}/members",
    response_model=GroupMembersOut,
    responses=problem_responses(
        *AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND, ErrorCode.REFERENCE_NOT_FOUND
    ),
)
async def sync_members(group_id: Id, body: GroupMembersIn, uow: InternalUoW) -> Response:
    """Replace the group's members. ``NOT_FOUND`` for an unknown group; ``REFERENCE_NOT_FOUND``
    when a user id is not a user of this org."""
    actor = _operator(uow)
    try:
        result = await set_group_members(uow.conn, uow.org_id, group_id, body.user_ids, actor=actor)
    except DirectoryError as e:
        code = (
            ErrorCode.NOT_FOUND if e.problem == "group_not_found" else ErrorCode.REFERENCE_NOT_FOUND
        )
        raise Refusal(code, evidence={"reason": e.problem, **e.evidence}) from e
    return uow.reply(
        GroupMembersOut(
            group_id=result.group_id, added=list(result.added), removed=list(result.removed)
        )
    )
