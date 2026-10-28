"""``PUT .../environments/{environment_id}/form-targets`` (CD-5.2): the origins an
environment's pages may post forms to beside their own. Active org admins only, never in an
agent session."""

from typing import Annotated, Final

from fastapi import APIRouter
from pydantic import AfterValidator, Field
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.snapshot import MAX_FORM_TARGETS, check_form_origin
from ssc_control.api.authz import require_admin
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.uow import UserUoW
from ssc_control.snapshot.service import mark_dirty

router = APIRouter()

FormOrigin = Annotated[
    str,
    Field(
        max_length=261,
        description="`https://` and a lower-case host name, no port or path.",
    ),
    AfterValidator(check_form_origin),
]

_ENVIRONMENT: Final = text(
    "select e.id from ssc.environment e join ssc.app a on a.org_id = e.org_id "
    "and a.id = e.app_id where e.org_id = :org and e.app_id = :app and e.id = :env "
    "and a.status <> 'deleted' for update of e"
)
_LISTED: Final = text(
    "select origin from ssc.form_target where org_id = :org and environment_id = :env "
    'order by origin collate "C"'
)
_CLEAR: Final = text("delete from ssc.form_target where org_id = :org and environment_id = :env")
_ADD: Final = text(
    "insert into ssc.form_target (org_id, environment_id, origin) values (:org, :env, :origin)"
)


class FormTargetsIn(Strict):
    origins: list[FormOrigin] = Field(max_length=MAX_FORM_TARGETS)


class FormTargetsOut(Strict):
    environment_id: str
    origins: list[str] = Field(description="By byte order.")


@router.put(
    "/apps/{app_id}/environments/{environment_id}/form-targets",
    response_model=FormTargetsOut,
    responses=problem_responses(
        *AUTHENTICATED,
        ErrorCode.FORBIDDEN,
        ErrorCode.AGENT_SESSION_REFUSED,
        ErrorCode.NOT_FOUND,
    ),
)
async def put_form_targets(
    app_id: Id, environment_id: Id, body: FormTargetsIn, uow: UserUoW
) -> FormTargetsOut:
    """Replace the origins the environment's pages may post forms to beside their own; an
    empty list leaves only the app's own origin. The gateway sends them in `form-action` once
    the next snapshot is read. Audited as ``org.updated`` on ``environment``."""
    await require_admin(uow)
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    key = {"org": uow.org_id, "app": app_id, "env": environment_id}
    if (await uow.conn.execute(_ENVIRONMENT, key)).first() is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    before = list((await uow.conn.execute(_LISTED, key)).scalars())
    after = sorted(set(body.origins))
    if after != before:
        await uow.conn.execute(_CLEAR, key)
        for origin in after:
            await uow.conn.execute(_ADD, {**key, "origin": origin})
        await uow.audit(
            AuditAction.ORG_UPDATED,
            target_kind="environment",
            target_id=environment_id,
            before={"form_targets": before},
            after={"form_targets": after},
        )
        await mark_dirty(uow.conn, uow.org_id)
    return FormTargetsOut(environment_id=environment_id, origins=after)
