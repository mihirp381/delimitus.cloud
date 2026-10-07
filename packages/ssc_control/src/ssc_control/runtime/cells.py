"""Placement: which cell holds an org, and the clients that reach it (decision 029).

Every org has one cell, named by its opaque ``ssc.org.cell_label`` (revision 0011), and every
cell has one agent, at ``hosts.agent_url(label, apps domain)``. ``SSC_CELLS`` says which cells
this control plane serves and what each one's public identity keys are:

    {"<label>": {"identity_jwks": {"keys": [...]}}, ...}

``CellRouter`` reads an org's label (cached for :data:`LABEL_SECONDS`), and hands back that
cell's clients, each sending ``X-SSC-Org: <org id>`` so the agent can refuse any org but its own.
An org whose label is not in the map has no reachable cell: ``CellUnavailableError``, which the
API answers ``CELL_UNAVAILABLE`` and the worker records on the failed job. ``StaticCells`` gives
every org the same ports, for development and tests.

Before ``SSC_CELLS`` there was one cell, set as ``SSC_CELL_AGENT_URL``, ``SSC_IDENTITY_JWKS`` and
``SSC_IDENTITY_ISSUER``. Those still make a one-cell map, with a deprecation warning.
"""

import base64
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Final, Protocol, cast, runtime_checkable

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ssc_control.db.bind import bound_org
from ssc_control.deploy.build_driver import BuildDriver
from ssc_control.deploy.cell_build import CellAgentBuildDriver
from ssc_control.runtime.app_databases import AppDatabases, CellAppDatabases
from ssc_control.runtime.cell_agent import CellAgentDriver, IdTokens
from ssc_control.runtime.cell_egress import AgentCellEgress, CellEgress
from ssc_control.runtime.cell_logs import AgentCellLogs
from ssc_control.runtime.cell_usage import AgentCellUsage
from ssc_control.runtime.driver import AppIdentity, RuntimeDriver
from ssc_control.runtime.secret_grants import CellSecretGrants, SecretGrants
from ssc_shared import hosts
from ssc_shared.blobstore import BlobStore
from ssc_shared.logs import CellLogs
from ssc_shared.usage import CellUsage

log = logging.getLogger(__name__)

CELLS_ENV: Final = "SSC_CELLS"
LEGACY_AGENT_URL_ENV: Final = "SSC_CELL_AGENT_URL"
LEGACY_JWKS_ENV: Final = "SSC_IDENTITY_JWKS"
LEGACY_ISSUER_ENV: Final = "SSC_IDENTITY_ISSUER"
LEGACY_ENVS: Final = (LEGACY_AGENT_URL_ENV, LEGACY_JWKS_ENV, LEGACY_ISSUER_ENV)
LABEL_SECONDS: Final = 300.0
"""How long a process trusts an org's cell label. Labels are set when the org is made and do not
change today; the bound keeps a future move from needing a restart of every process."""
STATIC_LABEL: Final = "staticcell"
CELL_UNAVAILABLE: Final = "CELL_UNAVAILABLE"
"""The failure code of a job for an org whose cell is not configured (``ErrorCode`` too)."""
PRIVATE_JWK_MEMBERS: Final = frozenset({"d", "p", "q", "dp", "dq", "qi", "k"})
_CELL_LABEL = text("select cell_label from ssc.org where id = :org")


class CellUnavailableError(Exception):
    """The org's cell is not one this process can reach: its label is not in ``SSC_CELLS``."""

    def __init__(self, org_id: str, label: str | None) -> None:
        super().__init__(f"no cell configured for {org_id} (label {label!r})")
        self.org_id = org_id
        self.label = label


@dataclass(frozen=True, slots=True)
class CellConfig:
    """One cell this control plane serves: its label and its public identity JWKS."""

    label: str
    identity_jwks: Mapping[str, object]

    @property
    def keys_url(self) -> str:
        """The JWKS as a ``data:`` URL, re-serialised compactly with sorted keys so whitespace
        and member order never change an app's spec."""
        compact = json.dumps(self.identity_jwks, separators=(",", ":"), sort_keys=True).encode()
        return "data:application/json;base64," + base64.b64encode(compact).decode()

    def identity(self, apps_domain: str) -> AppIdentity:
        return AppIdentity(keys_url=self.keys_url, cell_label=self.label, apps_domain=apps_domain)


@dataclass(frozen=True, slots=True, kw_only=True)
class OrgCell:
    """One org's cell and the ports that reach it. A port left None is not configured, with the
    same meaning it had on the ``Ports`` before placement (``RUNTIME_UNAVAILABLE`` and so on)."""

    label: str
    runtime: RuntimeDriver | None = None
    build: BuildDriver | None = None
    app_databases: AppDatabases | None = None
    egress: CellEgress | None = None
    usage: CellUsage | None = None
    logs: CellLogs | None = None
    secret_grants: SecretGrants | None = None
    identity: AppIdentity | None = None


class CellPorts(Protocol):
    async def for_org(self, org_id: str) -> OrgCell:
        """``org_id``'s cell, or ``CellUnavailableError`` when this process cannot reach it."""
        ...


def check_public_jwks(jwks: object, what: str) -> Mapping[str, object]:
    """``jwks`` when it is a JWKS of named keys with no private member, else ``ValueError``."""
    doc = cast("dict[str, object]", jwks) if isinstance(jwks, dict) else None
    keys = doc.get("keys") if doc is not None else None
    if doc is None or not isinstance(keys, list) or not keys:
        raise ValueError(f'{what} must be a JSON object with a non-empty "keys" list')
    for key in cast("list[object]", keys):
        members = cast("dict[str, object]", key) if isinstance(key, dict) else None
        if members is None or not isinstance(members.get("kid"), str):
            raise ValueError(f"{what}: every key must be an object with a kid")
        if PRIVATE_JWK_MEMBERS & set(members):
            raise ValueError(f"{what}: holds a private key; give only the public JWKS")
    return doc


def parse_cells(raw: str) -> Mapping[str, CellConfig]:
    """``SSC_CELLS``: ``{"<label>": {"identity_jwks": {"keys": [...]}}}``. ``ValueError`` for
    anything else, naming the label but never echoing a key."""
    try:
        doc: object = json.loads(raw)
    except ValueError:
        raise ValueError(f"{CELLS_ENV} must be JSON") from None
    if not isinstance(doc, dict):
        raise ValueError(f"{CELLS_ENV} must be an object of cell label to cell")
    cells: dict[str, CellConfig] = {}
    for label, cell in cast("dict[str, object]", doc).items():
        try:
            hosts.check_cell_label(label)
        except ValueError:
            raise ValueError(f"{CELLS_ENV}: {label!r} is not a cell label") from None
        body = cast("dict[str, object]", cell) if isinstance(cell, dict) else None
        if body is None or set(body) != {"identity_jwks"}:
            raise ValueError(f'{CELLS_ENV}: cell {label} must be {{"identity_jwks": {{...}}}}')
        jwks = check_public_jwks(body["identity_jwks"], f"{CELLS_ENV}: cell {label}")
        cells[label] = CellConfig(label=label, identity_jwks=jwks)
    return MappingProxyType(cells)


def legacy_cells(env: Mapping[str, str], apps_domain: str) -> Mapping[str, CellConfig]:
    """The one cell of ``SSC_CELL_AGENT_URL``, ``SSC_IDENTITY_JWKS`` and ``SSC_IDENTITY_ISSUER``,
    all three set. The label is the issuer's; the URL must be that label's agent."""
    url, jwks, issuer = (env.get(name, "") for name in LEGACY_ENVS)
    if not (url and jwks and issuer):
        raise ValueError(f"set {CELLS_ENV} (or, deprecated, all of {', '.join(LEGACY_ENVS)})")
    try:
        label = hosts.label_of_issuer(issuer)
    except ValueError:
        raise ValueError(f"{LEGACY_ISSUER_ENV} must be {hosts.ISSUER_PREFIX}<label>") from None
    if url.rstrip("/") != hosts.agent_url(label, apps_domain):
        raise ValueError(f"{LEGACY_AGENT_URL_ENV} must be the agent of the issuer's cell")
    try:
        parsed: object = json.loads(jwks)
    except ValueError:
        parsed = None
    doc = check_public_jwks(parsed, LEGACY_JWKS_ENV)
    log.warning("%s are deprecated: set %s instead", ", ".join(LEGACY_ENVS), CELLS_ENV)
    return MappingProxyType({label: CellConfig(label=label, identity_jwks=doc)})


def cells_from_env(env: Mapping[str, str], apps_domain: str) -> Mapping[str, CellConfig]:
    """``SSC_CELLS`` when set, else the deprecated one-cell variables when any is set, else no
    cells. ``ValueError`` for a malformed setting."""
    raw = env.get(CELLS_ENV, "")
    if raw:
        return parse_cells(raw)
    if any(env.get(name) for name in LEGACY_ENVS):
        return legacy_cells(env, apps_domain)
    return MappingProxyType({})


class CellRouter(CellPorts):
    """Each org's cell from ``ssc.org.cell_label`` and the cells in ``SSC_CELLS``.

    ``id_tokens`` mint ID tokens for each agent's URL; ``grant_tokens`` fresh ones for secret
    grants. ``build_store`` gives the store a label's bundles are signed from; None builds
    nothing (the API). Clients are made on first use and kept, one set per org since each names
    its org on every call."""

    def __init__(  # noqa: PLR0913  (keyword-only)
        self,
        engine: AsyncEngine,
        cells: Mapping[str, CellConfig],
        *,
        apps_domain: str,
        id_tokens: IdTokens,
        grant_tokens: IdTokens,
        build_store: Callable[[str], BlobStore] | None = None,
        client: httpx2.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._cells = dict(cells)
        self._domain = hosts.check_apps_domain(apps_domain)
        self._id_tokens = id_tokens
        self._grant_tokens = grant_tokens
        self._build_store = build_store
        self._client = client
        self._clock = clock
        self._labels: dict[str, tuple[str, float]] = {}
        self._orgs: dict[tuple[str, str], OrgCell] = {}

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(self._cells)

    async def for_org(self, org_id: str) -> OrgCell:
        label = await self._label(org_id)
        config = self._cells.get(label)
        if config is None:
            raise CellUnavailableError(org_id, label)
        cell = self._orgs.get((org_id, label))
        if cell is None:
            cell = self._orgs[org_id, label] = self._make(org_id, config)
        return cell

    async def _label(self, org_id: str) -> str:
        now = self._clock()
        cached = self._labels.get(org_id)
        if cached is not None and now < cached[1]:
            return cached[0]
        async with bound_org(self._engine, org_id) as conn:
            label = (await conn.execute(_CELL_LABEL, {"org": org_id})).scalar_one_or_none()
        if label is None:
            raise CellUnavailableError(org_id, None)
        self._labels[org_id] = (str(label), now + LABEL_SECONDS)
        return str(label)

    async def aclose(self) -> None:
        """Close the clients made here. A ``client`` passed in stays its owner's to close."""
        cells, self._orgs = list(self._orgs.values()), {}
        if self._client is not None:
            return
        for cell in cells:
            for port in (getattr(cell, f.name) for f in fields(cell)):
                if isinstance(port, _Closing):
                    await port.aclose()

    def _make(self, org_id: str, config: CellConfig) -> OrgCell:
        url = hosts.agent_url(config.label, self._domain)
        tokens, client = self._id_tokens, self._client
        store = self._build_store(config.label) if self._build_store is not None else None
        return OrgCell(
            label=config.label,
            runtime=CellAgentDriver(url, tokens, org_id=org_id, client=client),
            build=CellAgentBuildDriver(url, tokens, store, org_id=org_id, client=client)
            if store is not None
            else None,
            app_databases=CellAppDatabases(url, tokens, org_id=org_id, client=client),
            egress=AgentCellEgress(url, tokens, org_id=org_id, client=client),
            usage=AgentCellUsage(url, tokens, org_id=org_id, client=client),
            logs=AgentCellLogs(url, tokens, org_id=org_id, client=client),
            secret_grants=CellSecretGrants(
                agent_url=url,
                org_id=org_id,
                intake_origin=hosts.intake_url(config.label, self._domain),
                agent_tokens=tokens,
                grant_tokens=self._grant_tokens,
                client=client,
            ),
            identity=config.identity(self._domain),
        )


@runtime_checkable
class _Closing(Protocol):
    async def aclose(self) -> None: ...


class StaticCells(CellPorts):
    """Every org in the one ``cell``, or, for an org in ``orgs``, that org's. An org in neither
    has no cell. For development and tests."""

    def __init__(
        self, cell: OrgCell | None = None, *, orgs: Mapping[str, OrgCell] | None = None
    ) -> None:
        self._cell = cell
        self._orgs = dict(orgs or {})

    @property
    def cell(self) -> OrgCell | None:
        """The cell of every org not in ``orgs``."""
        return self._cell

    async def for_org(self, org_id: str) -> OrgCell:
        cell = self._orgs.get(org_id, self._cell)
        if cell is None:
            raise CellUnavailableError(org_id, None)
        return cell

    def ports(self) -> list[object]:
        """Every port of every cell here, for ``refuse_fakes``."""
        cells = [*([self._cell] if self._cell is not None else []), *self._orgs.values()]
        return [getattr(cell, f.name) for cell in cells for f in fields(cell) if f.name != "label"]


__all__ = [
    "CELLS_ENV",
    "CELL_UNAVAILABLE",
    "LABEL_SECONDS",
    "STATIC_LABEL",
    "CellConfig",
    "CellPorts",
    "CellRouter",
    "CellUnavailableError",
    "OrgCell",
    "StaticCells",
    "cells_from_env",
    "check_public_jwks",
    "legacy_cells",
    "parse_cells",
]
