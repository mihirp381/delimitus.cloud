"""``GET /v1/users``: the org's people for active org admins. With ``email=``, every exact match,
for sharing by address; without it, the roster, paged by name. Email is display data, never a key
(``db/PII.md``): callers act on the ``usr_`` id they pick.

In a pilot org that signs in with social providers (decision 002 amendment 2026-10-27), admins
invite people by email and deactivate or reactivate them; a directory org refuses all three."""

from datetime import datetime
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Query, Response
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_contracts.text import PRINTABLE
from ssc_control import directory
from ssc_control.api.authz import OrgRole, require_admin
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.identity import social
from ssc_control.snapshot.service import mark_dirty

router = APIRouter()

MAX_PAGE: Final = 200
Status = Literal["active", "invited", "deactivated"]
EMAIL: Final = r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+$"


class UserMatch(Strict):
    id: str
    display_name: str
    email: str
    role: OrgRole
    status: Status
    created_at: datetime
    last_sign_in_at: datetime | None = Field(
        description="The start of the person's latest browser, console or command-line session."
    )


class UserMatches(Strict):
    users: list[UserMatch]
    next_cursor: str | None = Field(
        description="Pass as `cursor` for the next page; null on the last page and with `email`."
    )


class UsersQuery(Strict):
    email: str | None = Field(
        default=None,
        min_length=3,
        max_length=320,
        pattern=EMAIL,
        description="Matched whole, ignoring case; deactivated people are included. Not with "
        "`q`, `status` or `cursor`.",
    )
    q: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=PRINTABLE,
        description="Part of the name or email, ignoring case.",
    )
    status: Status | None = None
    limit: int = Field(default=50, ge=1, le=MAX_PAGE)
    cursor: str | None = Field(
        default=None,
        pattern=r"^usr_[a-z0-9]{20}$",
        description="The previous page's `next_cursor`.",
    )


_BY_EMAIL = text(
    "select u.id, u.display_name, u.email, u.role, u.status, u.created_at, s.last_sign_in_at "
    "from ssc.user_account u left join lateral (select max(created_at) as last_sign_in_at "
    "from ssc.auth_session where org_id = u.org_id and user_id = u.id and kind <> 'ci') s on true "
    "where u.org_id = :org and lower(u.email) = lower(:email) order by u.display_name, u.id"
)
_ROSTER = text(
    "select u.id, u.display_name, u.email, u.role, u.status, u.created_at, s.last_sign_in_at "
    "from ssc.user_account u left join lateral (select max(created_at) as last_sign_in_at "
    "from ssc.auth_session where org_id = u.org_id and user_id = u.id and kind <> 'ci') s on true "
    "where u.org_id = :org and (cast(:status as text) is null or u.status = :status) "
    "and (cast(:q as text) is null or strpos(lower(u.display_name), lower(:q)) > 0 "
    "or strpos(lower(u.email), lower(:q)) > 0) "
    "and (cast(:cursor as text) is null or (lower(u.display_name), u.id) > ("
    "select lower(c.display_name), c.id from ssc.user_account c "
    "where c.org_id = :org and c.id = :cursor)) "
    "order by lower(u.display_name), u.id limit :limit"
)


@router.get(
    "/users",
    response_model=UserMatches,
    responses=problem_responses(
        *AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.AGENT_SESSION_REFUSED
    ),
)
async def list_users(params: Annotated[UsersQuery, Query()], uow: UserUoW) -> UserMatches:
    """Active org admins with a user credential only (``FORBIDDEN``); the roster, without
    ``email``, never in an agent session (``AGENT_SESSION_REFUSED``). By name, then id."""
    await require_admin(uow)
    if params.email is not None:
        if params.q is not None or params.status is not None or params.cursor is not None:
            raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"email": "alone"})
        found = await uow.conn.execute(_BY_EMAIL, {"org": uow.org_id, "email": params.email})
        return UserMatches(users=[UserMatch(**dict(r)) for r in found.mappings()], next_cursor=None)
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    bind = params.model_dump(include={"q", "status", "cursor"})
    rows = await uow.conn.execute(_ROSTER, {**bind, "org": uow.org_id, "limit": params.limit + 1})
    users = [UserMatch(**dict(r)) for r in rows.mappings()]
    more = len(users) > params.limit
    users = users[: params.limit]
    return UserMatches(users=users, next_cursor=users[-1].id if more else None)


class InviteIn(Strict):
    email: str = Field(min_length=3, max_length=320, pattern=EMAIL)


_PERSON = text(
    "select u.id, u.display_name, u.email, u.role, u.status, u.created_at, s.last_sign_in_at "
    "from ssc.user_account u left join lateral (select max(created_at) as last_sign_in_at "
    "from ssc.auth_session where org_id = u.org_id and user_id = u.id and kind <> 'ci') s on true "
    "where u.org_id = :org and u.id = :id"
)
_LOCK_ORG = text("select 1 from ssc.org where id = :org for update")
_INVITED = text(
    "select id from ssc.user_account where org_id = :org and lower(email) = :email "
    "order by created_at, id limit 1"
)
_INVITE = text(
    "insert into ssc.user_account (id, org_id, display_name, email, role, status) "
    "values (:id, :org, :email, :email, 'member', 'invited')"
)
_PEOPLE_CHANGES: Final = (
    *POST_COMMON,
    ErrorCode.FORBIDDEN,
    ErrorCode.AGENT_SESSION_REFUSED,
    ErrorCode.DIRECTORY_MANAGED,
)


async def _social_admin(uow: UnitOfWork) -> None:
    await require_admin(uow)
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    if await social.mode(uow.conn, uow.org_id) != "social":
        raise Refusal(ErrorCode.DIRECTORY_MANAGED)


async def _person(uow: UnitOfWork, user_id: str) -> UserMatch:
    row = (await uow.conn.execute(_PERSON, {"org": uow.org_id, "id": user_id})).mappings().one()
    return UserMatch(**dict(row))


@router.post(
    "/users/invite",
    response_model=UserMatch,
    status_code=201,
    dependencies=[UserIdempotent],
    responses=problem_responses(*_PEOPLE_CHANGES),
)
async def invite_user(body: InviteIn, uow: UserUoW) -> Response:
    """An invited member who signs in with one of the org's providers. ``201`` for a new person;
    ``200`` with the person who already has the address, ignoring case. Active admins only,
    never in an agent session; ``DIRECTORY_MANAGED`` in a directory org."""
    await _social_admin(uow)
    email = body.email.lower()
    await uow.conn.execute(_LOCK_ORG, {"org": uow.org_id})
    found = (
        await uow.conn.execute(_INVITED, {"org": uow.org_id, "email": email})
    ).scalar_one_or_none()
    if found is not None:
        return uow.reply(await _person(uow, str(found)))
    user_id = new_id("usr")
    await uow.conn.execute(_INVITE, {"id": user_id, "org": uow.org_id, "email": email})
    await uow.audit(
        AuditAction.USER_INVITED,
        target_kind="user_account",
        target_id=user_id,
        after={"role": "member", "status": "invited"},
    )
    await mark_dirty(uow.conn, uow.org_id)
    return uow.reply(await _person(uow, user_id), status=201)


async def _set_status(uow: UnitOfWork, user_id: str, status: directory.Status) -> Response:
    await _social_admin(uow)
    if status == "deactivated" and await directory.is_last_active_admin(
        uow.conn, uow.org_id, user_id
    ):
        raise Refusal(ErrorCode.LAST_ORG_ADMIN)
    before = await directory.set_status(
        uow.conn, uow.org_id, user_id, status, actor=actor_of(uow.principal)
    )
    if before is None:
        raise Refusal(ErrorCode.NOT_FOUND)
    return uow.reply(await _person(uow, user_id))


@router.post(
    "/users/{user_id}/deactivate",
    response_model=UserMatch,
    dependencies=[UserIdempotent],
    responses=problem_responses(*_PEOPLE_CHANGES, ErrorCode.NOT_FOUND, ErrorCode.LAST_ORG_ADMIN),
)
async def deactivate_user(user_id: Id, uow: UserUoW) -> Response:
    """End every session and token the person holds, in this transaction, and mark the access
    snapshot dirty. ``LAST_ORG_ADMIN`` for the org's last active admin."""
    return await _set_status(uow, user_id, "deactivated")


@router.post(
    "/users/{user_id}/reactivate",
    response_model=UserMatch,
    dependencies=[UserIdempotent],
    responses=problem_responses(*_PEOPLE_CHANGES, ErrorCode.NOT_FOUND),
)
async def reactivate_user(user_id: Id, uow: UserUoW) -> Response:
    """A deactivated person may sign in again; their old sessions stay ended."""
    return await _set_status(uow, user_id, "active")
