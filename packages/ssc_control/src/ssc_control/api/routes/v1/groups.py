"""``GET /v1/groups?name=``: find the org's groups by name, for sharing with a group. A group's
name is cached display data, never a key (``db/PII.md``): two groups can share one, so the answer
is a list, and callers share with the ``grp_`` id they pick."""

from typing import Annotated, Final, Literal

from fastapi import APIRouter, Query
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_sharer
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Strict
from ssc_control.api.uow import UserUoW

router = APIRouter()


class GroupMatch(Strict):
    id: str
    name: str
    member_count: int = Field(
        description="Active members only: a grant gives a deactivated member nothing."
    )


class GroupMatches(Strict):
    groups: list[GroupMatch]


PREFIX_LIMIT: Final = 20
_SELECT = (
    "select g.id, g.display_name as name, count(u.id) as member_count "
    "from ssc.user_group g "
    "left join ssc.group_member m on m.org_id = g.org_id and m.group_id = g.id "
    "left join ssc.user_account u "
    "on u.org_id = m.org_id and u.id = m.user_id and u.status = 'active' "
)
_BY_NAME = text(
    f"{_SELECT}where g.org_id = :org and lower(g.display_name) = lower(:name) "
    "group by g.id, g.display_name order by g.display_name, g.id"
)
_BY_PREFIX = text(
    f"{_SELECT}where g.org_id = :org and g.display_name ilike :name || '%' escape '\\' "
    f"group by g.id, g.display_name order by g.display_name, g.id limit {PREFIX_LIMIT}"
)


def _escaped(name: str) -> str:
    return name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get(
    "/groups",
    response_model=GroupMatches,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def find_groups(
    uow: UserUoW,
    name: Annotated[str, Query(min_length=1, max_length=200, description="Ignoring case.")],
    match: Annotated[
        Literal["whole", "prefix"],
        Query(description=f"`prefix`: names starting with `name`, at most {PREFIX_LIMIT}."),
    ] = "whole",
) -> GroupMatches:
    """Those who may change some app's sharing: an active org admin, an app's owner, or a
    builder on any environment, with a user credential (``FORBIDDEN`` otherwise)."""
    await require_sharer(uow)
    if match == "prefix":
        query, bound = _BY_PREFIX, _escaped(name)
    else:
        query, bound = _BY_NAME, name
    rows = (await uow.conn.execute(query, {"org": uow.org_id, "name": bound})).mappings()
    return GroupMatches(groups=[GroupMatch(**dict(r)) for r in rows])
