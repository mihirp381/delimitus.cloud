"""``GET .../access``: why a user can or cannot reach an app environment (SSC-021).

The answer comes from the one evaluator (``ssc_shared.access.decide``) run on a live compile of
the org's snapshot inputs, so it is what the gateway will decide once the next version is
published. The org's newest published version is returned beside it.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import Field
from sqlalchemy import text

from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_builder
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.uow import UserUoW
from ssc_control.snapshot.compiler import compile_document
from ssc_shared.access import AccessView, Reason, decide

router = APIRouter()


class ExplainedGrant(Strict):
    grant_id: str
    role: Literal["builder", "user"]
    subject_kind: Literal["user", "group", "org"]
    subject_id: str | None
    group_name: str | None = Field(description="The group's cached name, for a group grant.")


class AccessExplained(Strict):
    user_id: str
    environment_id: str
    allowed: bool
    role: Literal["builder", "user"] | None = Field(description="The best role granted.")
    floor: Literal["builder", "user"] = Field(description="The least role this environment takes.")
    reason: Reason
    grants: list[ExplainedGrant] = Field(
        description="The grants that decided it: those that count when allowed, those below "
        "the floor when refused for it, none otherwise."
    )
    evaluated_from: Literal["live"]
    published_version: int | None = Field(
        description="The org's newest published snapshot; null before the first."
    )


_SELECT_ENV = text(
    "select id from ssc.environment where org_id = :org and app_id = :app and id = :env"
)
_PUBLISHED = text("select max(version) from ssc.access_snapshot where org_id = :org")
_GROUP_NAMES = text(
    "select id, display_name from ssc.user_group "
    "where org_id = :org and id = any(cast(:ids as text[]))"
)


@router.get(
    "/apps/{app_id}/environments/{environment_id}/access",
    response_model=AccessExplained,
    responses=problem_responses(
        *AUTHENTICATED, ErrorCode.NOT_FOUND, ErrorCode.FORBIDDEN, ErrorCode.REFERENCE_NOT_FOUND
    ),
)
async def explain_access(
    app_id: Id,
    environment_id: Id,
    uow: UserUoW,
    user_id: Annotated[
        str | None,
        Query(pattern=r"^usr_[a-z0-9]{20}$", description="Whose access; the caller by default."),
    ] = None,
) -> AccessExplained:
    """Builders of the environment only (``FORBIDDEN``); ``REFERENCE_NOT_FOUND`` for a user
    who is not in the org."""
    params = {"org": uow.org_id, "app": app_id, "env": environment_id}
    if (await uow.conn.execute(_SELECT_ENV, params)).first() is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    caller = await require_builder(uow, environment_id)
    who = user_id or caller
    doc = await compile_document(uow.conn, uow.org_id, version=0, compiled_at=datetime.now(UTC))
    if who not in doc.users:
        raise Refusal(ErrorCode.REFERENCE_NOT_FOUND, evidence={"user_id": who})
    decision = decide(AccessView(doc), environment_id, who)
    group_ids = sorted(
        {g.subject_id for g in decision.via if g.subject_kind == "group" and g.subject_id}
    )
    names: dict[str, str] = {}
    if group_ids:
        rows = await uow.conn.execute(_GROUP_NAMES, {"org": uow.org_id, "ids": group_ids})
        names = {str(i): str(n) for i, n in rows}
    published = (await uow.conn.execute(_PUBLISHED, {"org": uow.org_id})).scalar_one()
    return AccessExplained(
        user_id=who,
        environment_id=environment_id,
        allowed=decision.allowed,
        role=decision.role,
        floor=doc.environments[environment_id].floor,
        reason=decision.reason,
        grants=[
            ExplainedGrant(
                grant_id=g.grant_id,
                role=g.role,
                subject_kind=g.subject_kind,
                subject_id=g.subject_id,
                group_name=names.get(g.subject_id) if g.subject_id else None,
            )
            for g in decision.via
        ],
        evaluated_from="live",
        published_version=None if published is None else int(published),
    )
