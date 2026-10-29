"""What an org's customer domain looks like to its admins (CD-4.2): the suffixes, the
delegation, the apps a flip moves and whether every one has rolled for the coming suffix."""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_control.db.domains import OrgDomain, domain_of, suffix_list
from ssc_control.db.orgs import RetiredSuffix, retired_suffixes, suffix_of
from ssc_control.domains.flip import FlipTo
from ssc_control.runtime import reconciler
from ssc_control.runtime.cells import OrgCell
from ssc_control.runtime.driver import identity_suffixes
from ssc_control.runtime.specs import ReleaseSpecs
from ssc_shared.hosts import app_host, slug_problem

_ORG: Final = text("select cell_label, apps_suffix from ssc.org where id = :org")
_ACTIVE_APPS: Final = text(
    "select slug from ssc.app where org_id = :org and status = 'active' order by slug"
)


class NoCellLabelError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class DomainView:
    default_suffix: str
    current_suffix: str
    apps_suffix: str | None
    previous: tuple[RetiredSuffix, ...]
    delegation: OrgDomain | None
    next_flip: FlipTo | None
    coming_suffix: str | None
    apps_that_move: tuple[tuple[str, tuple[str, ...]], ...]


async def view_of(
    conn: AsyncConnection, org_id: str, *, apps_domain: str, now: datetime
) -> DomainView:
    org = (await conn.execute(_ORG, {"org": org_id})).one()
    if org.cell_label is None:
        raise NoCellLabelError(org_id)
    default = suffix_of(org.cell_label, None, apps_domain)
    current = suffix_of(org.cell_label, org.apps_suffix, apps_domain)
    row = await domain_of(conn, org_id)
    next_flip: FlipTo | None = None
    coming: str | None = None
    if row is not None and row.suffix != org.apps_suffix:
        coming = row.suffix
        next_flip = "customer" if row.state == "active" else None
    elif org.apps_suffix is not None:
        coming, next_flip = default, "default"
    moves: tuple[tuple[str, tuple[str, ...]], ...] = ()
    if coming is not None:
        slugs = (await conn.execute(_ACTIVE_APPS, {"org": org_id})).scalars().all()
        moves = tuple(
            (slug, (app_host(slug, "prod", coming), app_host(slug, "preview", coming)))
            for slug in slugs
            if slug_problem(slug) is None
        )
    previous = tuple(
        r for r in await retired_suffixes(conn, org_id) if r.until > now and r.suffix != current
    )
    return DomainView(
        default_suffix=default,
        current_suffix=current,
        apps_suffix=org.apps_suffix,
        previous=previous,
        delegation=row,
        next_flip=next_flip,
        coming_suffix=coming,
        apps_that_move=moves,
    )


async def rolling_for(  # noqa: PLR0913
    conn: AsyncConnection,
    engine: AsyncEngine,
    cell: OrgCell | None,
    specs: ReleaseSpecs,
    view: DomainView,
    *,
    org_id: str,
) -> int:
    """How many live environments have not yet rolled with the coming suffix among their
    origins; every live one when the coming suffix is not in the org's suffix list."""
    if view.coming_suffix is None or cell is None or cell.runtime is None or cell.identity is None:
        return 0
    identity = replace(
        cell.identity,
        suffix=view.apps_suffix,
        previous_suffixes=await suffix_list(conn, org_id, view.apps_suffix),
    )
    if view.coming_suffix not in identity_suffixes(identity):
        return await live_count(conn, org_id)
    return await reconciler.rolling(engine, cell.runtime, specs, org_id=org_id, identity=identity)


async def live_count(conn: AsyncConnection, org_id: str) -> int:
    """Right after a flip every live environment rolls: its suffixes changed."""
    return len((await conn.execute(reconciler.LIVE_ENVS, {"org": org_id})).scalars().all())


__all__ = ["DomainView", "NoCellLabelError", "live_count", "rolling_for", "view_of"]
