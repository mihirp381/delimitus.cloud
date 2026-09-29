"""One transaction per request, bound to the credential's org, holding the audit row too.

The dependency is declared with ``scope="function"`` so its exit (commit or rollback) runs when
the endpoint returns and **before** the response is sent. FastAPI's default request scope would
send the response first and commit afterwards, which is how a client learns of a change that
then rolls back.

Endpoints answer through :meth:`UnitOfWork.reply`, which renders the body once in canonical JSON
and remembers it. The idempotency dependency stores exactly that rendering, so a replay is the
same bytes the first caller received.
"""

import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_control.api.auth import Principal, internal_principal, user_principal
from ssc_control.api.problems import request_id_of
from ssc_control.api.ratelimit import limit
from ssc_control.api.runtime import runtime_of
from ssc_control.audit import Actor, AppendedEvent, NewEvent, append_event
from ssc_control.db.bind import bound_org

JSON_MEDIA_TYPE = "application/json"


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict[str, str])

    def to_response(self, extra_headers: Mapping[str, str] | None = None) -> Response:
        return Response(
            content=canonical_json(self.body),
            status_code=self.status,
            media_type=JSON_MEDIA_TYPE,
            headers={**self.headers, **(extra_headers or {})},
        )


def actor_of(principal: Principal) -> Actor:
    return Actor(
        kind=ActorKind(principal.kind.value),
        id=principal.subject,
        via_agent=principal.is_agent,
        client_id=principal.client_id,
    )


@dataclass(slots=True)
class UnitOfWork:
    conn: AsyncConnection
    principal: Principal
    request_id: str
    reply_sent: Reply | None = None

    @property
    def org_id(self) -> str:
        return self.principal.org_id

    def reply(
        self,
        body: BaseModel | Mapping[str, Any],
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        data = body.model_dump(mode="json") if isinstance(body, BaseModel) else dict(body)
        self.reply_sent = Reply(status=status, body=data, headers=dict(headers or {}))
        return self.reply_sent.to_response()

    async def audit(  # noqa: PLR0913  (keyword-only)
        self,
        action: AuditAction,
        *,
        target_kind: str,
        target_id: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        policy_decision_id: str | None = None,
    ) -> AppendedEvent:
        """Append to the org's chain in this transaction. ``before``/``after`` follow the views."""
        return await append_event(
            self.conn,
            NewEvent(
                org_id=self.org_id,
                action=action,
                actor=actor_of(self.principal),
                target_kind=target_kind,
                target_id=target_id,
                before=before,
                after=after,
                policy_decision_id=policy_decision_id,
            ),
        )


def _make(
    principal_dep: Callable[[Request], Principal],
) -> Callable[[Request, Principal], AsyncIterator[UnitOfWork]]:
    async def unit_of_work(
        request: Request, principal: Annotated[Principal, Depends(principal_dep)]
    ) -> AsyncIterator[UnitOfWork]:
        limit(request, principal)
        async with bound_org(runtime_of(request).engine, principal.org_id) as conn:
            yield UnitOfWork(conn=conn, principal=principal, request_id=request_id_of(request))

    return unit_of_work


user_unit_of_work = _make(user_principal)
internal_unit_of_work = _make(internal_principal)

UserUoW = Annotated[UnitOfWork, Depends(user_unit_of_work, scope="function")]
InternalUoW = Annotated[UnitOfWork, Depends(internal_unit_of_work, scope="function")]
