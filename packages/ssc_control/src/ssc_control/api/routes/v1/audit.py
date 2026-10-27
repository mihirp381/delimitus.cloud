"""The org's audit log (SSC-012): newest-first search and a streamed export. Org admins only."""

from datetime import datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime, Field

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.text import PRINTABLE
from ssc_control import names
from ssc_control.api.auth import UserPrincipal
from ssc_control.api.authz import require_admin
from ssc_control.api.problems import Refusal, request_id_of
from ssc_control.api.ratelimit import limit
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Strict
from ssc_control.api.runtime import runtime_of
from ssc_control.api.uow import UnitOfWork, UserUoW, check_session
from ssc_control.audit.export import MEDIA_TYPES, stream_export
from ssc_control.audit.search import AuditFilters, event_record, select_events
from ssc_control.db.bind import bound_org

router = APIRouter()

Ref = Annotated[str, Field(min_length=1, max_length=200, pattern=PRINTABLE)]
EXPORT_CONTENT: Final = {media: {"schema": {"type": "string"}} for media in MEDIA_TYPES.values()}


class AuditFilterQuery(Strict):
    since: AwareDatetime | None = Field(default=None, description="Inclusive. RFC 3339.")
    until: AwareDatetime | None = Field(default=None, description="Exclusive. RFC 3339.")
    action: AuditAction | None = None
    actor_kind: ActorKind | None = None
    actor_id: Ref | None = None
    target_kind: Ref | None = None
    target_id: Ref | None = None

    def filters(self) -> AuditFilters:
        return AuditFilters(
            since=self.since,
            until=self.until,
            action=self.action,
            actor_kind=self.actor_kind,
            actor_id=self.actor_id,
            target_kind=self.target_kind,
            target_id=self.target_id,
        )


class AuditSearchQuery(AuditFilterQuery):
    before_seq: int | None = Field(
        default=None, ge=1, description="Only events older than this: the previous page's cursor."
    )
    limit: int = Field(default=100, ge=1, le=500)


class AuditExportQuery(AuditFilterQuery):
    format: Literal["csv", "jsonl"]


class AuditActor(Strict):
    kind: ActorKind
    id: str
    via_agent: bool
    client_id: str | None
    name: str = Field(
        description="Who, in words: the person; `<client id> for <person>` in an agent session; "
        "`Delimitus operator`; `System` for a workload, a schedule or an integration."
    )
    email: str | None = Field(description="The person's email, while they are an active member.")


class AuditTarget(Strict):
    kind: str
    id: str
    name: str | None = Field(
        description="The app's slug, the person's, group's, connection's or org's name, or "
        "`<slug> <environment>`; null for other kinds."
    )


class AuditEvent(Strict):
    seq: int
    at: datetime
    action: AuditAction
    actor: AuditActor
    target: AuditTarget
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    policy_decision_id: str | None
    prev_hash: str = Field(description="Hex. The previous event's hash; zeros before seq 1.")
    hash: str = Field(description="Hex. `sha256(prev_hash || canonical)`.")


class AuditPage(Strict):
    events: list[AuditEvent]
    next_before_seq: int | None = Field(
        description="Pass as `before_seq` for the next, older page; null on the last page."
    )


_TARGETS: Final = {
    "app": names.apps,
    "user_group": names.groups,
    "connection": names.connections,
    "environment": names.environments,
    "org": names.orgs,
}


def _actor_name(actor: dict[str, Any], who: names.People) -> str:
    match actor["kind"]:
        case ActorKind.USER:
            person = who.of(actor["id"]).name
            if actor["via_agent"]:
                return f"{actor['client_id'] or 'An agent'} for {person}"
            return person
        case ActorKind.OPERATOR:
            return names.OPERATOR
        case _:
            return names.SYSTEM


async def _name(uow: UnitOfWork, records: list[dict[str, Any]]) -> None:
    users = [r["actor"]["id"] for r in records if r["actor"]["kind"] == ActorKind.USER]
    users += [r["target"]["id"] for r in records if r["target"]["kind"] == "user_account"]
    who = await names.people(uow.conn, uow.org_id, users)
    found: dict[str, dict[str, str]] = {}
    for kind, lookup in _TARGETS.items():
        ids = [r["target"]["id"] for r in records if r["target"]["kind"] == kind]
        found[kind] = await lookup(uow.conn, uow.org_id, ids) if ids else {}
    for r in records:
        actor, target = r["actor"], r["target"]
        actor["name"] = _actor_name(actor, who)
        actor["email"] = who.email(actor["id"]) if actor["kind"] == ActorKind.USER else None
        if target["kind"] == "user_account":
            target["name"] = who.of(target["id"]).name
        elif target["kind"] == "user_group":
            target["name"] = found["user_group"].get(target["id"], names.REMOVED_GROUP)
        else:
            target["name"] = found.get(target["kind"], {}).get(target["id"])


@router.get(
    "/audit",
    response_model=AuditPage,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def search_audit(params: Annotated[AuditSearchQuery, Query()], uow: UserUoW) -> AuditPage:
    """The org's audit events, newest first, in keyset pages. Org admins only."""
    await require_admin(uow)
    query = select_events(
        uow.org_id, params.filters(), before_seq=params.before_seq, limit=params.limit + 1
    )
    rows = (await uow.conn.execute(query)).mappings().all()
    records = [event_record(r) for r in rows[: params.limit]]
    await _name(uow, records)
    events = [AuditEvent.model_validate(r) for r in records]
    more = len(rows) > params.limit
    return AuditPage(events=events, next_before_seq=events[-1].seq if more else None)


@router.get(
    "/audit/export",
    response_class=StreamingResponse,
    responses={
        200: {"description": "The matching events, oldest first.", "content": EXPORT_CONTENT},
        **problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
    },
)
async def export_audit(
    params: Annotated[AuditExportQuery, Query()], request: Request, principal: UserPrincipal
) -> StreamingResponse:
    """Every matching event as a CSV or JSON-lines download, itself recorded as
    ``audit.exported``. Org admins in their own session only; agent credentials are refused."""
    limit(request, principal)
    if principal.is_agent:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"reason": "agent_session"})
    org_id, filters, engine = principal.org_id, params.filters(), runtime_of(request).engine
    async with bound_org(engine, org_id) as conn:
        await check_session(conn, principal)
        uow = UnitOfWork(conn=conn, principal=principal, request_id=request_id_of(request))
        await require_admin(uow)
        await uow.audit(
            AuditAction.AUDIT_EXPORTED,
            target_kind="audit",
            target_id=org_id,
            after={"format": params.format, "filters": filters.as_view()},
        )
    return StreamingResponse(
        stream_export(engine, org_id, filters, params.format),
        media_type=MEDIA_TYPES[params.format],
        headers={
            "Content-Disposition": f'attachment; filename="audit-{org_id}.{params.format}"',
            "Cache-Control": "no-store",
        },
    )
