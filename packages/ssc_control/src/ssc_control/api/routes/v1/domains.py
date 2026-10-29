"""``/v1/domain``: the org's customer domain (CD-4.2). Active org admins only; changes never in
an agent session."""

import logging
from datetime import UTC, datetime
from typing import Final, Literal

from fastapi import APIRouter, Request, Response
from pydantic import Field
from sqlalchemy.exc import IntegrityError

from ssc_contracts.audit import AuditAction
from ssc_contracts.domains import DomainState, resource_name
from ssc_contracts.errors import ErrorCode
from ssc_contracts.snapshot import MAX_SUFFIX
from ssc_control.api.authz import require_admin
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Strict
from ssc_control.api.runtime import cell_of, runtime_of
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.db.domains import (
    OrgDomain,
    domain_of,
    insert_domain,
    restart_domain,
    update_state,
)
from ssc_control.domains.admission import admit
from ssc_control.domains.flip import FlipRefusedError, flip
from ssc_control.domains.provider import parent_provider
from ssc_control.domains.view import DomainView, NoCellLabelError, live_count, rolling_for, view_of
from ssc_control.runtime.cell_domains import (
    CellDomainNotFoundError,
    CellDomains,
    CellDomainsError,
)

log = logging.getLogger(__name__)

router = APIRouter()

_READ: Final = (*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.CELL_UNAVAILABLE)
_CHANGE: Final = (*_READ, ErrorCode.AGENT_SESSION_REFUSED)
_ADMISSION: Final = (
    ErrorCode.DOMAIN_SUFFIX_TOO_SHORT,
    ErrorCode.DOMAIN_SUFFIX_PUBLIC,
    ErrorCode.DOMAIN_SUFFIX_RESERVED,
    ErrorCode.DOMAIN_SUFFIX_HELD,
    ErrorCode.DOMAIN_SUFFIX_OWN,
)


class PreviousSuffixOut(Strict):
    suffix: str
    until: datetime = Field(description="Until when its hosts redirect to the current suffix.")


class DelegationOut(Strict):
    id: str
    suffix: str
    state: DomainState
    name_servers: list[str] = Field(description="The NS records to add at the parent zone.")
    observed_ns: dict[str, list[str]] | None = Field(
        description="What each public resolver last answered for the suffix's NS."
    )
    failure: str | None
    provider: str | None = Field(description="Where the parent zone is hosted, when known.")
    created_at: datetime
    updated_at: datetime
    last_delegated_at: datetime | None


class AppMoveOut(Strict):
    slug: str
    hosts: list[str] = Field(description="Its prod and preview hosts under the coming suffix.")


class DomainOut(Strict):
    default_suffix: str
    current_suffix: str
    previous: list[PreviousSuffixOut]
    delegation: DelegationOut | None
    apps_that_move: list[AppMoveOut]
    ready_to_flip: bool
    rolling: int = Field(
        ge=0, description="Environments not yet rolled with the coming suffix among their origins."
    )


class DomainIn(Strict):
    suffix: str = Field(min_length=1, max_length=MAX_SUFFIX)


class FlipIn(Strict):
    to: Literal["customer", "default"]
    confirm: Literal[True]


def delegation_out(row: OrgDomain) -> DelegationOut:
    return DelegationOut(
        id=row.id,
        suffix=row.suffix,
        state=row.state,
        name_servers=list(row.name_servers),
        observed_ns=None
        if row.observed_ns is None
        else {k: list(v) for k, v in row.observed_ns.items()},
        failure=row.failure,
        provider=row.provider,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_delegated_at=row.last_delegated_at,
    )


async def _view(request: Request, uow: UnitOfWork) -> DomainView:
    try:
        return await view_of(
            uow.conn,
            uow.org_id,
            apps_domain=runtime_of(request).settings.apps_domain,
            now=datetime.now(UTC),
        )
    except NoCellLabelError:
        raise Refusal(ErrorCode.CELL_UNAVAILABLE, evidence={"cell_label": None}) from None


async def _rolling(request: Request, uow: UnitOfWork, view: DomainView) -> int:
    rt = runtime_of(request)
    cell = await cell_of(request, uow.org_id)
    return await rolling_for(uow.conn, rt.engine, cell, rt.release_specs, view, org_id=uow.org_id)


async def domain_out(request: Request, uow: UnitOfWork, rolling: int | None = None) -> DomainOut:
    view = await _view(request, uow)
    if rolling is None:
        rolling = await _rolling(request, uow, view)
    return DomainOut(
        default_suffix=view.default_suffix,
        current_suffix=view.current_suffix,
        previous=[PreviousSuffixOut(suffix=p.suffix, until=p.until) for p in view.previous],
        delegation=None if view.delegation is None else delegation_out(view.delegation),
        apps_that_move=[AppMoveOut(slug=s, hosts=list(h)) for s, h in view.apps_that_move],
        ready_to_flip=view.next_flip is not None and rolling == 0,
        rolling=rolling,
    )


async def _change(uow: UnitOfWork) -> None:
    await require_admin(uow)
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)


async def agent_of(request: Request, org_id: str) -> CellDomains:
    cell = await cell_of(request, org_id)
    if cell is None or cell.domains is None:
        raise Refusal(ErrorCode.CELL_UNAVAILABLE, evidence={"port": "domains"})
    return cell.domains


@router.get("/domain", response_model=DomainOut, responses=problem_responses(*_READ))
async def get_domain(request: Request, uow: UserUoW) -> DomainOut:
    """The org's suffixes, its customer domain's delegation, and whether a flip may run now.
    Active org admins only."""
    await require_admin(uow)
    return await domain_out(request, uow)


@router.put(
    "/domain",
    response_model=DomainOut,
    responses=problem_responses(*_CHANGE, *_ADMISSION, ErrorCode.ALREADY_EXISTS),
)
async def request_domain(request: Request, body: DomainIn, uow: UserUoW) -> DomainOut:
    """Ask for ``suffix`` as the org's customer domain: the cell makes its zone and
    certificate, and the answer names the NS records to add at the parent. A name the admission
    rules refuse answers its own ``DOMAIN_SUFFIX_*`` code. The same suffix again changes nothing;
    another while one is live is ``ALREADY_EXISTS``, unless that one failed, which it replaces.
    Audited as ``domain.requested``."""
    await _change(uow)
    rt = runtime_of(request)
    refused = await admit(uow.conn, uow.org_id, body.suffix, apps_domain=rt.settings.apps_domain)
    if refused is not None:
        raise Refusal(refused, evidence={"suffix": body.suffix})
    live = await domain_of(uow.conn, uow.org_id)
    if live is not None and live.state != "failed":
        if live.suffix == body.suffix:
            return await domain_out(request, uow)
        raise Refusal(ErrorCode.ALREADY_EXISTS, evidence={"domain_id": live.id})
    agent = await agent_of(request, uow.org_id)
    provider = await parent_provider(rt.resolvers, body.suffix)
    try:
        row = await _fresh_row(uow, agent, live, body.suffix, provider)
        made = await agent.create(body.suffix)
    except CellDomainsError as exc:
        log.warning("domain request failed at the cell", extra={"error": str(exc)})
        raise Refusal(ErrorCode.CELL_UNAVAILABLE, evidence={"port": "domains"}) from None
    row = await update_state(
        uow.conn,
        org_id=uow.org_id,
        domain_id=row.id,
        state="pending_dns",
        name_servers=made.name_servers,
        certificate=made.certificate,
    )
    after: dict[str, object] = {
        "suffix": row.suffix,
        "state": row.state,
        "zone_name": row.zone_name,
        "name_servers": list(row.name_servers),
    }
    if provider is not None:
        after["provider"] = provider
    await uow.audit(
        AuditAction.DOMAIN_REQUESTED, target_kind="org_domain", target_id=row.id, after=after
    )
    return await domain_out(request, uow)


async def _fresh_row(
    uow: UnitOfWork,
    agent: CellDomains,
    failed: OrgDomain | None,
    suffix: str,
    provider: str | None,
) -> OrgDomain:
    if failed is None:
        return await _insert(uow, suffix, provider)
    try:
        await agent.retire(failed.suffix)
    except CellDomainNotFoundError:
        pass
    if failed.suffix == suffix:
        try:
            async with uow.conn.begin_nested():
                return await restart_domain(uow.conn, uow.org_id, failed.id, provider=provider)
        except IntegrityError:
            raise Refusal(ErrorCode.ALREADY_EXISTS, evidence={"suffix": suffix}) from None
    await update_state(uow.conn, org_id=uow.org_id, domain_id=failed.id, state="retired")
    await uow.audit(
        AuditAction.DOMAIN_RETIRED,
        target_kind="org_domain",
        target_id=failed.id,
        before={"suffix": failed.suffix, "state": failed.state},
        after={"suffix": failed.suffix, "state": "retired"},
    )
    return await _insert(uow, suffix, provider)


async def _insert(uow: UnitOfWork, suffix: str, provider: str | None) -> OrgDomain:
    try:
        async with uow.conn.begin_nested():
            return await insert_domain(
                uow.conn,
                org_id=uow.org_id,
                suffix=suffix,
                zone_name=resource_name(suffix),
                provider=provider,
            )
    except IntegrityError:
        raise Refusal(ErrorCode.ALREADY_EXISTS, evidence={"suffix": suffix}) from None


@router.post(
    "/domain/flip",
    response_model=DomainOut,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.FORBIDDEN,
        ErrorCode.AGENT_SESSION_REFUSED,
        ErrorCode.CELL_UNAVAILABLE,
        ErrorCode.DOMAIN_NOT_READY,
        ErrorCode.DOMAIN_STATE,
    ),
)
async def flip_domain(request: Request, body: FlipIn, uow: UserUoW) -> Response:
    """Move every app of the org to its customer suffix (``to: customer``) or back to the
    default one, once every environment has rolled with the coming suffix among its origins
    (``DOMAIN_NOT_READY`` until then). The suffix left stays a previous one for 30 days: its
    hosts redirect. One snapshot carries the change. Audited as ``domain.flipped``."""
    await _change(uow)
    view = await _view(request, uow)
    counted_for = view.coming_suffix if view.next_flip == body.to else None
    rolling = await _rolling(request, uow, view) if counted_for is not None else 0
    try:
        await flip(
            uow.conn,
            uow.org_id,
            body.to,
            actor_of(uow.principal),
            datetime.now(UTC),
            apps_domain=runtime_of(request).settings.apps_domain,
            rolling=rolling,
            counted_for=counted_for,
        )
    except FlipRefusedError as exc:
        code = ErrorCode.DOMAIN_NOT_READY if exc.reason == "not_ready" else ErrorCode.DOMAIN_STATE
        raise Refusal(code, evidence={"reason": str(exc), "rolling": exc.rolling}) from None
    return uow.reply(await domain_out(request, uow, await live_count(uow.conn, uow.org_id)))
