"""Whether an app has a named owner (decision 019 amendment 2026-10-29).

``active``: the owner is active. ``ownerless``: the owner is not, and an active admin can assign
one; the app keeps serving. ``blocked``: no active admin either; the snapshot marks the app
``owner_needed`` and the gateways refuse it.
"""

from typing import Final, Literal, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

OwnerState = Literal["active", "ownerless", "blocked"]


OWNER_STATE: Final = (
    "case when exists (select 1 from ssc.user_account ow where ow.org_id = a.org_id "
    "and ow.id = a.owner_user_id and ow.status = 'active') then 'active' "
    "when exists (select 1 from ssc.user_account ad where ad.org_id = a.org_id "
    "and ad.role = 'admin' and ad.status = 'active') then 'ownerless' else 'blocked' end"
)
"""The state as a SQL expression over the ``ssc.app`` row aliased ``a``."""

_STATE: Final = text(
    f"select {OWNER_STATE} from ssc.app a where a.org_id = :org and a.id = :app"  # noqa: S608  (a constant SQL fragment)
)


async def owner_state(conn: AsyncConnection, org_id: str, app_id: str) -> OwnerState:
    return cast(
        "OwnerState", (await conn.execute(_STATE, {"org": org_id, "app": app_id})).scalar_one()
    )
