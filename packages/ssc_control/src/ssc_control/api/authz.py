"""Who may do what, checked inside the request's unit of work.

SSC-021 adds the per-app roles; this module starts with the org-admin check the audit log needs
and the builder check approval requests need.
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


# Active, and an org admin, the owner of the environment's app, or a builder on the environment
# directly, through a group, or through an org-wide builder grant.
_SELECT_BUILDER = text(
    "select 1 from ssc.user_account u "
    "join ssc.environment e on e.org_id = u.org_id and e.id = :env "
    "join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "where u.org_id = :org and u.id = :id and u.status = 'active' and ("
    "u.role = 'admin' or a.owner_user_id = u.id or exists ("
    "select 1 from ssc.app_grant g where g.org_id = e.org_id and g.environment_id = e.id "
    "and g.role = 'builder' and (g.subject_kind = 'org' "
    "or (g.subject_kind = 'user' and g.user_id = u.id) "
    "or (g.subject_kind = 'group' and exists (select 1 from ssc.group_member m "
    "where m.org_id = g.org_id and m.group_id = g.group_id and m.user_id = u.id)))))"
)


async def require_builder(uow: UnitOfWork, environment_id: str) -> str:
    """The caller's user id when they may change ``environment_id``: an active org admin, the
    app's owner, or a builder there. ``FORBIDDEN`` otherwise, including for a missing environment;
    callers that must say ``NOT_FOUND`` look the environment up first."""
    principal = uow.principal
    if principal.kind is not PrincipalKind.USER:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"kind": principal.kind.value})
    params = {"org": uow.org_id, "id": principal.subject, "env": environment_id}
    if (await uow.conn.execute(_SELECT_BUILDER, params)).first() is None:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"reason": "not_a_builder"})
    return principal.subject


# Active, and an org admin, the app's owner, or a builder (direct, group or org-wide) on any of the
# app's environments.
_SELECT_APP_BUILDER = text(
    "select 1 from ssc.user_account u "
    "join ssc.app a on a.org_id = u.org_id and a.id = :app "
    "where u.org_id = :org and u.id = :id and u.status = 'active' and ("
    "u.role = 'admin' or a.owner_user_id = u.id or exists ("
    "select 1 from ssc.app_grant g join ssc.environment e "
    "on e.org_id = g.org_id and e.id = g.environment_id "
    "where g.org_id = a.org_id and e.app_id = a.id and g.role = 'builder' and ("
    "g.subject_kind = 'org' "
    "or (g.subject_kind = 'user' and g.user_id = u.id) "
    "or (g.subject_kind = 'group' and exists (select 1 from ssc.group_member m "
    "where m.org_id = g.org_id and m.group_id = g.group_id and m.user_id = u.id)))))"
)


async def require_app_builder(uow: UnitOfWork, app_id: str) -> str:
    """The caller's user id when they may ship source to ``app_id``: an active org admin, the
    app's owner, or a builder on any of its environments. ``FORBIDDEN`` otherwise, including for
    a missing app; callers that must say ``NOT_FOUND`` look the app up first."""
    principal = uow.principal
    if principal.kind is not PrincipalKind.USER:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"kind": principal.kind.value})
    params = {"org": uow.org_id, "id": principal.subject, "app": app_id}
    if (await uow.conn.execute(_SELECT_APP_BUILDER, params)).first() is None:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"reason": "not_a_builder"})
    return principal.subject
