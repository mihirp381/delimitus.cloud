"""Who may do what, checked inside the request's unit of work.

SSC-021 adds the per-app roles; this module starts with the org-admin check the audit log needs
and the builder check approval requests need. ``active_role`` is the one answer to "is the caller
an admin": ``require_admin`` and ``GET /v1/whoami`` both use it. ``buildable_app_ids`` lists the
apps ``require_app_builder`` would allow, for ``GET /v1/apps?builder=me``.
"""

from typing import Literal

from sqlalchemy import text

from ssc_contracts.errors import ErrorCode
from ssc_control.api.auth import PrincipalKind
from ssc_control.api.problems import Refusal
from ssc_control.api.uow import UnitOfWork

type OrgRole = Literal["admin", "member"]

_ACTIVE_ROLE = text(
    "select role from ssc.user_account where org_id = :org and id = :id and status = 'active'"
)


async def active_role(uow: UnitOfWork) -> OrgRole | None:
    """The caller's org role when the credential is a user's and that user is active in the org;
    None for any other credential kind, a deactivated user or an unknown one."""
    principal = uow.principal
    if principal.kind is not PrincipalKind.USER:
        return None
    params = {"org": uow.org_id, "id": principal.subject}
    match (await uow.conn.execute(_ACTIVE_ROLE, params)).scalar_one_or_none():
        case "admin":
            return "admin"
        case "member":
            return "member"
        case _:
            return None


async def require_admin(uow: UnitOfWork) -> str:
    """The caller's user id when they are an active admin of the org; ``FORBIDDEN`` otherwise."""
    principal = uow.principal
    if principal.kind is not PrincipalKind.USER:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"kind": principal.kind.value})
    if await active_role(uow) != "admin":
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


# The rule of ``_SELECT_APP_BUILDER`` over every app of the org at once; a test keeps them equal.
_SELECT_BUILDABLE_APPS = text(
    "select a.id from ssc.app a "
    "join ssc.user_account u on u.org_id = a.org_id and u.id = :id "
    "where a.org_id = :org and u.status = 'active' and ("
    "u.role = 'admin' or a.owner_user_id = u.id or exists ("
    "select 1 from ssc.app_grant g join ssc.environment e "
    "on e.org_id = g.org_id and e.id = g.environment_id "
    "where g.org_id = a.org_id and e.app_id = a.id and g.role = 'builder' and ("
    "g.subject_kind = 'org' "
    "or (g.subject_kind = 'user' and g.user_id = u.id) "
    "or (g.subject_kind = 'group' and exists (select 1 from ssc.group_member m "
    "where m.org_id = g.org_id and m.group_id = g.group_id and m.user_id = u.id)))))"
)


async def buildable_app_ids(uow: UnitOfWork) -> frozenset[str]:
    """The apps the caller may ship source to (``require_app_builder``'s rule): every app for an
    active admin, none for a deactivated user. Any credential but a user's is ``FORBIDDEN``."""
    principal = uow.principal
    if principal.kind is not PrincipalKind.USER:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"kind": principal.kind.value})
    params = {"org": uow.org_id, "id": principal.subject}
    return frozenset(str(r[0]) for r in await uow.conn.execute(_SELECT_BUILDABLE_APPS, params))
