"""Whether an org may ask for a customer suffix (CD-4.2; CD-5.3 adds checks)."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.snapshot import check_suffix
from ssc_control.db.orgs import suffix_of

_ORG = text("select cell_label, apps_suffix from ssc.org where id = :org")
_USED = text(
    "select 1 from ssc.org_suffix_retired where org_id = :org and suffix = :suffix union all "
    "select 1 from ssc.org_domain where org_id = :org and suffix = :suffix and state = 'retired'"
)


async def admit(conn: AsyncConnection, org_id: str, suffix: str, *, apps_domain: str) -> str | None:
    """None when ``suffix`` may be requested, else why not."""
    try:
        check_suffix(suffix)
    except ValueError as exc:
        return str(exc)
    row = (await conn.execute(_ORG, {"org": org_id})).one()
    if row.cell_label is not None and suffix == suffix_of(row.cell_label, None, apps_domain):
        return "the suffix is the organization's default suffix"
    if suffix == row.apps_suffix:
        return "the organization already uses the suffix"
    if (await conn.execute(_USED, {"org": org_id, "suffix": suffix})).first() is not None:
        return "the organization used the suffix before"
    return None


__all__ = ["admit"]
