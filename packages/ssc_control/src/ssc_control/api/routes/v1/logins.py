"""Logins the auth host could not tie to a person (SSC-019, decision 024): an org admin lists
them and links one to an active person. An address-shaped subject (Google SAML) is never
linkable; the fix for those is the directory."""

from datetime import datetime
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Response
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_admin
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.uow import UserUoW, actor_of
from ssc_control.identity.join import LinkError, link_unlinked
from ssc_control.identity.rules import subject_problem

router = APIRouter()

MAX_LISTED: Final = 200


class UnlinkedLogin(Strict):
    id: str
    connection_id: str
    subject: str
    email: str = Field(description="As the identity provider sent it; display only.")
    reason: Literal["no_match", "ambiguous_email"]
    attempts: int
    first_seen_at: datetime
    last_seen_at: datetime
    linkable: bool = Field(description="False for an address-shaped subject.")


class UnlinkedLogins(Strict):
    unlinked_logins: list[UnlinkedLogin]


class LinkIn(Strict):
    user_id: Annotated[str, Field(pattern=r"^usr_[a-z0-9]{20}$")]


class Linked(Strict):
    identity_link_id: str
    user_id: str


_PENDING = text(
    "select id, connection_id, subject, email, reason, attempts, first_seen_at, last_seen_at "
    "from ssc.unlinked_login where org_id = :org and linked_user_id is null "
    "order by last_seen_at desc, id limit :limit"
)
_REFUSALS: Final = {
    "not_found": ErrorCode.NOT_FOUND,
    "subject_not_keyable": ErrorCode.VALIDATION_FAILED,
    "user_not_active": ErrorCode.REFERENCE_NOT_FOUND,
}


@router.get(
    "/unlinked-logins",
    response_model=UnlinkedLogins,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def list_unlinked_logins(uow: UserUoW) -> UnlinkedLogins:
    """Active org admins only (``FORBIDDEN``). The most recent first, at most 200."""
    await require_admin(uow)
    rows = (await uow.conn.execute(_PENDING, {"org": uow.org_id, "limit": MAX_LISTED})).mappings()
    return UnlinkedLogins(
        unlinked_logins=[
            UnlinkedLogin(**dict(r), linkable=subject_problem(str(r["subject"])) is None)
            for r in rows
        ]
    )


@router.post(
    "/unlinked-logins/{unlinked_login_id}/link",
    response_model=Linked,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.FORBIDDEN,
        ErrorCode.AGENT_SESSION_REFUSED,
        ErrorCode.NOT_FOUND,
        ErrorCode.REFERENCE_NOT_FOUND,
    ),
)
async def link_login(unlinked_login_id: Id, body: LinkIn, uow: UserUoW) -> Response:
    """Tie the login to an active person; their next login with it signs them in. Active org
    admins only, never in an agent session. ``NOT_FOUND`` when it is gone or already linked,
    ``VALIDATION_FAILED`` for an address-shaped subject, ``REFERENCE_NOT_FOUND`` when the person
    is not active."""
    await require_admin(uow)
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    try:
        link_id = await link_unlinked(
            uow.conn, uow.org_id, unlinked_login_id, body.user_id, actor=actor_of(uow.principal)
        )
    except LinkError as e:
        reason = str(e)
        raise Refusal(
            _REFUSALS[reason], evidence={"unlinked_login_id": unlinked_login_id, "reason": reason}
        ) from None
    return uow.reply(Linked(identity_link_id=link_id, user_id=body.user_id))
