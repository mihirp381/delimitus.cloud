"""``GET /v1/whoami``."""

from fastapi import APIRouter, Request

from ssc_control.api.auth import PrincipalKind, UserPrincipal
from ssc_control.api.ratelimit import limit
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Strict

router = APIRouter()


class Whoami(Strict):
    org_id: str
    subject: str
    kind: PrincipalKind
    credential_id: str
    is_agent: bool
    client_id: str | None


@router.get("/whoami", response_model=Whoami, responses=problem_responses(*AUTHENTICATED))
def whoami(request: Request, principal: UserPrincipal) -> Whoami:
    limit(request, principal)
    return Whoami(
        org_id=principal.org_id,
        subject=principal.subject,
        kind=principal.kind,
        credential_id=principal.credential_id,
        is_agent=principal.is_agent,
        client_id=principal.client_id,
    )
