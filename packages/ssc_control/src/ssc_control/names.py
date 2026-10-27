"""Display names for the ids a response carries, read in one query per kind."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncConnection

FORMER_MEMBER: Final = "Former member"
REMOVED_GROUP: Final = "Removed group"
OPERATOR: Final = "Delimitus operator"
SYSTEM: Final = "System"

_PEOPLE: Final = text(
    "select id, display_name, email, status from ssc.user_account "
    "where org_id = :org and id = any(cast(:ids as text[]))"
)
_GROUPS: Final = text(
    "select id, display_name from ssc.user_group "
    "where org_id = :org and id = any(cast(:ids as text[]))"
)
_APPS: Final = text(
    "select id, slug from ssc.app where org_id = :org and id = any(cast(:ids as text[]))"
)
_ENVIRONMENTS: Final = text(
    "select e.id, a.slug || ' ' || e.name from ssc.environment e "
    "join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "where e.org_id = :org and e.id = any(cast(:ids as text[]))"
)
_ORGS: Final = text(
    "select id, name from ssc.org where id = :org and id = any(cast(:ids as text[]))"
)
_CONNECTIONS: Final = text(
    "select id, name from ssc.connection where org_id = :org and id = any(cast(:ids as text[]))"
)


@dataclass(frozen=True, slots=True)
class Person:
    name: str
    email: str | None


FORMER: Final = Person(FORMER_MEMBER, None)


def person(display_name: str | None, email: str | None, status: str | None) -> Person:
    if status not in {"active", "invited"} or email is None:
        return FORMER
    return Person((display_name or "").strip() or email, email)


@dataclass(frozen=True, slots=True)
class People:
    found: Mapping[str, Person]

    def of(self, user_id: str) -> Person:
        return self.found.get(user_id, FORMER)

    def name(self, user_id: str | None) -> str | None:
        return None if user_id is None else self.of(user_id).name

    def email(self, user_id: str | None) -> str | None:
        return None if user_id is None else self.of(user_id).email


def _ids(ids: Iterable[str | None]) -> list[str]:
    return sorted({i for i in ids if i})


async def _pairs(
    conn: AsyncConnection, query: TextClause, org_id: str, ids: list[str]
) -> dict[str, str]:
    if not ids:
        return {}
    rows = await conn.execute(query, {"org": org_id, "ids": ids})
    return {str(i): str(n) for i, n in rows}


async def people(conn: AsyncConnection, org_id: str, ids: Iterable[str | None]) -> People:
    wanted = _ids(ids)
    if not wanted:
        return People({})
    rows = (await conn.execute(_PEOPLE, {"org": org_id, "ids": wanted})).mappings()
    return People({str(r["id"]): person(r["display_name"], r["email"], r["status"]) for r in rows})


async def groups(conn: AsyncConnection, org_id: str, ids: Iterable[str | None]) -> dict[str, str]:
    return await _pairs(conn, _GROUPS, org_id, _ids(ids))


async def apps(conn: AsyncConnection, org_id: str, ids: Iterable[str | None]) -> dict[str, str]:
    return await _pairs(conn, _APPS, org_id, _ids(ids))


async def connections(
    conn: AsyncConnection, org_id: str, ids: Iterable[str | None]
) -> dict[str, str]:
    return await _pairs(conn, _CONNECTIONS, org_id, _ids(ids))


async def environments(
    conn: AsyncConnection, org_id: str, ids: Iterable[str | None]
) -> dict[str, str]:
    return await _pairs(conn, _ENVIRONMENTS, org_id, _ids(ids))


async def orgs(conn: AsyncConnection, org_id: str, ids: Iterable[str | None]) -> dict[str, str]:
    return await _pairs(conn, _ORGS, org_id, _ids(ids))
