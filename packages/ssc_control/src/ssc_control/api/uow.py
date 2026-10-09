"""One transaction per request, bound to the credential's org, holding the audit row too.

The dependency is declared with ``scope="function"`` so its exit (commit or rollback) runs when
the endpoint returns and **before** the response is sent. FastAPI's default request scope would
send the response first and commit afterwards, which is how a client learns of a change that
then rolls back.

Endpoints answer through :meth:`UnitOfWork.reply`, which renders the body once in canonical JSON
and remembers it. The idempotency dependency stores exactly that rendering, so a replay is the
same bytes the first caller received.

A ``preview``-scoped credential never touches production (:func:`check_scope`), checked here so
that every route, and every route added later, is covered before its handler runs. So is a
credential's auth-host session (:func:`check_session`): once it is revoked, or its person is
deactivated, every request with it is ``UNAUTHENTICATED``.
"""

import json
import re
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, cast

from fastapi import Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.audit import ActorKind, AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_control.api.auth import CredentialScope, Principal, internal_principal, user_principal
from ssc_control.api.problems import Refusal, request_id_of
from ssc_control.api.ratelimit import limit
from ssc_control.api.runtime import runtime_of
from ssc_control.audit import Actor, AppendedEvent, NewEvent, append_event
from ssc_control.db.bind import bound_org
from ssc_control.domain.approval_rules import RequirementKind
from ssc_control.identity.sessions import live_session
from ssc_control.ports import MetricsPort, NullMetricsPort

JSON_MEDIA_TYPE = "application/json"
PREVIEW_SCOPE_CHANGES: Final = (
    "POST /v1/apps/{app_id}/bundles",
    "POST /v1/apps/{app_id}/bundles/{bundle_id}/complete",
)
"""The changes naming no environment that a ``preview``-scoped credential may make."""
PREVIEW_SHARE_ASK: Final = "POST /v1/approvals"
"""The one change naming its environment in the body: a ``preview``-scoped credential may ask
for a share of an environment that is not prod, and nothing else."""
_SHARE_KINDS: Final = frozenset(
    {RequirementKind.AGENT_SHARE, RequirementKind.WIDEN_AUDIENCE, RequirementKind.EXCEED_CEILING}
)
_PREVIEW_SCOPE_CHANGES: Final = tuple(
    re.compile(re.sub(r"\{[a-z_]+\}", "[^/]+", change)) for change in PREVIEW_SCOPE_CHANGES
)
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_ENVIRONMENT_NAME: Final = text(
    "select name from ssc.environment where org_id = :org and id = :environment"
)


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
    metrics: MetricsPort = field(default_factory=NullMetricsPort)
    """Records metrics events in this transaction (``ssc_control.metrics``)."""

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


async def check_scope(request: Request, conn: AsyncConnection, principal: Principal) -> None:
    """A ``preview``-scoped credential never touches production: a request naming an environment,
    in its path or as a share ask (:data:`PREVIEW_SHARE_ASK`), must name one that is not prod, and
    any other change must be in :data:`PREVIEW_SCOPE_CHANGES`. Anything else is ``FORBIDDEN``. A
    route that names an environment changes only that one."""
    if principal.scope is not CredentialScope.PREVIEW:
        return
    change = f"{request.method} {request.url.path}"
    environment_id = request.path_params.get("environment_id")
    if environment_id is None and change == PREVIEW_SHARE_ASK:
        environment_id = await _share_environment(request)
    if environment_id is not None:
        name = (
            await conn.execute(
                _ENVIRONMENT_NAME, {"org": principal.org_id, "environment": environment_id}
            )
        ).scalar_one_or_none()
        if name == "prod":
            raise Refusal(
                ErrorCode.FORBIDDEN,
                evidence={"reason": "preview_scope", "environment_id": environment_id},
            )
        return
    if request.method not in _SAFE_METHODS and not any(
        p.fullmatch(change) for p in _PREVIEW_SCOPE_CHANGES
    ):
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"reason": "preview_scope", "change": change})


async def check_session(conn: AsyncConnection, principal: Principal) -> None:
    """A credential issued from an auth-host session is good only while that session is live
    and belongs to the subject. A CI session's credential is ``preview``-scoped (GA-7.7)."""
    if principal.session_id is None:
        return
    live = await live_session(conn, principal.org_id, principal.session_id)
    if live is None or live.user_id != principal.subject:
        raise Refusal(ErrorCode.UNAUTHENTICATED, evidence={"reason": "session_not_live"})
    if live.kind == "ci" and principal.scope is not CredentialScope.PREVIEW:
        raise Refusal(ErrorCode.UNAUTHENTICATED, evidence={"reason": "ci_session_unscoped"})


async def _share_environment(request: Request) -> str | None:
    """The environment a share ask names, or None when the body asks for anything else."""
    try:
        body: object = await request.json()
    except ValueError, RecursionError:
        return None
    if not isinstance(body, dict):
        return None
    fields = cast("dict[str, object]", body)
    kind, environment_id = fields.get("kind"), fields.get("environment_id")
    if not isinstance(kind, str) or kind not in _SHARE_KINDS:
        return None
    return environment_id if isinstance(environment_id, str) else None


def _make(
    principal_dep: Callable[[Request], Principal],
) -> Callable[[Request, Principal], AsyncIterator[UnitOfWork]]:
    async def unit_of_work(
        request: Request, principal: Annotated[Principal, Depends(principal_dep)]
    ) -> AsyncIterator[UnitOfWork]:
        limit(request, principal)
        rt = runtime_of(request)
        async with bound_org(rt.engine, principal.org_id) as conn:
            await check_session(conn, principal)
            await check_scope(request, conn, principal)
            yield UnitOfWork(
                conn=conn,
                principal=principal,
                request_id=request_id_of(request),
                metrics=rt.metrics,
            )

    return unit_of_work


user_unit_of_work = _make(user_principal)
internal_unit_of_work = _make(internal_principal)

UserUoW = Annotated[UnitOfWork, Depends(user_unit_of_work, scope="function")]
InternalUoW = Annotated[UnitOfWork, Depends(internal_unit_of_work, scope="function")]
