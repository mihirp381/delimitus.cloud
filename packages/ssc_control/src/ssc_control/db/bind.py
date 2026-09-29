"""Bind an org to a unit of work. Once per transaction, never per statement, never per session.

``set_config('ssc.org', <org id>, true)`` is transaction-scoped: the third argument is not
optional and there is no variant without it. A session-scoped bind would survive into the next
borrower of a pooled connection, which is a cross-customer leak that only shows up under load.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts.ids import prefix_of

BIND_ORG = text("select set_config('ssc.org', :org, true)")
BIND_ORG_PSYCOPG = "select set_config('ssc.org', %s, true)"


def check_org_id(org_id: str) -> str:
    if prefix_of(org_id) != "org":
        raise ValueError(f"not an org id: {org_id!r}")
    return org_id


async def bind_org(conn: AsyncConnection, org_id: str) -> None:
    """Bind inside a transaction that is already open. Prefer :func:`bound_org`."""
    await conn.execute(BIND_ORG, {"org": check_org_id(org_id)})


@asynccontextmanager
async def bound_org(engine: AsyncEngine, org_id: str) -> AsyncGenerator[AsyncConnection]:
    """One transaction, bound to one org. Commits on exit, rolls back on error."""
    async with engine.begin() as conn:
        await bind_org(conn, org_id)
        yield conn


def bind_org_sync(conn: psycopg.Connection[Any], org_id: str) -> None:
    """The same bind for plain psycopg connections (migration seeds, operator tools, tests)."""
    conn.execute(BIND_ORG_PSYCOPG, (check_org_id(org_id),))
