"""Where a release's manifest comes from: the stored bundle the release was built from.

``reconcile_env`` never guesses a manifest. ``BundleReleaseSpecs`` (the worker's port) reads the
manifest stored with the release's bundle, joined on ``release.source_digest = bundle.digest``
(decision 015), and re-derives its digest; a release without a stored bundle, or whose manifest
does not hash to ``release.manifest_digest``, reports ``no_spec`` instead of being changed. The
framework is the one the build found and kept on the release (SSC-015).
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.manifest import Manifest
from ssc_shared.canonical import manifest_digest

log = logging.getLogger(__name__)

_RELEASE_MANIFEST = text(
    "select b.manifest, r.manifest_digest, r.framework from ssc.release r "
    "join ssc.bundle b on b.org_id = r.org_id and b.app_id = r.app_id "
    "and b.digest = r.source_digest "
    "where r.org_id = :org and r.id = :rel and b.state = 'stored'"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseSpec:
    manifest: Manifest
    framework: str | None = None


class ReleaseSpecUnavailableError(LookupError):
    """No manifest for this release (yet). The reconciler leaves the environment alone."""


class ReleaseSpecs(Protocol):
    async def get(
        self, conn: AsyncConnection, *, org_id: str, app_id: str, release_id: str
    ) -> ReleaseSpec:
        """Read in the caller's org-bound transaction; raise ``ReleaseSpecUnavailableError``."""
        ...


async def release_manifest(conn: AsyncConnection, org_id: str, release_id: str) -> Manifest | None:
    """The manifest of the bundle ``release_id`` was built from, or None when there is none or it
    does not hash to the release's ``manifest_digest``. An approvals ``ManifestLoader``."""
    spec = await _release_spec(conn, org_id, release_id)
    return None if spec is None else spec.manifest


async def _release_spec(conn: AsyncConnection, org_id: str, release_id: str) -> ReleaseSpec | None:
    row = (await conn.execute(_RELEASE_MANIFEST, {"org": org_id, "rel": release_id})).first()
    if row is None:
        return None
    try:
        manifest = Manifest.model_validate(row[0])
    except ValidationError:
        log.warning("stored manifest does not validate", extra={"release_id": release_id})
        return None
    if manifest_digest(manifest) != row[1]:
        log.warning("stored manifest does not match its release", extra={"release_id": release_id})
        return None
    return ReleaseSpec(manifest=manifest, framework=row[2])


class BundleReleaseSpecs(ReleaseSpecs):
    """The manifest stored with the release's bundle (``release_manifest``) and the framework
    kept on the release."""

    async def get(
        self, conn: AsyncConnection, *, org_id: str, app_id: str, release_id: str
    ) -> ReleaseSpec:
        spec = await _release_spec(conn, org_id, release_id)
        if spec is None:
            raise ReleaseSpecUnavailableError(f"no stored bundle manifest for {release_id}")
        return spec


class NoReleaseSpecs(ReleaseSpecs):
    """Every release is unavailable: the default for ports that never reconcile."""

    async def get(
        self, conn: AsyncConnection, *, org_id: str, app_id: str, release_id: str
    ) -> ReleaseSpec:
        raise ReleaseSpecUnavailableError(f"no bundle store yet for {release_id}")


class StaticReleaseSpecs(ReleaseSpecs):
    """A fixed map from release id, for tests and local runs."""

    def __init__(self, specs: Mapping[str, ReleaseSpec] | None = None) -> None:
        self._specs = dict(specs or {})

    def put(self, release_id: str, spec: ReleaseSpec) -> None:
        self._specs[release_id] = spec

    @property
    def specs(self) -> Mapping[str, ReleaseSpec]:
        return MappingProxyType(self._specs)

    async def get(
        self, conn: AsyncConnection, *, org_id: str, app_id: str, release_id: str
    ) -> ReleaseSpec:
        try:
            return self._specs[release_id]
        except KeyError:
            raise ReleaseSpecUnavailableError(release_id) from None
