"""``GET /v1/users``: the org's people for active org admins. With ``email=``, every exact match,
for sharing by address; without it, the roster, paged by name. Email is display data, never a key
(``db/PII.md``): callers act on the ``usr_`` id they pick."""

from datetime import datetime
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Query
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.errors import ErrorCode
from ssc_contracts.text import PRINTABLE
from ssc_control.api.authz import OrgRole, require_admin
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Strict
from ssc_control.api.uow import UserUoW

router = APIRouter()

MAX_PAGE: Final = 200
Status = Literal["active", "deactivated"]


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
        pattern=r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+$",
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
