"""Where the production gate learns what a release asks to reach.

:class:`ManifestCapabilities` reads the release manifest (``[connections]`` and ``[egress]``)
through a loader B4 supplies once it stores manifests; :func:`requested_by_diff` maps B1's
capability diff for a caller that holds only the diff. Until B4, :class:`RecordedCapabilities`
answers from what the environment's records hold: the data connections and internet hosts its
approval requests name. A source answers None when it cannot tell, and the gate refuses on None.
"""

from collections.abc import Awaitable, Callable
from typing import Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.capabilities import CapabilityDiff
from ssc_contracts.manifest import Manifest
from ssc_control.domain.approval_rules import RequestedCapabilities, RequirementKind

# (conn, org_id, release_id) to that release's manifest, or None when it cannot be read.
type ManifestLoader = Callable[[AsyncConnection, str, str], Awaitable[Manifest | None]]

_RELEASE_OF_APP = text(
    "select 1 from ssc.release where org_id = :org and app_id = :app and id = :rel"
)
_CURRENT_RELEASE = text(
    "select d.release_id from ssc.environment e join ssc.deployment d "
    "on d.org_id = e.org_id and d.id = e.current_deployment_id "
    "where e.org_id = :org and e.id = :env"
)
# The newest request per question; a cancelled (withdrawn) one no longer declares anything.
_DECLARED = text(
    "select kind, subject_key from ("
    "select distinct on (kind, subject_key) kind, subject_key, state "
    "from ssc.approval_request where org_id = :org and environment_id = :env "
    "order by kind, subject_key, created_at desc, (state = 'pending') desc, id desc"
    ") newest where state <> 'cancelled'"
)


class CapabilitySource(Protocol):
    async def for_release(
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        app_id: str,
        environment_id: str,
        release_id: str,
    ) -> RequestedCapabilities | None:
        """What ``release_id`` asks for when it runs in ``environment_id``; None if unknown."""
        ...

    async def for_environment(
        self, conn: AsyncConnection, *, org_id: str, environment_id: str
    ) -> RequestedCapabilities | None:
        """What the environment's app currently asks for; None if unknown."""
        ...


class RecordedCapabilities(CapabilitySource):
    """Capabilities named by the environment's approval requests, newest per question."""

    async def for_release(
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        app_id: str,
        environment_id: str,
        release_id: str,
    ) -> RequestedCapabilities | None:
        found = await conn.execute(
            _RELEASE_OF_APP, {"org": org_id, "app": app_id, "rel": release_id}
        )
        if found.first() is None:
            return None
        return await self.for_environment(conn, org_id=org_id, environment_id=environment_id)

    async def for_environment(
        self, conn: AsyncConnection, *, org_id: str, environment_id: str
    ) -> RequestedCapabilities | None:
        connections: set[str] = set()
        hosts: set[str] = set()
        unknown: set[str] = set()
        rows = await conn.execute(_DECLARED, {"org": org_id, "env": environment_id})
        for kind, subject in rows.tuples():
            try:
                known = RequirementKind(str(kind))
            except ValueError:
                unknown.add(str(kind))
                continue
            match known:
                case RequirementKind.CONNECT_DATA_SOURCE:
                    connections.add(str(subject))
                case RequirementKind.ENABLE_INTERNET_HOSTS:
                    hosts.add(str(subject))
                case (
                    RequirementKind.WIDEN_AUDIENCE
                    | RequirementKind.AGENT_SHARE
                    | RequirementKind.EXCEED_CEILING
                ):
                    pass  # sharing questions, not capabilities
        return RequestedCapabilities(
            connections=frozenset(connections),
            egress_hosts=frozenset(hosts),
            unknown=frozenset(unknown),
        )


def requested_by_manifest(manifest: Manifest) -> RequestedCapabilities:
    """What an ``ssc/v1`` manifest asks to reach. The model refuses unknown tables, so a
    capability kind these rules do not know cannot get this far."""
    return RequestedCapabilities(
        connections=frozenset(manifest.connections.names),
        egress_hosts=frozenset(manifest.egress.hosts),
    )


# Diff kinds no approval applies to. Any other kind these rules do not map is unknown.
_NO_APPROVAL: Final = frozenset({"postgres_missing", "schedules_declared"})


def requested_by_diff(diff: CapabilityDiff) -> RequestedCapabilities:
    """What a capability diff asks to reach. Fails closed: a kind not mapped here lands in
    ``unknown``, and so does a summarised diff, whose unlisted changes cannot be checked."""
    connections: set[str] = set()
    hosts: set[str] = set()
    unknown: set[str] = {"summarised_diff"} if diff.summarised else set()
    for change in diff.changes:
        if change.kind == "connection_missing":
            connections.add(change.subject)
        elif change.kind == "egress_host_missing":
            hosts.add(change.subject)
        elif change.kind not in _NO_APPROVAL:
            unknown.add(change.kind)
    return RequestedCapabilities(
        connections=frozenset(connections),
        egress_hosts=frozenset(hosts),
        unknown=frozenset(unknown),
    )


class ManifestCapabilities(CapabilitySource):
    """Capabilities declared by release manifests, read through ``load``."""

    def __init__(self, load: ManifestLoader) -> None:
        self._load = load

    async def for_release(
        self,
        conn: AsyncConnection,
        *,
        org_id: str,
        app_id: str,
        environment_id: str,
        release_id: str,
    ) -> RequestedCapabilities | None:
        found = await conn.execute(
            _RELEASE_OF_APP, {"org": org_id, "app": app_id, "rel": release_id}
        )
        if found.first() is None:
            return None
        manifest = await self._load(conn, org_id, release_id)
        return None if manifest is None else requested_by_manifest(manifest)

    async def for_environment(
        self, conn: AsyncConnection, *, org_id: str, environment_id: str
    ) -> RequestedCapabilities | None:
        """The running release's manifest; nothing running asks for nothing."""
        release_id = (
            await conn.execute(_CURRENT_RELEASE, {"org": org_id, "env": environment_id})
        ).scalar_one_or_none()
        if release_id is None:
            return RequestedCapabilities()
        manifest = await self._load(conn, org_id, str(release_id))
        return None if manifest is None else requested_by_manifest(manifest)
