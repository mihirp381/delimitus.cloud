"""What an environment's database may have run, and what a release knows (SSC-043).

A release keeps the migrations its build found in the source, per ledger tool, in the order the
tool applies them (``ssc_bundle.migrations``); NULL when the build did not read the source. Each
time the deploy job hands a release to the runtime for an environment with a database, the
release's migrations join that database's (``ssc.app_database.migrations``, ``record_seen``):
an app may run its migrations at start, and a rollback does not undo them. Nothing here reads the
app's database: the cell agent never connects to it.

``ahead`` is what a rollback is checked against: the migrations the database may have run that
the target release does not have. It is empty when the environment has no database or the
release's migrations are unknown.
"""

import json
from collections.abc import Mapping
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

type Ledgers = dict[str, list[str]]

_SEEN = text(
    "select migrations from ssc.app_database where org_id = :org and environment_id = :env"
)
_SEEN_FOR_UPDATE = text(
    "select migrations from ssc.app_database where org_id = :org and environment_id = :env "
    "for update"
)
_SET_SEEN = text(
    "update ssc.app_database set migrations = cast(:migrations as jsonb) "
    "where org_id = :org and environment_id = :env"
)
_RELEASE = text(
    "select migrations from ssc.release where org_id = :org and app_id = :app and id = :id"
)


def ledgers_of(value: object) -> Ledgers:
    """A stored ``migrations`` object as ledgers; anything else in it is left out."""
    if not isinstance(value, Mapping):
        return {}
    out: Ledgers = {}
    for ledger, names in cast("Mapping[object, object]", value).items():
        if isinstance(ledger, str) and isinstance(names, list):
            kept = [n for n in cast("list[object]", names) if isinstance(n, str)]
            if kept:
                out[ledger] = kept
    return out


def merged(seen: Ledgers, more: Ledgers) -> Ledgers:
    """``seen`` with the names of ``more`` it lacks added after its own, in ``more``'s order."""
    out = {ledger: list(names) for ledger, names in seen.items()}
    for ledger, names in more.items():
        have = out.setdefault(ledger, [])
        known = set(have)
        have += [n for n in names if n not in known]
    return out


def missing(seen: Ledgers, release: Ledgers) -> Ledgers:
    """The names of ``seen`` that ``release`` does not have, per ledger, in ``seen``'s order."""
    out: Ledgers = {}
    for ledger, names in seen.items():
        known = set(release.get(ledger, ()))
        if lacking := [n for n in names if n not in known]:
            out[ledger] = lacking
    return out


async def ahead(
    conn: AsyncConnection, *, org_id: str, app_id: str, environment_id: str, release_id: str
) -> Ledgers:
    """The migrations the environment's database may have run that the release lacks."""
    params = {"org": org_id, "env": environment_id}
    seen = (await conn.execute(_SEEN, params)).first()
    if seen is None:
        return {}
    known = (await conn.execute(_RELEASE, {"org": org_id, "app": app_id, "id": release_id})).first()
    if known is None or known.migrations is None:
        return {}
    return missing(ledgers_of(seen.migrations), ledgers_of(known.migrations))


async def record_seen(
    conn: AsyncConnection, *, org_id: str, environment_id: str, migrations: object
) -> None:
    """Add a release's ``migrations`` to the environment's database, which must exist."""
    more = ledgers_of(migrations)
    if not more:
        return
    params = {"org": org_id, "env": environment_id}
    seen = ledgers_of((await conn.execute(_SEEN_FOR_UPDATE, params)).scalar_one())
    now = merged(seen, more)
    if now != seen:
        await conn.execute(_SET_SEEN, {**params, "migrations": json.dumps(now)})
