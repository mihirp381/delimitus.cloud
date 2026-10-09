"""CI tokens (GA-7.7, decision 011): list and revoke them. The auth host creates them
(``POST /ci-tokens`` there, ``ssc token create-ci``); this API never sees or returns a token.

A CI token is the one access token of a ``ci`` session, so revoking the session ends the token on
its next call (``uow.check_session``)."""

from datetime import datetime
from typing import Final

from fastapi import APIRouter, Response
from sqlalchemy import text

from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import active_role
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.identity.sessions import revoke_session

router = APIRouter()

MAX_LISTED: Final = 200


class CiToken(Strict):
    id: str
    user_id: str
    label: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class CiTokens(Strict):
    ci_tokens: list[CiToken]


_LIST = text(
    "select id, user_id, label, created_at, expires_at, revoked_at from ssc.auth_session "
    "where org_id = :org and kind = 'ci' and (:everyone or user_id = :user) "
    "order by created_at desc, id limit :limit"
)
_ONE = text(
    "select id, user_id, label, created_at, expires_at, revoked_at from ssc.auth_session "
    "where org_id = :org and kind = 'ci' and id = :id for update"
)


async def _is_admin(uow: UnitOfWork) -> bool:
    return await active_role(uow) == "admin"


@router.get(
    "/ci-tokens",
    response_model=CiTokens,
    responses=problem_responses(*AUTHENTICATED),
)
async def list_ci_tokens(uow: UserUoW) -> CiTokens:
    """The caller's CI tokens, or every one in the org for an active org admin, revoked and
    expired ones included, newest first, at most 200. Never a token itself."""
    params = {
        "org": uow.org_id,
        "user": uow.principal.subject,
        "everyone": await _is_admin(uow),
        "limit": MAX_LISTED,
    }
    rows = (await uow.conn.execute(_LIST, params)).mappings()
    return CiTokens(ci_tokens=[CiToken(**dict(r)) for r in rows])


@router.delete(
    "/ci-tokens/{ci_token_id}",
    response_model=CiToken,
    responses=problem_responses(
        *AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.AGENT_SESSION_REFUSED, ErrorCode.NOT_FOUND
    ),
)
async def revoke_ci_token(ci_token_id: Id, uow: UserUoW) -> Response:
    """Revoke a CI token: its next call is ``UNAUTHENTICATED``. Its owner or an active org admin,
    never in an agent session and never with a ``preview``-scoped credential (``FORBIDDEN``);
    ``NOT_FOUND`` for anyone else's. A token already revoked is answered as it is. Audited as
    ``token.revoked``."""
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    params = {"org": uow.org_id, "id": ci_token_id}
    row = (await uow.conn.execute(_ONE, params)).mappings().one_or_none()
    if row is None or (row["user_id"] != uow.principal.subject and not await _is_admin(uow)):
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"ci_token_id": ci_token_id})
    if row["revoked_at"] is None:
        await revoke_session(
            uow.conn, uow.org_id, ci_token_id, "revoked", actor=actor_of(uow.principal)
        )
        row = (await uow.conn.execute(_ONE, params)).mappings().one()
    return uow.reply(CiToken(**dict(row)))
