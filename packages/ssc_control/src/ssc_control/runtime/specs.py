"""Where a release's manifest comes from: a port, because the bundle table arrives with B3.

``reconcile_env`` never guesses a manifest. Until B3 stores bundles, the worker runs with
``NoReleaseSpecs`` and every environment reports ``no_spec`` instead of being changed.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.manifest import Manifest


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseSpec:
    manifest: Manifest
    framework: str | None = None  # what the build detected (B4)


class ReleaseSpecUnavailableError(LookupError):
    """No manifest for this release (yet). The reconciler leaves the environment alone."""


class ReleaseSpecs(Protocol):
    async def get(
        self, conn: AsyncConnection, *, org_id: str, app_id: str, release_id: str
    ) -> ReleaseSpec:
        """Read in the caller's org-bound transaction; raise ``ReleaseSpecUnavailableError``."""
        ...


class NoReleaseSpecs(ReleaseSpecs):
    """Until B3: every release is unavailable."""

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
