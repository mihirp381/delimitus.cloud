"""Compile an org's ``ssc-snapshot/v1`` and publish it to the blob store (decision 019).

``publish`` holds the org's snapshot lock exclusively, so versions are assigned one at a time
and every transaction that took the lock shared (``service.mark_dirty``) has committed before
the compile reads. The object key carries the version and a digest prefix: an object left by a
rolled-back publish is never referenced. ``point_latest`` moves ``latest.json`` after commit.
``is_stale`` tells the sweep when the newest published version, or the pointer, lags the org.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ssc_contracts.egress import MAX_CREDENTIALS
from ssc_contracts.snapshot import FORMAT_V1, SnapshotDoc
from ssc_control.db.bind import bound_org
from ssc_control.domain.grant_rules import floor_of
from ssc_shared.blobstore import BlobError, BlobStore
from ssc_shared.canonical import canonical_bytes, canonical_digest
from ssc_shared.hosts import host_label, slug_problem
from ssc_shared.runtime import REQUEST_TIMEOUT_SECONDS
from ssc_shared.snapshot_feed import latest_key, object_key

LOCK_CLASS: Final = 21
"""First key of the org's snapshot advisory lock; the second is ``hashtext(org_id)``."""
CONTENT_TYPE: Final = "application/json"
PUT_TIMEOUT_SECONDS: Final = 20.0
"""Bounds how long a publish holds the lock, and so how long a sharing change can wait."""
POINTER_ATTEMPTS: Final = 5
_UNVERSIONED: Final = frozenset({"version", "compiled_at"})
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

LOCK_SHARED = text("select pg_advisory_xact_lock_shared(:cls, hashtext(:org))")
_LOCK_TIMEOUT = text("select set_config('lock_timeout', '30s', true)")
_LOCK_EXCLUSIVE = text("select pg_advisory_xact_lock(:cls, hashtext(:org))")
NEXT_VERSION = text(
    "select coalesce(max(version), 0) + 1 from ssc.access_snapshot where org_id = :org"
)
_NEWEST = text(
    "select version, object_key, digest, content_digest from ssc.access_snapshot "
    "where org_id = :org order by version desc limit 1"
)
_INSERT = text(
    "insert into ssc.access_snapshot "
    "(org_id, version, digest, content_digest, object_key, compiled_at) "
    "values (:org, :version, :digest, :content, :key, :at)"
)
# One statement, so one read snapshot: every grant's environment and user is in the result, and
# every proxy credential's environment.
_READ_ORG = text(
    "select "
    "(select coalesce(jsonb_agg(jsonb_build_array(e.id, e.app_id, e.name, a.status, a.slug, "
    "e.request_timeout_seconds) order by e.id), '[]') from ssc.environment e join ssc.app a "
    "on a.org_id = e.org_id and a.id = e.app_id where e.org_id = :org), "
    "(select coalesce(jsonb_agg(jsonb_build_array(g.id, g.environment_id, g.role, "
    "g.subject_kind, coalesce(g.user_id, g.group_id)) order by g.environment_id, g.id), '[]') "
    "from ssc.app_grant g where g.org_id = :org), "
    "(select coalesce(jsonb_agg(jsonb_build_array(u.id, u.status, "
    "ceil(extract(epoch from u.sessions_not_before))::bigint) order by u.id), '[]') "
    "from ssc.user_account u where u.org_id = :org), "
    "(select coalesce(jsonb_agg(jsonb_build_array(m.user_id, m.group_id) "
    "order by m.user_id, m.group_id), '[]') from ssc.group_member m where m.org_id = :org), "
    "(select coalesce(jsonb_agg(h.host order by h.host collate \"C\"), '[]') "
    "from ssc.egress_host h where h.org_id = :org), "
    "(select coalesce(jsonb_agg(jsonb_build_array(c.environment_id, c.credential_id, c.sha1) "
    "order by c.environment_id, c.created_at, c.credential_id), '[]') from (select k.*, "
    "row_number() over (partition by k.environment_id order by k.created_at desc, "
    "k.credential_id desc) as n from ssc.egress_credential k where k.org_id = :org) c "
    "where c.n <= :keep), "
    "(select coalesce(jsonb_agg(jsonb_build_array(k.id, k.name, k.kind, k.status, k.limits, "
    "(select coalesce(jsonb_agg(jsonb_build_array(g.environment_id, g.limits) "
    "order by g.environment_id), '[]') from ssc.connection_grant g "
    "where g.org_id = k.org_id and g.connection_id = k.id)) order by k.name collate \"C\"), '[]') "
    "from ssc.connection k where k.org_id = :org and k.setup_status = 'ready')"
)


def document_bytes(doc: SnapshotDoc) -> bytes:
    """The published form: RFC 8785 canonical JSON."""
    return canonical_bytes(doc.model_dump(mode="json"))


def content_digest(doc: SnapshotDoc) -> str:
    """``sha256:`` over the document without ``version`` and ``compiled_at``: equal for two
    compiles that would decide every request the same way."""
    body = {k: v for k, v in doc.model_dump(mode="json").items() if k not in _UNVERSIONED}
    return canonical_digest(body)


def _egress(hosts: list[str], credentials: list[list[str]]) -> dict[str, Any] | None:
    """The proxy's member: the allowlist and each environment's newest credentials, or None
    (left out of the document) while the org has neither."""
    if not hosts and not credentials:
        return None
    by_env: dict[str, list[dict[str, str]]] = {}
    for env_id, credential_id, sha1 in credentials:
        by_env.setdefault(env_id, []).append({"credential_id": credential_id, "sha1": sha1})
    return {"hosts": hosts, "credentials": by_env}


def _connections(linked: list[list[Any]]) -> dict[str, Any] | None:
    """The data gateway's member: each ready connection with the environments granted it, or
    None (left out of the document) while no connection is ready."""
    if not linked:
        return None
    return {
        name: {
            "connection_id": con_id,
            "kind": kind,
            "status": status,
            "limits": limits or None,
            "grants": {env_id: {"limits": env_limits or None} for env_id, env_limits in grants},
        }
        for con_id, name, kind, status, limits, grants in linked
    }


def _longer_timeout(seconds: int | None) -> int | None:
    """An environment's stored request timeout when it is longer than the request-billed one;
    None, which the document leaves out and a reader takes as that figure, otherwise."""
    return seconds if seconds is not None and seconds > REQUEST_TIMEOUT_SECONDS else None


async def compile_document(
    conn: AsyncConnection, org_id: str, *, version: int, compiled_at: datetime
) -> SnapshotDoc:
    """The org's snapshot as of one read. Pure reads in ``conn``'s org-bound transaction."""
    read = {"org": org_id, "keep": MAX_CREDENTIALS}
    envs, grants, users, members, egress_hosts, credentials, linked = (
        await conn.execute(_READ_ORG, read)
    ).one()
    environments: dict[str, Any] = {
        env_id: {
            "app_id": app_id,
            "name": name,
            "status": status,
            "floor": floor_of(name),
            "timeout_seconds": _longer_timeout(timeout),
        }
        for env_id, app_id, name, status, _, timeout in envs
    }
    hosts: dict[str, str] = {}
    for env_id, _, name, _, slug, _ in envs:
        if slug_problem(slug) is None:
            hosts[host_label(slug, name)] = env_id
    by_env: dict[str, list[dict[str, Any]]] = {env_id: [] for env_id in environments}
    for grant_id, env_id, role, kind, subject in grants:
        by_env[env_id].append(
            {"grant_id": grant_id, "role": role, "subject_kind": kind, "subject_id": subject}
        )
    groups: dict[str, list[str]] = {}
    for user_id, group_id in members:
        groups.setdefault(user_id, []).append(group_id)
    return SnapshotDoc.model_validate(
        {
            "format": FORMAT_V1,
            "org_id": org_id,
            "version": version,
            "compiled_at": compiled_at,
            "environments": environments,
            "hosts": hosts,
            "grants": by_env,
            "groups_by_user": groups,
            "users": {
                user_id: {"status": status, "sessions_not_before": not_before}
                for user_id, status, not_before in users
            },
            "ceiling": None,
            "connections": _connections(linked),
            "egress": _egress(egress_hosts, credentials),
        }
    )


async def publish(conn: AsyncConnection, org_id: str, blob: BlobStore, *, at: datetime) -> int:
    """Compile and store the next version in ``conn``'s org-bound transaction; its number.

    The object is written before the row, so a committed row always has its object."""
    await conn.execute(_LOCK_TIMEOUT)
    await conn.execute(_LOCK_EXCLUSIVE, {"cls": LOCK_CLASS, "org": org_id})
    version = int((await conn.execute(NEXT_VERSION, {"org": org_id})).scalar_one())
    doc = await compile_document(conn, org_id, version=version, compiled_at=at)
    body = document_bytes(doc)
    sha = hashlib.sha256(body).hexdigest()
    key = object_key(org_id, version, sha)
    async with asyncio.timeout(PUT_TIMEOUT_SECONDS):
        await blob.put(key, body, content_type=CONTENT_TYPE, size=len(body), sha256=sha)
    await conn.execute(
        _INSERT,
        {
            "org": org_id,
            "version": version,
            "digest": f"sha256:{sha}",
            "content": content_digest(doc),
            "key": key,
            "at": at,
        },
    )
    return version


async def point_latest(engine: AsyncEngine, org_id: str, blob: BlobStore) -> int | None:
    """Point ``latest.json`` at the newest committed version, re-reading until it is stable,
    so a slower publisher never leaves the pointer on an older version. Returns that version."""
    written: int | None = None
    for _ in range(POINTER_ATTEMPTS):
        async with bound_org(engine, org_id) as conn:
            row = (await conn.execute(_NEWEST, {"org": org_id})).first()
        if row is None or int(row[0]) == written:
            return written
        version = int(row[0])
        body = canonical_bytes({"version": version, "key": str(row[1]), "digest": str(row[2])})
        sha = hashlib.sha256(body).hexdigest()
        await blob.put(
            latest_key(org_id), body, content_type=CONTENT_TYPE, size=len(body), sha256=sha
        )
        written = version
    return written


async def pointed_version(blob: BlobStore, org_id: str) -> int | None:
    """The version ``latest.json`` names; None when it is missing or unreadable."""
    try:
        raw = b"".join([chunk async for chunk in blob.get(latest_key(org_id))])
        version = json.loads(raw)["version"]
    except BlobError, OSError, ValueError, KeyError, TypeError:
        return None
    return version if isinstance(version, int) else None


async def is_stale(engine: AsyncEngine, org_id: str, blob: BlobStore) -> bool:
    """True when the org has no published version, when its newest version's content differs
    from a live compile, or when ``latest.json`` does not name that version."""
    async with bound_org(engine, org_id) as conn:
        newest = (await conn.execute(_NEWEST, {"org": org_id})).first()
        live = await compile_document(conn, org_id, version=0, compiled_at=_EPOCH)
    if newest is None or newest[3] != content_digest(live):
        return True
    return await pointed_version(blob, org_id) != int(newest[0])
