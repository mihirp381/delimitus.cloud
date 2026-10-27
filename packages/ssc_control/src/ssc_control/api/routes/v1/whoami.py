"""``GET /v1/whoami``."""

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy import text

from ssc_control.api.auth import PrincipalKind
from ssc_control.api.authz import OrgRole, active_role
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Strict
from ssc_control.api.uow import UserUoW
from ssc_control.names import people

router = APIRouter()

_ORG_NAME = text("select name from ssc.org where id = :org")


class Whoami(Strict):
    org_id: str
    subject: str
    kind: PrincipalKind
    credential_id: str
    is_agent: bool
    client_id: str | None
    role: OrgRole | None = Field(
        description="The caller's org role when the credential is an active user's; null for any "
        "other credential and for a deactivated user. Admin-only endpoints need `admin`."
    )
    org_name: str | None
    name: str | None = Field(description="The user's display name; null for other credentials.")
    email: str | None


@router.get("/whoami", response_model=Whoami, responses=problem_responses(*AUTHENTICATED))
async def whoami(uow: UserUoW) -> Whoami:
    principal = uow.principal
    org_name = (await uow.conn.execute(_ORG_NAME, {"org": principal.org_id})).scalar_one_or_none()
    who = None
    if principal.kind is PrincipalKind.USER:
        who = (await people(uow.conn, principal.org_id, [principal.subject])).of(principal.subject)
    return Whoami(
        org_id=principal.org_id,
        subject=principal.subject,
        kind=principal.kind,
        credential_id=principal.credential_id,
        is_agent=principal.is_agent,
        client_id=principal.client_id,
        role=await active_role(uow),
        org_name=org_name,
        name=None if who is None else who.name,
        email=None if who is None else who.email,
    )
