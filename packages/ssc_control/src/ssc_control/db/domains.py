"""``ssc.org_domain`` (CD-3), read and written in the caller's org-bound transaction."""

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts.domains import DomainState
from ssc_contracts.ids import new_id
from ssc_control.db.bind import bound_org
from ssc_control.db.orgs import all_org_ids, retired_suffixes

__all__ = [
    "OrgDomain",
    "domain_of",
    "domains_in_state",
    "insert_domain",
    "lock_domain",
    "mark_stalled",
    "open_domains",
    "restart_domain",
    "suffix_list",
    "update_state",
]

_INSERT = text(
    "insert into ssc.org_domain "
    "(id, org_id, suffix, zone_name, name_servers, state, certificate, provider) "
    "values (:id, :org, :suffix, :zone, :ns, :state, :certificate, :provider) returning "
    "id, org_id, suffix, zone_name, name_servers, state, observed_ns, failure, certificate, "
    "last_delegated_at, retire_after, created_at, updated_at, provider, stalled_at"
)
_LIVE = text(
    "select "
    "id, org_id, suffix, zone_name, name_servers, state, observed_ns, failure, certificate, "
    "last_delegated_at, retire_after, created_at, updated_at, provider, stalled_at "
    "from ssc.org_domain where org_id = :org and state <> 'retired'"
)
_IN_STATE = text(
    "select "
    "id, org_id, suffix, zone_name, name_servers, state, observed_ns, failure, certificate, "
    "last_delegated_at, retire_after, created_at, updated_at, provider, stalled_at "
    "from ssc.org_domain where org_id = :org and state = :state order by created_at, id"
)
_OPEN = text(
    "select "
    "id, org_id, suffix, zone_name, name_servers, state, observed_ns, failure, certificate, "
    "last_delegated_at, retire_after, created_at, updated_at, provider, stalled_at "
    "from ssc.org_domain where org_id = :org and state in ('pending_dns', 'issuing') "
    "order by created_at, id"
)
_LOCK = text(
    "select "
    "id, org_id, suffix, zone_name, name_servers, state, observed_ns, failure, certificate, "
    "last_delegated_at, retire_after, created_at, updated_at, provider, stalled_at "
    "from ssc.org_domain where org_id = :org and id = :id for update"
)
_RESTART = text(
    "update ssc.org_domain set state = 'pending_dns', failure = null, observed_ns = null, "
    "name_servers = '{}', certificate = null, provider = :provider, last_delegated_at = null, "
    "stalled_at = null, created_at = now(), updated_at = now(), "
    "retire_after = now() + interval '12 months' "
    "where org_id = :org and id = :id and state = 'failed' returning "
    "id, org_id, suffix, zone_name, name_servers, state, observed_ns, failure, certificate, "
    "last_delegated_at, retire_after, created_at, updated_at, provider, stalled_at"
)
_STALLED = text(
    "update ssc.org_domain set stalled_at = :at where org_id = :org and id = :id "
    "and stalled_at is null returning id"
)
_UPDATE = text(
    "update ssc.org_domain set state = :state, failure = :failure, "
    "observed_ns = coalesce(cast(:observed as jsonb), observed_ns), "
    "name_servers = coalesce(cast(:ns as text[]), name_servers), "
    "certificate = coalesce(:certificate, certificate), "
    "last_delegated_at = coalesce(cast(:delegated as timestamptz), last_delegated_at), "
    "retire_after = coalesce(cast(:delegated as timestamptz), last_delegated_at, created_at) "
    "+ interval '12 months', "
    "updated_at = now() where org_id = :org and id = :id returning "
    "id, org_id, suffix, zone_name, name_servers, state, observed_ns, failure, certificate, "
    "last_delegated_at, retire_after, created_at, updated_at, provider, stalled_at"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class OrgDomain:
    id: str
    org_id: str
    suffix: str
    zone_name: str
    name_servers: tuple[str, ...]
    state: DomainState
    observed_ns: Mapping[str, tuple[str, ...]] | None
    failure: str | None
    certificate: str | None
    last_delegated_at: datetime | None
    retire_after: datetime
    created_at: datetime
    updated_at: datetime
    provider: str | None = None
    stalled_at: datetime | None = None


def _row(row: Row[Any]) -> OrgDomain:
    observed = cast("dict[str, list[str]] | None", row.observed_ns)
    return OrgDomain(
        id=row.id,
        org_id=row.org_id,
        suffix=row.suffix,
        zone_name=row.zone_name,
        name_servers=tuple(row.name_servers),
        state=row.state,
        observed_ns=None if observed is None else {k: tuple(v) for k, v in observed.items()},
        failure=row.failure,
        certificate=row.certificate,
        last_delegated_at=row.last_delegated_at,
        retire_after=row.retire_after,
        created_at=row.created_at,
        updated_at=row.updated_at,
        provider=row.provider,
        stalled_at=row.stalled_at,
    )


async def insert_domain(  # noqa: PLR0913
    conn: AsyncConnection,
    *,
    org_id: str,
    suffix: str,
    zone_name: str,
    name_servers: Sequence[str] = (),
    certificate: str | None = None,
    state: DomainState = "pending_dns",
    provider: str | None = None,
) -> OrgDomain:
    params = {
        "id": new_id("dom"),
        "org": org_id,
        "suffix": suffix,
        "zone": zone_name,
        "ns": list(name_servers),
        "state": state,
        "certificate": certificate,
        "provider": provider,
    }
    return _row((await conn.execute(_INSERT, params)).one())


async def domain_of(conn: AsyncConnection, org_id: str) -> OrgDomain | None:
    """The org's domain that is not retired."""
    row = (await conn.execute(_LIVE, {"org": org_id})).one_or_none()
    return None if row is None else _row(row)


async def lock_domain(conn: AsyncConnection, org_id: str, domain_id: str) -> OrgDomain | None:
    row = (await conn.execute(_LOCK, {"org": org_id, "id": domain_id})).one_or_none()
    return None if row is None else _row(row)


async def restart_domain(
    conn: AsyncConnection, org_id: str, domain_id: str, *, provider: str | None
) -> OrgDomain:
    """A ``failed`` row asked for again: ``pending_dns`` from now, as if new."""
    params = {"org": org_id, "id": domain_id, "provider": provider}
    return _row((await conn.execute(_RESTART, params)).one())


async def mark_stalled(conn: AsyncConnection, org_id: str, domain_id: str, at: datetime) -> bool:
    """True when this call set ``stalled_at``; False when it was already set."""
    found = await conn.execute(_STALLED, {"org": org_id, "id": domain_id, "at": at})
    return found.one_or_none() is not None


async def open_domains(engine: AsyncEngine) -> list[OrgDomain]:
    """Every org's ``pending_dns`` and ``issuing`` rows, each org read in its own bind."""
    found: list[OrgDomain] = []
    for org_id in await all_org_ids(engine):
        async with bound_org(engine, org_id) as conn:
            found.extend(_row(r) for r in (await conn.execute(_OPEN, {"org": org_id})).all())
    return found


async def suffix_list(
    conn: AsyncConnection, org_id: str, apps_suffix: str | None
) -> tuple[str, ...]:
    """The previous suffixes, then an ``active`` domain not yet the org's suffix (the pre-roll)."""
    listed = [r.suffix for r in await retired_suffixes(conn, org_id)]
    row = await domain_of(conn, org_id)
    if row is not None and row.state == "active":
        listed.append(row.suffix)
    return tuple(s for s in dict.fromkeys(listed) if s != apps_suffix)


async def domains_in_state(
    conn: AsyncConnection, org_id: str, state: DomainState
) -> list[OrgDomain]:
    rows = (await conn.execute(_IN_STATE, {"org": org_id, "state": state})).all()
    return [_row(r) for r in rows]


async def update_state(  # noqa: PLR0913
    conn: AsyncConnection,
    *,
    org_id: str,
    domain_id: str,
    state: DomainState,
    observed_ns: Mapping[str, Collection[str]] | None = None,
    failure: str | None = None,
    last_delegated_at: datetime | None = None,
    name_servers: Sequence[str] | None = None,
    certificate: str | None = None,
) -> OrgDomain:
    """Set ``state`` and ``failure`` (None clears it); the rest keep their value when None.
    ``retire_after`` follows ``last_delegated_at``, or ``created_at`` until it is set."""
    observed = (
        None
        if observed_ns is None
        else json.dumps({k: sorted(v) for k, v in sorted(observed_ns.items())})
    )
    params = {
        "org": org_id,
        "id": domain_id,
        "state": state,
        "failure": failure,
        "observed": observed,
        "ns": None if name_servers is None else list(name_servers),
        "certificate": certificate,
        "delegated": last_delegated_at,
    }
    return _row((await conn.execute(_UPDATE, params)).one())
