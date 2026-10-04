"""``POST /v1/apps/{app_id}/promote``: build for prod what preview runs now (SSC-042).

Promote is the only way to make a prod release: it builds, for prod, the bundle that made the
release live in preview, and answers ``202`` with that build to poll. Putting the new release
live is the ordinary forward deploy to prod, behind the production gate, which also syncs the
app's timers. Nothing here changes the worker or the schema.

Prod is always rebuilt, never handed preview's image: the build keeps preview's source digest and
takes ``[build.public_env.prod]``, and gets an image of its own even when the two inputs match.
Secrets are never copied. Promote refuses with ``PROD_SECRET_MISSING`` while preview has a secret
prod lacks, before anything is built: the same code reads the same names, so prod would otherwise
run a configuration preview never ran. The database secrets are not counted; prod's own database
is made on its first deploy.

An app connected to a GitHub repository with required checks (SSC-047) promotes only a commit on
which each of them is green, bound to its workflow file and the connected branch:
``REQUIRED_CHECKS_FAILING`` otherwise, and ``GITHUB_UNAVAILABLE`` when GitHub cannot say, both
before anything is built. Such an app promotes only a release built from a bundle the push job
made for that link, since an uploaded bundle's commit is only what the client declared.
"""

from typing import Final

from fastapi import APIRouter, Request, Response
from pydantic import Field
from sqlalchemy import text

from ssc_contracts import app_database
from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_builder
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.routes.v1.deployments import BuildAccepted, start_build
from ssc_control.api.routes.v1.github import require_green_checks
from ssc_control.api.uow import UserUoW

router = APIRouter()

PROD: Final = "prod"


class PromoteIn(Strict):
    preview_release_id: str | None = Field(
        default=None,
        pattern=r"^rel_[a-z0-9]{20}$",
        description="The release the caller saw live in preview. `PRECONDITION_STALE` when "
        "preview now runs another.",
    )


_SELECT_APP = text("select status from ssc.app where org_id = :org and id = :app")
# Locked so two promotes of one app queue up and the second sees the first's build in flight.
_LOCK_PROD = text(
    "select id from ssc.environment where org_id = :org and app_id = :app and name = 'prod' "
    "for update"
)
_SELECT_PREVIEW_LIVE = text(
    "select d.state, d.release_id, r.source_digest, r.source_commit from ssc.environment e "
    "join ssc.deployment d on d.org_id = e.org_id and d.id = e.current_deployment_id "
    "join ssc.release r on r.org_id = d.org_id and r.id = d.release_id "
    "where e.org_id = :org and e.app_id = :app and e.name = 'preview'"
)
_SELECT_IN_FLIGHT = text(
    "select exists (select 1 from ssc.deployment where org_id = :org and environment_id = :env "
    "and state in ('pending', 'running')), "
    "exists (select 1 from ssc.build where org_id = :org and environment_id = :env "
    "and state in ('queued', 'running'))"
)
_SELECT_MISSING_SECRETS = text(
    "select s.name from ssc.secret_ref s "
    "join ssc.environment e on e.org_id = s.org_id and e.id = s.environment_id "
    "where e.org_id = :org and e.app_id = :app and e.name = 'preview' "
    "and s.name <> all(:platform) and not exists (select 1 from ssc.secret_ref p "
    "where p.org_id = s.org_id and p.environment_id = :env and p.name = s.name) "
    "order by s.name"
)
_SELECT_SOURCE_BUNDLE = text(
    "select b.id, b.actor_kind, b.actor_id from ssc.bundle b where b.org_id = :org "
    "and b.id = coalesce("
    "(select bundle_id from ssc.build where org_id = :org and release_id = :rel), "
    "(select id from ssc.bundle where org_id = :org and app_id = :app and digest = :digest))"
)


@router.post(
    "/apps/{app_id}/promote",
    status_code=202,
    response_model=BuildAccepted,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.APP_NOT_ACTIVE,
        ErrorCode.NOTHING_TO_PROMOTE,
        ErrorCode.DEPLOYMENT_IN_FLIGHT,
        ErrorCode.BUILD_IN_FLIGHT,
        ErrorCode.PROD_SECRET_MISSING,
        ErrorCode.BUNDLE_NOT_UPLOADED,
        ErrorCode.REFERENCE_NOT_FOUND,
        ErrorCode.PRECONDITION_STALE,
        ErrorCode.REQUIRED_CHECKS_FAILING,
        ErrorCode.GITHUB_UNAVAILABLE,
    ),
)
async def promote(app_id: Id, body: PromoteIn, request: Request, uow: UserUoW) -> Response:
    """Build for prod the source of the release live in preview: 202 plus a ``Location`` to
    poll. Deploy the release it makes to prod with ``POST .../deployments``.

    Needs a builder on prod; a ``preview``-scoped credential is ``FORBIDDEN``. Preview must run a
    healthy deployment (``NOTHING_TO_PROMOTE``), the one named by ``preview_release_id`` when
    given (``PRECONDITION_STALE``), prod must have no deployment or build in flight, and every
    secret set on preview must be set on prod too (``PROD_SECRET_MISSING``). A connected
    repository's required checks must be green on the release's commit
    (``REQUIRED_CHECKS_FAILING``)."""
    params = {"org": uow.org_id, "app": app_id}
    status = (await uow.conn.execute(_SELECT_APP, params)).scalar_one_or_none()
    prod = (await uow.conn.execute(_LOCK_PROD, params)).scalar_one_or_none()
    if status is None or prod is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"app_id": app_id})
    await require_builder(uow, prod)
    if status != "active":
        raise Refusal(ErrorCode.APP_NOT_ACTIVE, evidence={"app_id": app_id})
    live = (await uow.conn.execute(_SELECT_PREVIEW_LIVE, params)).mappings().first()
    if live is None or live["state"] != "healthy":
        raise Refusal(ErrorCode.NOTHING_TO_PROMOTE, evidence={"app_id": app_id})
    release_id = str(live["release_id"])
    if body.preview_release_id is not None and body.preview_release_id != release_id:
        raise Refusal(
            ErrorCode.PRECONDITION_STALE,
            evidence={"preview_release_id": body.preview_release_id, "live": release_id},
        )
    deploying, building = (
        await uow.conn.execute(_SELECT_IN_FLIGHT, {"org": uow.org_id, "env": prod})
    ).one()
    if deploying:
        raise Refusal(ErrorCode.DEPLOYMENT_IN_FLIGHT, evidence={"environment_id": prod})
    if building:
        raise Refusal(ErrorCode.BUILD_IN_FLIGHT, evidence={"environment_id": prod})
    missing = (
        (
            await uow.conn.execute(
                _SELECT_MISSING_SECRETS,
                {**params, "env": prod, "platform": list(app_database.SECRETS)},
            )
        )
        .scalars()
        .all()
    )
    if missing:
        raise Refusal(
            ErrorCode.PROD_SECRET_MISSING, evidence={"environment_id": prod, "names": missing}
        )
    bundle = (
        (
            await uow.conn.execute(
                _SELECT_SOURCE_BUNDLE,
                {**params, "rel": release_id, "digest": live["source_digest"]},
            )
        )
        .mappings()
        .first()
    )
    uploader = None if bundle is None else (bundle["actor_kind"], bundle["actor_id"])
    await require_green_checks(uow, request, app_id, live["source_commit"], uploader)
    if bundle is None:
        raise Refusal(ErrorCode.REFERENCE_NOT_FOUND, evidence={"release_id": release_id})
    accepted = await start_build(
        uow,
        app_id,
        prod,
        str(bundle["id"]),
        audit_extra={"via": "promote", "source_release_id": release_id},
    )
    return uow.reply(accepted, status=202, headers={"Location": f"/v1/builds/{accepted.build_id}"})
