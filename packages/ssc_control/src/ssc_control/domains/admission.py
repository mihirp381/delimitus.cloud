"""Whether an org may ask for a customer suffix (CD-4.2, CD-5.3).

Overlap with other orgs is checked under one advisory lock, class :data:`LOCK_CLASS` key 0,
held to the end of the caller's transaction, so two orgs cannot both pass and then insert. A
suffix equal to another org's ``org_domain`` row is left to that table's unique index."""

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.snapshot import check_suffix
from ssc_control.db.bind import bound_org
from ssc_control.db.orgs import all_org_ids, suffix_of
from ssc_control.domains.psl import public_suffix

LOCK_CLASS: Final = 31
PLATFORM_DOMAINS: Final = ("delimitusapps.com", "delimitus.com")

_LOCK = text("select pg_advisory_xact_lock(:cls, 0)")
_ORG = text("select cell_label, apps_suffix from ssc.org where id = :org")
_USED = text(
    "select 1 from ssc.org_suffix_retired where org_id = :org and suffix = :suffix union all "
    "select 1 from ssc.org_domain where org_id = :org and suffix = :suffix and state = 'retired'"
)
_HELD = text(
    "select apps_suffix, false from ssc.org where id = :org and apps_suffix is not null "
    "union all select suffix, false from ssc.org_suffix_retired where org_id = :org "
    "union all select suffix, true from ssc.org_domain where org_id = :org"
)


def under(name: str, parent: str) -> bool:
    return name == parent or name.endswith(f".{parent}")


def _shape(suffix: str, apps_domain: str) -> str | None:
    if len(suffix.split(".")) < 3:  # noqa: PLR2004  (a label under a registrable name)
        return "a suffix needs at least three labels, such as tools.example.com"
    try:
        check_suffix(suffix)
    except ValueError as exc:
        return str(exc)
    if any(under(suffix, d) for d in (*PLATFORM_DOMAINS, apps_domain)):
        return "the suffix is under a Delimitus domain"
    public = public_suffix(suffix)
    if suffix == public:
        return "the suffix is a public suffix"
    if suffix.count(".") == public.count(".") + 1:
        return "the suffix is a registered domain; ask for a name under it"
    return None


async def _overlap(conn: AsyncConnection, org_id: str, suffix: str) -> str | None:
    await conn.execute(_LOCK, {"cls": LOCK_CLASS})
    for other in await all_org_ids(conn.engine):
        if other == org_id:
            continue
        async with bound_org(conn.engine, other) as theirs:
            held = (await theirs.execute(_HELD, {"org": other})).all()
        for name, domain in held:
            if name == suffix and not domain:
                return "another organization uses or used the suffix"
            if under(name, suffix) and name != suffix:
                return "the suffix is above a suffix another organization uses or used"
            if under(suffix, name) and name != suffix:
                return "the suffix is under a suffix another organization uses or used"
    return None


async def admit(conn: AsyncConnection, org_id: str, suffix: str, *, apps_domain: str) -> str | None:
    """None when ``suffix`` may be requested, else why not."""
    row = (await conn.execute(_ORG, {"org": org_id})).one()
    if row.cell_label is not None and suffix == suffix_of(row.cell_label, None, apps_domain):
        return "the suffix is the organization's default suffix"
    if (refused := _shape(suffix, apps_domain)) is not None:
        return refused
    if suffix == row.apps_suffix:
        return "the organization already uses the suffix"
    if (await conn.execute(_USED, {"org": org_id, "suffix": suffix})).first() is not None:
        return "the organization used the suffix before"
    return await _overlap(conn, org_id, suffix)


__all__ = ["LOCK_CLASS", "PLATFORM_DOMAINS", "admit", "under"]
