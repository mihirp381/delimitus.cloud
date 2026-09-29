"""``GET /v1/users?email=``: find the org's people by email, for sharing by address. Org admins
only. Email is display data, never a key (``db/PII.md``): several people can share one address,
so the answer is a list, and callers act on the ``usr_`` id they pick."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from sqlalchemy import text

from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import OrgRole, require_admin
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Strict
from ssc_control.api.uow import UserUoW

router = APIRouter()


class UserMatch(Strict):
    id: str
    display_name: str
    email: str
    role: OrgRole
    status: Literal["active", "deactivated"]


class UserMatches(Strict):
    users: list[UserMatch]


_BY_EMAIL = text(
    "select id, display_name, email, role, status from ssc.user_account "
    "where org_id = :org and lower(email) = lower(:email) order by display_name, id"
)


@router.get(
    "/users",
    response_model=UserMatches,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def find_users(
    uow: UserUoW,
    email: Annotated[
        str,
        Query(
            min_length=3,
            max_length=320,
            pattern=r"^[^@\s]+@[^@\s]+$",
            description="Matched whole, ignoring case. Deactivated people are included.",
        ),
    ],
) -> UserMatches:
    """Active org admins with a user credential only (``FORBIDDEN``)."""
    await require_admin(uow)
    rows = (await uow.conn.execute(_BY_EMAIL, {"org": uow.org_id, "email": email})).mappings()
    return UserMatches(users=[UserMatch(**dict(r)) for r in rows])
