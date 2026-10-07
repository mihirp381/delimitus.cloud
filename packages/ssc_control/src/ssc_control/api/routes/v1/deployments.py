"""Builds, releases and deployments (SSC-016, decision 014).

A build turns a stored bundle into a numbered release for one environment; a deployment puts a
release live in an environment. Both are long-running: the POST answers ``202`` with a
``Location`` to poll and defers the job in the same transaction. Starting either needs an org
admin, the app's owner or a builder on the environment. Releases are never changed or deleted.

A build for ``prod`` comes only from promote (``routes/v1/promote.py``), so every prod release is
built from source that ran in preview; this route refuses it with ``PROD_REQUIRES_PROMOTE``.

A ``prod`` deployment is checked against the production gate here, so the approvals it needs are
opened with the deployment; the job checks the gate again and never boots before it clears. A
rollback supersedes the environment's in-flight forward deploy and keeps the environment's
current config and sharing versions: it changes the running code, nothing else.

A rollback to a release that lacks migrations the environment's database may have run is
``SCHEMA_AHEAD`` unless the request says ``confirm`` (SSC-043): the problem carries fixed text, so
``GET .../migrations-ahead`` names the migrations. A confirmed one is audited with them. A
``prod`` deployment of an environment with a database shows the recovery point its job recorded.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import Field, ValidationError
from sqlalchemy import RowMapping, text

from ssc_contracts.audit import AuditAction
from ssc_contracts.capabilities import CapabilityDiff, EnvironmentCapabilities, diff_capabilities
from ssc_contracts.cells import CellResourceState
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_contracts.manifest import Manifest
from ssc_control.api.authz import require_app_builder, require_builder
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.runtime import runtime_of
from ssc_control.api.uow import UnitOfWork, UserUoW
from ssc_control.cell.resources import needs_for, notice_for, states, waiting_on
from ssc_control.deploy import ledgers
from ssc_control.deploy.tasks import defer_build, defer_deployment
from ssc_control.metrics.source_tool import SOURCE_TOOL_HEADER, source_tool_of
from ssc_control.ports import MetricKind
from ssc_control.runtime.specs import release_spec
from ssc_shared.runtime import Billing, billing_for

router = APIRouter()

MAX_PAGE: Final = 100
_NO_UPPER_BOUND: Final = 2**31 - 1

ActorKindName = Literal["user", "workload", "schedule", "operator", "integration"]
DeploymentKind = Literal["deploy", "rollback"]
DeploymentState = Literal["pending", "running", "healthy", "failed", "superseded"]
BuildState = Literal["queued", "running", "succeeded", "failed"]


class DeploymentCreate(Strict):
    release_id: str = Field(pattern=r"^rel_[a-z0-9]{20}$")
    kind: DeploymentKind = "deploy"
    confirm: bool = Field(
        default=False,
        description="For a rollback: go ahead although the environment's database has "
        "migrations the release lacks (`SCHEMA_AHEAD` otherwise). Ignored for a deploy.",
    )


class RecoveryPointOut(Strict):
    at: datetime = Field(description="When the database was at this point.")
    lsn: str | None = Field(
        description="The instance's write-ahead log position then; null when the cell could "
        "not say and `at` is the control plane's time."
    )


class LedgerAhead(Strict):
    ledger: str = Field(
        description="The migration tool: `prisma`, `alembic`, `django`, `drizzle` or `knex`."
    )
    names: list[str] = Field(
        description="The migrations the database may have run that the release lacks, in the "
        "order the tool applies them."
    )


class MigrationsAhead(Strict):
    environment_id: str
    release_id: str
    ledgers: list[LedgerAhead] = Field(
        description="Empty when a rollback to the release needs no `confirm`: the environment "
        "has no database, the database has run nothing the release lacks, or the release was "
        "made before migrations were recorded."
    )


class OperationAccepted(Strict):
    operation_id: str
    state: Literal["pending"]
    notice: str | None = Field(
        default=None,
        description="What this deployment sets off and waits for, such as the company's "
        "database being created the first time an app asks for one.",
    )


class OperationOut(Strict):
    operation_id: str
    kind: DeploymentKind
    state: DeploymentState
    app_id: str
    environment_id: str
    release_id: str
    started_at: datetime
    finished_at: datetime | None
    failure_code: str | None = Field(
        default=None, description="Why a `failed` deployment failed, as a reason code."
    )
    notice: str | None = Field(
        default=None,
        description="What a `running` deployment is waiting for, such as the company's "
        "database being created.",
    )
    billing: Billing | None = Field(
        default=None,
        description="How the release is billed while it runs: `instance` for a session app, "
        "`request` for any other. Null when its manifest cannot be read.",
    )
    recovery_point: RecoveryPointOut | None = Field(
        default=None,
        description="Where the environment's database was before this production deployment "
        "started; null for any other deployment.",
    )


class ActorOut(Strict):
    kind: ActorKindName
    id: str
    via_agent: bool


class DeploymentOut(Strict):
    operation_id: str
    kind: DeploymentKind
    state: DeploymentState
    release_id: str
    release_number: int
    failure_code: str | None
    current: bool = Field(description="The environment's live deployment.")
    actor: ActorOut
    started_at: datetime
    finished_at: datetime | None
    recovery_point: RecoveryPointOut | None = Field(
        default=None,
        description="Where the environment's database was before this production deployment "
        "started; null for any other deployment.",
    )


class DeploymentList(Strict):
    environment_id: str
    items: list[DeploymentOut] = Field(description="Newest first.")


class BuildCreate(Strict):
    bundle_id: str = Field(pattern=r"^bdl_[a-z0-9]{20}$")


class BuildAccepted(Strict):
    build_id: str
    state: Literal["queued"]
    capability_diff: CapabilityDiff = Field(
        description="What the manifest asks for beyond the environment. It never blocks."
    )


class BuildOut(Strict):
    build_id: str
    app_id: str
    environment_id: str
    bundle_id: str
    state: BuildState
    release_id: str | None
    release_number: int | None
    failure_code: str | None
    capability_diff: CapabilityDiff = Field(description="Against the environment today.")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ReleaseOut(Strict):
    release_id: str
    number: int
    label: str = Field(description="How the number is shown: `R<number>`.")
    image_digest: str
    manifest_digest: str
    source_digest: str = Field(description="The digest of the bundle it was built from.")
    source_commit: str | None
    built_for_environment_id: str | None = Field(
        description="The environment a build made it for; null when no build made it."
    )
    latest_migrations: dict[str, str] | None = Field(
        default=None,
        description="The latest migration of each migration tool the build found in the "
        'source, such as `{"prisma": "20261003120000_add_total"}`; null when the build did not '
        "read the source.",
    )
    created_at: datetime
    actor: ActorOut


class ReleaseList(Strict):
    items: list[ReleaseOut] = Field(description="Highest number first.")
    next_before: int | None = Field(
        description="Pass as `before` for the next page; null at the end."
    )


_SELECT_ENV = text(
    "select e.name, e.config_version, e.grants_version, a.status from ssc.environment e "
    "join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "where e.org_id = :org and e.app_id = :app and e.id = :env"
)
_SELECT_APP_STATUS = text("select status from ssc.app where org_id = :org and id = :app")
_SELECT_RELEASE_BUILD_ENV = text(
    "select b.environment_id from ssc.release r "
    "left join ssc.build b on b.org_id = r.org_id and b.release_id = r.id "
    "where r.org_id = :org and r.app_id = :app and r.id = :rel"
)
_SUPERSEDE_IN_FLIGHT_DEPLOY = text(
    "update ssc.deployment set state = 'superseded', finished_at = now() "
    "where org_id = :org and environment_id = :env and state in ('pending', 'running') "
    "and kind = 'deploy' returning id"
)
_INSERT_DEPLOYMENT = text(
    "insert into ssc.deployment (id, org_id, app_id, environment_id, release_id, kind, state, "
    "config_version, grants_version, actor_kind, actor_id, actor_via_agent, actor_client_id) "
    "values (:id, :org, :app, :env, :rel, :kind, 'pending', :cv, :gv, :actor_kind, :actor_id, "
    ":via_agent, :client_id)"
)
_SELECT_DEPLOYMENT = text(
    "select id as operation_id, kind, state, app_id, environment_id, release_id, started_at, "
    "finished_at, failure_code, recovery_at, recovery_lsn from ssc.deployment "
    "where org_id = :org and id = :id"
)
_SELECT_DEPLOYMENTS = text(
    "select d.id as operation_id, d.kind, d.state, d.release_id, r.number as release_number, "
    "d.failure_code, coalesce(e.current_deployment_id = d.id, false) as current, "
    "d.actor_kind, d.actor_id, d.actor_via_agent, d.started_at, d.finished_at, d.recovery_at, "
    "d.recovery_lsn from ssc.deployment d "
    "join ssc.environment e on e.org_id = d.org_id and e.id = d.environment_id "
    "join ssc.release r on r.org_id = d.org_id and r.id = d.release_id "
    "where d.org_id = :org and d.environment_id = :env "
    "order by d.started_at desc, d.id desc limit :limit"
)
_SELECT_BUNDLE = text(
    "select state, manifest from ssc.bundle where org_id = :org and app_id = :app and id = :id "
    "for share"
)
"""Shared, so a build never starts on a bundle the collector is expiring (``bundle_gc``)."""
_INSERT_BUILD = text(
    "insert into ssc.build (id, org_id, app_id, environment_id, bundle_id, actor_kind, actor_id, "
    "actor_via_agent, actor_client_id) values (:id, :org, :app, :env, :bundle, :actor_kind, "
    ":actor_id, :via_agent, :client_id)"
)
_SELECT_BUILD = text(
    "select b.id as build_id, b.app_id, b.environment_id, b.bundle_id, b.state, b.release_id, "
    "r.number as release_number, b.failure_code, b.created_at, b.started_at, b.finished_at, "
    "d.manifest from ssc.build b "
    "join ssc.bundle d on d.org_id = b.org_id and d.app_id = b.app_id and d.id = b.bundle_id "
    "left join ssc.release r on r.org_id = b.org_id and r.id = b.release_id "
    "where b.org_id = :org and b.id = :id"
)
_SELECT_CONNECTION_NAMES = text("select name from ssc.connection where org_id = :org")
_SELECT_EGRESS_HOSTS = text("select host from ssc.egress_host where org_id = :org")
_RELEASE_COLUMNS = (
    "select r.id as release_id, r.number, r.image_digest, r.manifest_digest, r.source_digest, "
    "r.source_commit, b.environment_id as built_for_environment_id, r.created_at, "
    "r.actor_kind, r.actor_id, r.actor_via_agent, case when r.migrations is null then null "
    "else coalesce((select jsonb_object_agg(m.key, m.value ->> -1) "
    "from jsonb_each(r.migrations) m), '{}'::jsonb) end as latest_migrations "
    "from ssc.release r "
    "left join ssc.build b on b.org_id = r.org_id and b.release_id = r.id "
)
_SELECT_RELEASES = text(
    _RELEASE_COLUMNS + "where r.org_id = :org and r.app_id = :app and r.number < :before "
    "order by r.number desc limit :limit"
)
_SELECT_RELEASE = text(
    _RELEASE_COLUMNS + "where r.org_id = :org and r.app_id = :app and r.id = :id"
)

_STARTED: Final = {"deploy": AuditAction.DEPLOY_STARTED, "rollback": AuditAction.ROLLBACK_STARTED}

SourceTool = Annotated[
    str | None,
    Header(
        alias=SOURCE_TOOL_HEADER,
        description="The builder tool making the change, for product metrics. "
        "An agent credential's client id takes precedence.",
    ),
]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]
ReleaseRef = Annotated[str, Query(pattern=r"^rel_[a-z0-9]{20}$", description="A release id.")]


def _actor(row: RowMapping) -> ActorOut:
    return ActorOut(kind=row["actor_kind"], id=row["actor_id"], via_agent=row["actor_via_agent"])


def _recovery_point(row: RowMapping) -> RecoveryPointOut | None:
    at = row["recovery_at"]
    return None if at is None else RecoveryPointOut(at=at, lsn=row["recovery_lsn"])


def _actor_params(uow: UnitOfWork) -> dict[str, Any]:
    p = uow.principal
    return {
        "actor_kind": p.kind.value,
        "actor_id": p.subject,
        "via_agent": p.is_agent,
        "client_id": p.client_id,
    }


async def _environment(uow: UnitOfWork, app_id: str, environment_id: str) -> RowMapping:
    """``NOT_FOUND`` for an environment the org's app does not have, then ``FORBIDDEN`` for a
    caller who may not change it."""
    params = {"org": uow.org_id, "app": app_id, "env": environment_id}
    env = (await uow.conn.execute(_SELECT_ENV, params)).mappings().first()
    if env is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    await require_builder(uow, environment_id)
    return env


async def _capability_diff(uow: UnitOfWork, manifest: object) -> CapabilityDiff:
    """The manifest against the environment: the org's connections by name and its egress
    allowlist (SSC-053); no Postgres until SSC-040."""
    try:
        parsed = Manifest.model_validate(manifest)
    except ValidationError as e:
        raise Refusal(ErrorCode.INTERNAL, evidence={"reason": "stored_manifest_invalid"}) from e
    names = (await uow.conn.execute(_SELECT_CONNECTION_NAMES, {"org": uow.org_id})).scalars()
    listed = (await uow.conn.execute(_SELECT_EGRESS_HOSTS, {"org": uow.org_id})).scalars()
    return diff_capabilities(
        parsed,
        EnvironmentCapabilities(
            connections=frozenset(str(n) for n in names),
            egress_hosts=frozenset(str(h) for h in listed),
        ),
    )


def _release(row: RowMapping) -> ReleaseOut:
    return ReleaseOut(
        release_id=row["release_id"],
        number=row["number"],
        label=f"R{row['number']}",
        image_digest=row["image_digest"],
        manifest_digest=row["manifest_digest"],
        source_digest=row["source_digest"],
        source_commit=row["source_commit"],
        built_for_environment_id=row["built_for_environment_id"],
        latest_migrations=row["latest_migrations"],
        created_at=row["created_at"],
        actor=_actor(row),
    )


# ── deployments ──────────────────────────────────────────────────────────────


@router.post(
    "/apps/{app_id}/environments/{environment_id}/deployments",
    status_code=202,
    response_model=OperationAccepted,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.APP_NOT_ACTIVE,
        ErrorCode.DEPLOYMENT_IN_FLIGHT,
        ErrorCode.RELEASE_ENVIRONMENT_MISMATCH,
        ErrorCode.REFERENCE_NOT_FOUND,
        ErrorCode.SCHEMA_AHEAD,
    ),
)
async def create_deployment(  # noqa: PLR0913  (FastAPI maps each parameter to the request)
    *,
    app_id: Id,
    environment_id: Id,
    body: DeploymentCreate,
    request: Request,
    uow: UserUoW,
    source_tool: SourceTool = None,
) -> Response:
    """Start a deployment: 202 plus a ``Location`` to poll. The deploy job runs it.

    A release a build made for another environment is ``RELEASE_ENVIRONMENT_MISMATCH``: prod
    builds separately from the same source. A second forward deploy while one is in flight is
    ``DEPLOYMENT_IN_FLIGHT``; a rollback supersedes the in-flight forward deploy instead. A
    rollback to a release that lacks migrations the environment's database may have run is
    ``SCHEMA_AHEAD`` without ``confirm``; ``GET .../migrations-ahead`` names them."""
    env = await _environment(uow, app_id, environment_id)
    if env["status"] != "active":
        raise Refusal(ErrorCode.APP_NOT_ACTIVE, evidence={"app_id": app_id})
    params = {"org": uow.org_id, "app": app_id, "env": environment_id, "rel": body.release_id}
    release = (await uow.conn.execute(_SELECT_RELEASE_BUILD_ENV, params)).first()
    if release is None:
        raise Refusal(ErrorCode.REFERENCE_NOT_FOUND, evidence={"release_id": body.release_id})
    if release[0] is not None and release[0] != environment_id:
        raise Refusal(
            ErrorCode.RELEASE_ENVIRONMENT_MISMATCH,
            evidence={"release_id": body.release_id, "built_for_environment_id": release[0]},
        )
    superseded: list[str] = []
    ahead: ledgers.Ledgers = {}
    if body.kind == "rollback":
        ahead = await ledgers.ahead(
            uow.conn,
            org_id=uow.org_id,
            app_id=app_id,
            environment_id=environment_id,
            release_id=body.release_id,
        )
        if ahead and not body.confirm:
            raise Refusal(
                ErrorCode.SCHEMA_AHEAD,
                evidence={
                    "release_id": body.release_id,
                    "ahead": {ledger: len(names) for ledger, names in ahead.items()},
                },
            )
        superseded = [
            str(i) for i in (await uow.conn.execute(_SUPERSEDE_IN_FLIGHT_DEPLOY, params)).scalars()
        ]
    dep_id = await start_deployment(
        uow,
        request,
        app_id=app_id,
        environment_id=environment_id,
        env_name=str(env["name"]),
        versions=(int(env["config_version"]), int(env["grants_version"])),
        release_id=body.release_id,
        kind=body.kind,
        superseded=superseded,
        migrations_ahead=ahead,
    )
    if body.kind == "deploy":
        await uow.metrics.record_event(
            uow.conn,
            org_id=uow.org_id,
            kind=MetricKind.DEPLOY,
            app_id=app_id,
            user_id=uow.principal.subject,
            source_tool=source_tool_of(uow.principal, source_tool),
            properties={"environment": str(env["name"]), "via_agent": uow.principal.is_agent},
        )
    return uow.reply(
        OperationAccepted(
            operation_id=dep_id,
            state="pending",
            notice=await _first_use_notice(uow, body.release_id),
        ),
        status=202,
        headers={"Location": f"/v1/operations/{dep_id}"},
    )


async def _first_use_notice(uow: UnitOfWork, release_id: str) -> str | None:
    """What deploying ``release_id`` sets off: the cell resources it needs that are not ready."""
    spec = await release_spec(uow.conn, uow.org_id, release_id)
    if spec is None:
        return None
    have = await states(uow.conn, uow.org_id)
    return notice_for(
        [
            r
            for r in needs_for(spec.manifest)
            if r not in have or have[r].state is not CellResourceState.READY
        ]
    )


async def start_deployment(  # noqa: PLR0913  (keyword-only)
    uow: UnitOfWork,
    request: Request,
    *,
    app_id: str,
    environment_id: str,
    env_name: str,
    versions: tuple[int, int],
    release_id: str,
    kind: DeploymentKind,
    superseded: list[str] | None = None,
    migrations_ahead: Mapping[str, list[str]] | None = None,
) -> str:
    """Insert a ``pending`` deployment with the environment's config and sharing ``versions``, open
    the production gate's approvals for ``prod``, audit it and defer its job. The caller has
    checked the environment, the caller, the app's status and the release. ``migrations_ahead``
    are the migrations a confirmed rollback goes back past, kept in the audit as
    ``<ledger>:<name>``."""
    dep_id = new_id("dep")
    await uow.conn.execute(
        _INSERT_DEPLOYMENT,
        {
            "org": uow.org_id,
            "app": app_id,
            "env": environment_id,
            "rel": release_id,
            "id": dep_id,
            "kind": kind,
            "cv": versions[0],
            "gv": versions[1],
            **_actor_params(uow),
        },
    )
    policy_decision_id: str | None = None
    if env_name == "prod":
        gate = await runtime_of(request).prod_gate.check(
            uow.conn,
            org_id=uow.org_id,
            app_id=app_id,
            environment_id=environment_id,
            release_id=release_id,
        )
        policy_decision_id = gate.policy_decision_id
    after: dict[str, object] = {"environment_id": environment_id, "release_id": release_id}
    if superseded:
        after["superseded"] = superseded
    if migrations_ahead:
        after["migrations_ahead"] = [
            f"{ledger}:{name}" for ledger, names in migrations_ahead.items() for name in names
        ]
        after["confirmed"] = True
    await uow.audit(
        _STARTED[kind],
        target_kind="deployment",
        target_id=dep_id,
        after=after,
        policy_decision_id=policy_decision_id,
    )
    await defer_deployment(
        uow.conn,
        org_id=uow.org_id,
        environment_id=environment_id,
        deployment_id=dep_id,
        rollback=kind == "rollback",
    )
    return dep_id


@router.get(
    "/operations/{operation_id}",
    response_model=OperationOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.NOT_FOUND),
)
async def get_operation(operation_id: Id, uow: UserUoW) -> OperationOut:
    row = (
        (await uow.conn.execute(_SELECT_DEPLOYMENT, {"org": uow.org_id, "id": operation_id}))
        .mappings()
        .first()
    )
    if row is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"operation_id": operation_id})
    waiting = await waiting_on(uow.conn, uow.org_id, operation_id)
    spec = await release_spec(uow.conn, uow.org_id, str(row["release_id"]))
    billing = None if spec is None else billing_for(spec.manifest.runtime, spec.framework)
    fields = {k: v for k, v in row.items() if k not in ("recovery_at", "recovery_lsn")}
    return OperationOut(
        **fields,
        notice=notice_for(waiting),
        billing=billing,
        recovery_point=_recovery_point(row),
    )


@router.get(
    "/apps/{app_id}/environments/{environment_id}/deployments",
    response_model=DeploymentList,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def list_deployments(
    app_id: Id, environment_id: Id, uow: UserUoW, limit: Limit = 50
) -> DeploymentList:
    """The environment's deployments, newest first; ``current`` marks the live one."""
    await _environment(uow, app_id, environment_id)
    params = {"org": uow.org_id, "env": environment_id, "limit": limit}
    found = (await uow.conn.execute(_SELECT_DEPLOYMENTS, params)).mappings().all()
    items = [
        DeploymentOut(
            operation_id=r["operation_id"],
            kind=r["kind"],
            state=r["state"],
            release_id=r["release_id"],
            release_number=r["release_number"],
            failure_code=r["failure_code"],
            current=r["current"],
            actor=_actor(r),
            started_at=r["started_at"],
            finished_at=r["finished_at"],
            recovery_point=_recovery_point(r),
        )
        for r in found
    ]
    return DeploymentList(environment_id=environment_id, items=items)


@router.get(
    "/apps/{app_id}/environments/{environment_id}/migrations-ahead",
    response_model=MigrationsAhead,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def migrations_ahead(
    app_id: Id, environment_id: Id, release_id: ReleaseRef, uow: UserUoW
) -> MigrationsAhead:
    """The migrations the environment's database may have run that ``release_id`` lacks: what a
    rollback to it goes back past, and why it is ``SCHEMA_AHEAD`` without ``confirm``."""
    await _environment(uow, app_id, environment_id)
    params = {"org": uow.org_id, "app": app_id, "rel": release_id}
    if (await uow.conn.execute(_SELECT_RELEASE_BUILD_ENV, params)).first() is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"release_id": release_id})
    ahead = await ledgers.ahead(
        uow.conn,
        org_id=uow.org_id,
        app_id=app_id,
        environment_id=environment_id,
        release_id=release_id,
    )
    return MigrationsAhead(
        environment_id=environment_id,
        release_id=release_id,
        ledgers=[LedgerAhead(ledger=k, names=v) for k, v in ahead.items()],
    )


# ── builds ───────────────────────────────────────────────────────────────────


@router.post(
    "/apps/{app_id}/environments/{environment_id}/builds",
    status_code=202,
    response_model=BuildAccepted,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.APP_NOT_ACTIVE,
        ErrorCode.BUILD_IN_FLIGHT,
        ErrorCode.BUNDLE_NOT_UPLOADED,
        ErrorCode.REFERENCE_NOT_FOUND,
        ErrorCode.PROD_REQUIRES_PROMOTE,
    ),
)
async def create_build(app_id: Id, environment_id: Id, body: BuildCreate, uow: UserUoW) -> Response:
    """Build a stored bundle for one environment: 202 plus a ``Location`` to poll. A build that
    succeeds creates the app's next numbered release. One build of a bundle per environment is in
    flight at a time (``BUILD_IN_FLIGHT``). ``prod`` builds only through promote
    (``PROD_REQUIRES_PROMOTE``)."""
    env = await _environment(uow, app_id, environment_id)
    if env["status"] != "active":
        raise Refusal(ErrorCode.APP_NOT_ACTIVE, evidence={"app_id": app_id})
    if env["name"] == "prod":
        raise Refusal(ErrorCode.PROD_REQUIRES_PROMOTE, evidence={"environment_id": environment_id})
    accepted = await start_build(uow, app_id, environment_id, body.bundle_id)
    return uow.reply(accepted, status=202, headers={"Location": f"/v1/builds/{accepted.build_id}"})


async def start_build(
    uow: UnitOfWork,
    app_id: str,
    environment_id: str,
    bundle_id: str,
    audit_extra: Mapping[str, object] | None = None,
) -> BuildAccepted:
    """Queue a build of a stored bundle, audit it and defer its job. The caller has checked the
    environment, the caller and the app's status."""
    params = {"org": uow.org_id, "app": app_id, "id": bundle_id}
    bundle = (await uow.conn.execute(_SELECT_BUNDLE, params)).first()
    if bundle is None:
        raise Refusal(ErrorCode.REFERENCE_NOT_FOUND, evidence={"bundle_id": bundle_id})
    if bundle[0] != "stored":
        raise Refusal(ErrorCode.BUNDLE_NOT_UPLOADED, evidence={"bundle_id": bundle_id})
    diff = await _capability_diff(uow, bundle[1])
    build_id = new_id("bld")
    await uow.conn.execute(
        _INSERT_BUILD,
        {
            "id": build_id,
            "org": uow.org_id,
            "app": app_id,
            "env": environment_id,
            "bundle": bundle_id,
            **_actor_params(uow),
        },
    )
    await uow.audit(
        AuditAction.BUILD_STARTED,
        target_kind="build",
        target_id=build_id,
        after={
            "environment_id": environment_id,
            "bundle_id": bundle_id,
            "state": "queued",
            **(audit_extra or {}),
        },
    )
    await defer_build(uow.conn, org_id=uow.org_id, build_id=build_id)
    return BuildAccepted(build_id=build_id, state="queued", capability_diff=diff)


@router.get(
    "/builds/{build_id}",
    response_model=BuildOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def get_build(build_id: Id, uow: UserUoW) -> BuildOut:
    row = (
        (await uow.conn.execute(_SELECT_BUILD, {"org": uow.org_id, "id": build_id}))
        .mappings()
        .first()
    )
    if row is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"build_id": build_id})
    await require_builder(uow, row["environment_id"])
    fields = {k: v for k, v in row.items() if k != "manifest"}
    return BuildOut(**fields, capability_diff=await _capability_diff(uow, row["manifest"]))


# ── releases ─────────────────────────────────────────────────────────────────


async def _app_for_builder(uow: UnitOfWork, app_id: str) -> None:
    params = {"org": uow.org_id, "app": app_id}
    if (await uow.conn.execute(_SELECT_APP_STATUS, params)).first() is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"app_id": app_id})
    await require_app_builder(uow, app_id)


@router.get(
    "/apps/{app_id}/releases",
    response_model=ReleaseList,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def list_releases(
    app_id: Id,
    uow: UserUoW,
    limit: Limit = 50,
    before: Annotated[int | None, Query(ge=1, le=_NO_UPPER_BOUND)] = None,
) -> ReleaseList:
    """The app's releases, highest number first; ``before`` pages to lower numbers."""
    await _app_for_builder(uow, app_id)
    params = {
        "org": uow.org_id,
        "app": app_id,
        "before": _NO_UPPER_BOUND if before is None else before,
        "limit": limit + 1,
    }
    found = (await uow.conn.execute(_SELECT_RELEASES, params)).mappings().all()
    items = [_release(r) for r in found[:limit]]
    more = len(found) > limit
    return ReleaseList(items=items, next_before=items[-1].number if more else None)


@router.get(
    "/apps/{app_id}/releases/{release_id}",
    response_model=ReleaseOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def get_release(app_id: Id, release_id: Id, uow: UserUoW) -> ReleaseOut:
    await _app_for_builder(uow, app_id)
    params = {"org": uow.org_id, "app": app_id, "id": release_id}
    row = (await uow.conn.execute(_SELECT_RELEASE, params)).mappings().first()
    if row is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"release_id": release_id})
    return _release(row)
