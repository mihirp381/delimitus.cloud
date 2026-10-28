"""A customer domain's zone and certificate through the cell agent (CD-3)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, cast

import httpx2

from ssc_contracts.domains import (
    CERTIFICATE_STATES,
    CertificateState,
    name_server,
    resource_name,
)
from ssc_contracts.snapshot import check_suffix
from ssc_control.runtime.cell_agent import IdTokens
from ssc_shared.redaction import redact
from ssc_shared.runtime import ORG_HEADER, check_org

CALL_TIMEOUT_SECONDS: Final = 300.0
MAX_NAME_SERVERS: Final = 16
MAX_DETAIL: Final = 1000
MAX_CERTIFICATE: Final = 400


class CellDomainsError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(redact(message))


class CellDomainNotFoundError(CellDomainsError):
    """The cell has no zone or certificate for the suffix."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CreatedDomain:
    zone_name: str
    name_servers: tuple[str, ...]
    certificate: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainStatus:
    name_servers: tuple[str, ...]
    state: CertificateState
    detail: str | None


class CellDomains(Protocol):
    async def create(self, suffix: str) -> CreatedDomain:
        """Make or find the suffix's zone, records, certificate and map entry."""
        ...

    async def status(self, suffix: str) -> DomainStatus: ...

    async def retire(self, suffix: str) -> None:
        """Remove the certificate and every record but NS and SOA; the zone stays."""
        ...


class AgentCellDomains(CellDomains):
    def __init__(
        self,
        agent_url: str,
        id_tokens: IdTokens,
        *,
        org_id: str,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._url = agent_url.rstrip("/")
        self._id_tokens = id_tokens
        self._org = check_org(org_id)
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def create(self, suffix: str) -> CreatedDomain:
        return created_of(suffix, await self._call("create", suffix))

    async def status(self, suffix: str) -> DomainStatus:
        return status_of(await self._call("status", suffix))

    async def retire(self, suffix: str) -> None:
        body = await self._call("retire", suffix)
        if body.get("retired") != suffix:
            raise CellDomainsError("cell agent domains retire: the answer names another suffix")

    async def _call(self, method: str, suffix: str) -> dict[str, Any]:
        token = await self._id_tokens(self._url)
        try:
            response = await self._client.post(
                f"{self._url}/v1/domains/{method}",
                json={"suffix": check_suffix(suffix)},
                headers={"Authorization": f"Bearer {token}", ORG_HEADER: self._org},
            )
        except httpx2.HTTPError as exc:
            raise CellDomainsError(f"cell agent domains {method}: {type(exc).__name__}") from None
        try:
            parsed: object = response.json()
        except ValueError:
            parsed = None
        body = cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else {}
        if not response.is_success:
            code = body.get("code", "")
            message = f"cell agent domains {method}: HTTP {response.status_code} {code} " + str(
                body.get("message", response.reason_phrase)
            )
            if response.status_code == 404 and code == "DOMAIN_NOT_FOUND":
                raise CellDomainNotFoundError(message)
            raise CellDomainsError(message)
        return body

    async def aclose(self) -> None:
        await self._client.aclose()


def _name_servers(method: str, value: object) -> tuple[str, ...]:
    items = cast("list[object]", value) if isinstance(value, list) else None
    if items is None or len(items) > MAX_NAME_SERVERS or not all(isinstance(v, str) for v in items):
        raise CellDomainsError(f"cell agent domains {method}: name_servers is not a list of hosts")
    try:
        return tuple(name_server(cast("str", v)) for v in items)
    except ValueError:
        raise CellDomainsError(
            f"cell agent domains {method}: a name server is not a host"
        ) from None


def created_of(suffix: str, body: Mapping[str, Any]) -> CreatedDomain:
    zone, certificate = resource_name(suffix), body.get("certificate")
    if body.get("zone_name") != zone:
        raise CellDomainsError("cell agent domains create: zone_name is not the suffix's zone")
    if (
        not isinstance(certificate, str)
        or not certificate.endswith(f"/certificates/{zone}")
        or len(certificate) > MAX_CERTIFICATE
    ):
        raise CellDomainsError("cell agent domains create: certificate is not the suffix's")
    return CreatedDomain(
        zone_name=zone,
        name_servers=_name_servers("create", body.get("name_servers")),
        certificate=certificate,
    )


def status_of(body: Mapping[str, Any]) -> DomainStatus:
    state, detail = body.get("state"), body.get("detail")
    if state not in CERTIFICATE_STATES:
        raise CellDomainsError("cell agent domains status: state is not a certificate state")
    if detail is not None and (not isinstance(detail, str) or not 0 < len(detail) <= MAX_DETAIL):
        raise CellDomainsError("cell agent domains status: detail is not a short string")
    return DomainStatus(
        name_servers=_name_servers("status", body.get("name_servers")),
        state=state,
        detail=detail,
    )


FAKE_NAME_SERVERS: Final = ("ns-cloud-a1.googledomains.com", "ns-cloud-a2.googledomains.com")


@dataclass(slots=True)
class FakeCellDomains(CellDomains):
    """In memory: ``create`` starts a suffix provisioning; ``advance`` moves it on."""

    name_servers: tuple[str, ...] = FAKE_NAME_SERVERS
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    states: dict[str, DomainStatus] = field(default_factory=dict[str, DomainStatus])

    async def create(self, suffix: str) -> CreatedDomain:
        self.calls.append(("create", suffix))
        zone = resource_name(suffix)
        self.states.setdefault(
            suffix, DomainStatus(name_servers=self.name_servers, state="provisioning", detail=None)
        )
        return CreatedDomain(
            zone_name=zone,
            name_servers=self.name_servers,
            certificate=f"projects/ssc-c-fake/locations/global/certificates/{zone}",
        )

    async def status(self, suffix: str) -> DomainStatus:
        self.calls.append(("status", suffix))
        found = self.states.get(suffix)
        if found is None:
            raise CellDomainNotFoundError(f"cell agent domains status: no domain {suffix}")
        return found

    async def retire(self, suffix: str) -> None:
        self.calls.append(("retire", suffix))
        self.states.pop(suffix, None)

    def advance(self, suffix: str, state: CertificateState, detail: str | None = None) -> None:
        self.states[suffix] = DomainStatus(
            name_servers=self.name_servers, state=state, detail=detail
        )


__all__ = [
    "AgentCellDomains",
    "CellDomainNotFoundError",
    "CellDomains",
    "CellDomainsError",
    "CreatedDomain",
    "DomainStatus",
    "FakeCellDomains",
    "created_of",
    "status_of",
]
