"""Release numbers, allocated in the transaction that writes the release (SSC-016).

Mined from Delimitus ``policy.version-allocation-sql``: lock the app row, take the next number,
insert, all in the caller's transaction, never read-then-write without the lock. Concurrent
builds of one app queue on the lock, so each sees the number the previous one committed.
``UNIQUE (org_id, app_id, number)`` is the backstop. Releases are never updated or deleted, so a
number is never reused.
"""

import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.ids import new_id
from ssc_control.audit import Actor

_LOCK_APP = text("select 1 from ssc.app where org_id = :org and id = :app for update")
_NEXT_NUMBER = text(
    "select coalesce(max(number), 0) + 1 from ssc.release where org_id = :org and app_id = :app"
)
_INSERT = text(
    "insert into ssc.release (id, org_id, app_id, number, image_digest, manifest_digest, "
    "source_digest, source_commit, scan_refs, actor_kind, actor_id, actor_via_agent, "
    "actor_client_id) values (:id, :org, :app, :number, :image, :manifest, :source, :commit, "
    "cast(:scan_refs as jsonb), :actor_kind, :actor_id, :via_agent, :client_id)"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class NewRelease:
    app_id: str
    image_digest: str
    manifest_digest: str
    source_digest: str
    source_commit: str | None
    scan_refs: tuple[str, ...]
    actor: Actor


@dataclass(frozen=True, slots=True)
class AllocatedRelease:
    id: str
    number: int


class AppNotFoundError(LookupError):
    pass


async def allocate_and_insert(
    conn: AsyncConnection, *, org_id: str, release: NewRelease
) -> AllocatedRelease:
    """Insert ``release`` as the app's next number, in ``conn``'s org-bound transaction."""
    params = {"org": org_id, "app": release.app_id}
    if (await conn.execute(_LOCK_APP, params)).first() is None:
        raise AppNotFoundError(release.app_id)
    number = int((await conn.execute(_NEXT_NUMBER, params)).scalar_one())
    release_id = new_id("rel")
    actor = release.actor
    await conn.execute(
        _INSERT,
        {
            **params,
            "id": release_id,
            "number": number,
            "image": release.image_digest,
            "manifest": release.manifest_digest,
            "source": release.source_digest,
            "commit": release.source_commit,
            "scan_refs": json.dumps(list(release.scan_refs)),
            "actor_kind": actor.kind.value,
            "actor_id": actor.id,
            "via_agent": actor.via_agent,
            "client_id": actor.client_id,
        },
    )
    return AllocatedRelease(release_id, number)
