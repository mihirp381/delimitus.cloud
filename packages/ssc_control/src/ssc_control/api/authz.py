"""Who may do what, checked inside the request's unit of work.

SSC-021 adds the per-app roles; this module starts with the org-admin check the audit log needs.
"""

from sqlalchemy import text

from ssc_contracts.errors import ErrorCode
from ssc_control.api.auth import PrincipalKind
from ssc_control.api.problems import Refusal
from ssc_control.api.uow import UnitOfWork

_SELECT_ACCOUNT = text("select role, status from ssc.user_account where org_id = :org and id = :id")


async def require_admin(uow: UnitOfWork) -> str:
    """The caller's user id when they are an active admin of the org; ``FORBIDDEN`` otherwise."""
    principal = uow.principal
    if principal.kind is not PrincipalKind.USER:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"kind": principal.kind.value})
    row = (
        await uow.conn.execute(_SELECT_ACCOUNT, {"org": uow.org_id, "id": principal.subject})
    ).first()
    if row is None or row[0] != "admin" or row[1] != "active":
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"reason": "not_an_active_admin"})
    return principal.subject
