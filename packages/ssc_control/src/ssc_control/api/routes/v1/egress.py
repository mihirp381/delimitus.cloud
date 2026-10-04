"""The org's egress allowlist (SSC-053): the hosts its apps may reach through the cell's proxy,
the IT catalogue of common destinations, and the fixed address the cell's traffic leaves from.

Anyone in the org may read the allowlist and the catalogue. Only an active org admin, never in
an agent session, adds or removes a host. A high-risk catalogue host (a public AI API, a
file-sharing site) is added only with ``acknowledge_high_risk``. The first host turns on the
cell's ``egress`` resource, which brings the proxy machine with no human step.
"""

import logging
from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Path, Request
from pydantic import Field

from ssc_contracts.egress import CATALOGUE, MAX_HOSTS, catalogue_entry
from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_admin
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Strict
from ssc_control.api.runtime import runtime_of
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.egress import allowlist
from ssc_control.runtime.cell_egress import CellEgressError, EgressInfo

log = logging.getLogger(__name__)

router = APIRouter()

Host = Annotated[
    str,
    Path(
        max_length=255,
        description="A lower-case host name, or `*.` and a name for exactly one label more.",
    ),
]
_CHANGE: Final = (*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.AGENT_SESSION_REFUSED)


class EgressHostOut(Strict):
    host: str
    high_risk: bool = Field(description="A catalogue host data can leave by in bulk.")
    added_by_user_id: str | None
    approval_request_id: str | None = Field(
        description="The approved request that added it, when an approval did."
    )
    created_at: datetime


class EgressHostsOut(Strict):
    hosts: list[EgressHostOut] = Field(max_length=MAX_HOSTS)


class EgressOut(EgressHostsOut):
    outbound_ip: str | None = Field(
        description="The fixed address the cell's outbound traffic leaves from, for a partner's "
        "firewall; null when the cell cannot say."
    )
    proxy_address: str | None = Field(description="The proxy's internal address in the cell.")


class AllowHost(Strict):
    acknowledge_high_risk: bool = Field(
        default=False, description="Required to add a high-risk catalogue host."
    )


class CatalogueEntryOut(Strict):
    host: str
    purpose: str
    high_risk: bool
    note: str
    listed: bool = Field(description="Whether the org's allowlist has it.")


class CatalogueOut(Strict):
    entries: list[CatalogueEntryOut]


async def _hosts(uow: UnitOfWork) -> EgressHostsOut:
    rows = await allowlist.hosts(uow.conn, uow.org_id)
    return EgressHostsOut(
        hosts=[
            EgressHostOut(
                host=r.host,
                high_risk=r.high_risk,
                added_by_user_id=r.added_by_user_id,
                approval_request_id=r.approval_request_id,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


async def _change(uow: UnitOfWork) -> None:
    await require_admin(uow)
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)


@router.get(
    "/egress",
    response_model=EgressOut,
    responses=problem_responses(*AUTHENTICATED),
)
async def get_egress(request: Request, uow: UserUoW) -> EgressOut:
    """The allowlist, and the cell's fixed outbound address when its agent can say."""
    listed = await _hosts(uow)
    info = EgressInfo(proxy_address=None, outbound_ip=None)
    cell = runtime_of(request).cell_egress
    if cell is not None:
        try:
            info = await cell.info()
        except CellEgressError as exc:
            log.warning("egress info failed", extra={"error": str(exc)})
    return EgressOut(
        hosts=listed.hosts, outbound_ip=info.outbound_ip, proxy_address=info.proxy_address
    )


@router.put(
    "/egress/hosts/{host}",
    response_model=EgressHostsOut,
    responses=problem_responses(*_CHANGE),
)
async def allow_host(host: Host, body: AllowHost, uow: UserUoW) -> EgressHostsOut:
    """Add a host. Active org admins only, never in an agent session. A host already listed
    changes nothing. ``VALIDATION_FAILED`` names why a host is not an entry (an IP address, a
    port, a wildcard anywhere but the front), a high-risk host without
    ``acknowledge_high_risk``, or a full list. Audited as ``org.updated`` on ``egress_host``."""
    await _change(uow)
    entry = catalogue_entry(host)
    if entry is not None and entry.high_risk and not body.acknowledge_high_risk:
        raise Refusal(
            ErrorCode.VALIDATION_FAILED,
            evidence={"host": host, "high_risk": True, "required": "acknowledge_high_risk"},
        )
    try:
        await allowlist.allow(uow.conn, org_id=uow.org_id, host=host, actor=actor_of(uow.principal))
    except allowlist.EgressHostError as exc:
        raise Refusal(ErrorCode.VALIDATION_FAILED, evidence=exc.evidence) from None
    return await _hosts(uow)


@router.delete(
    "/egress/hosts/{host}",
    response_model=EgressHostsOut,
    responses=problem_responses(*_CHANGE, ErrorCode.NOT_FOUND),
)
async def remove_host(host: Host, uow: UserUoW) -> EgressHostsOut:
    """Remove a host. Active org admins only, never in an agent session; ``NOT_FOUND`` when it
    is not listed. The proxy closes open tunnels to it within its drain time of reading the next
    snapshot. Audited as ``org.updated`` on ``egress_host``."""
    await _change(uow)
    removed = await allowlist.remove(
        uow.conn, org_id=uow.org_id, host=host, actor=actor_of(uow.principal)
    )
    if not removed:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"host": host})
    return await _hosts(uow)


@router.get(
    "/egress/catalogue",
    response_model=CatalogueOut,
    responses=problem_responses(*AUTHENTICATED),
)
async def get_catalogue(uow: UserUoW) -> CatalogueOut:
    """Common destinations IT allows, with the high-risk ones flagged, and which are listed."""
    listed = {r.host for r in await allowlist.hosts(uow.conn, uow.org_id)}
    return CatalogueOut(
        entries=[
            CatalogueEntryOut(
                host=e.host,
                purpose=e.purpose,
                high_risk=e.high_risk,
                note=e.note,
                listed=e.host in listed,
            )
            for e in CATALOGUE
        ]
    )
