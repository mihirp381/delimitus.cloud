"""Data connections and what each environment may reach (SSC-052).

Only an active org admin, never in an agent session, creates or changes a connection or links an
environment to one. A connection's address is stored when it is created and never returned. A
connection stays ``pending`` until an admin sets it ``ready`` after the runbook's first read; the
data gateway serves only ready ones. Reads follow the approvals rule (decision 016): operators and
active org admins see every connection; anyone else sees only those their own approved
``connect_data_source`` requests name.

Linking an environment whose audience already exceeds the connection's ceiling needs an approved
``exceed_ceiling`` request, decided by the connection's owner or an org admin. Lowering a ceiling
flags every environment now over it and opens nothing (``ssc_control.connections.service``).
"""

from datetime import datetime
from typing import Annotated, Any, Final, Literal, Self

from fastapi import APIRouter, Path
from pydantic import Field, StringConstraints, model_validator
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.connections import SQL_KINDS, Address, AddressError, Kind, parse_address
from ssc_contracts.errors import ErrorCode
from ssc_contracts.snapshot import SnapshotLimits
from ssc_control.api.auth import PrincipalKind
from ssc_control.api.authz import require_admin
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.approvals import sees_every_request
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.approvals.service import newest
from ssc_control.connections import service
from ssc_control.domain.approval_rules import Requirement, RequirementKind, exceed_subject_key
from ssc_control.domain.audience import Ceiling, CeilingError, ceiling_json, parse_ceiling

router = APIRouter()

Name = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
NewName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
Schema = Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]{0,62}$")]
Classification = Literal["internal", "confidential", "restricted"]
_CHANGE: Final = (*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.AGENT_SESSION_REFUSED)
_OWN_NAMES: Final = text(
    "select distinct subject_key from ssc.approval_request where org_id = :org "
    "and kind = 'connect_data_source' and state = 'approved' and requested_by_user_id = :me"
)
_ENVIRONMENT: Final = text(
    "select 1 from ssc.environment where org_id = :org and app_id = :app and id = :env"
)


class SubjectDoc(Strict):
    kind: Literal["group", "user"]
    id: Annotated[str, StringConstraints(pattern=r"^(grp|usr)_[a-z0-9]{20}$")]


class CeilingDoc(Strict):
    """The widest audience an app using the connection may have."""

    audience: Literal["org", "subjects"] = Field(
        description="`org`: anyone in the org. `subjects`: only the listed groups and users; a "
        "user also counts when they are an active member of a listed group when it is checked."
    )
    subjects: list[SubjectDoc] = Field(default_factory=list[SubjectDoc], max_length=100)

    @model_validator(mode="after")
    def _subjects_match_audience(self) -> Self:
        if (self.audience == "subjects") != bool(self.subjects):
            raise ValueError("subjects are listed when, and only when, the audience is subjects")
        return self


class ConnectionIn(Strict):
    name: NewName
    kind: Kind = Field(
        default="postgres",
        description="The kind of source. A kind without a connector yet is refused with "
        "`CONNECTOR_UNAVAILABLE`.",
    )
    owner_user_id: Annotated[str, StringConstraints(pattern=r"^usr_[a-z0-9]{20}$")] = Field(
        description="The active user who decides when an app exceeds the ceiling."
    )
    classification: Classification
    ceiling: CeilingDoc | None = Field(
        default=None,
        description="Required for `confidential` and `restricted`; `internal` defaults to `org`.",
    )
    allowed_schemas: list[Schema] = Field(
        default_factory=lambda: ["public"], min_length=1, max_length=50
    )
    limits: SnapshotLimits | None = None
    address: dict[str, Any] | None = Field(
        default=None,
        description="Where the source is, by kind: `{host, port?, database}` for `postgres`, "
        "`mysql` and `sqlserver`; `{project, dataset, location?}` for `bigquery`; `{account, "
        "database, schema?, warehouse, role?}` for `snowflake`; `{spreadsheet_id, sheet?}` for "
        "`gsheets`; `{bucket, prefix?}` for `gcs`; `{bucket, prefix?, region}` for `s3`; "
        "`{base_id, table?}` for `airtable`; `{base_url}` for `rest`. Never a credential. Stored "
        "for the data gateway; never returned.",
    )
    host: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9.-]{1,255}$")] | None = Field(
        default=None, description="The SQL kinds' address, in place of `address`. Never returned."
    )
    port: Annotated[int, Field(ge=1, le=65535)] | None = Field(
        default=None, description="With `host`; the engine's default when left out."
    )
    database: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,63}$")] | None = Field(
        default=None, description="With `host`. Never returned."
    )

    @model_validator(mode="after")
    def _one_address_that_fits_the_kind(self) -> Self:
        self.parsed_address()
        return self

    def parsed_address(self) -> Address:
        """The address as the kind's model. The three SQL members and `address` are two ways to
        say the same thing; sending both, or neither, is refused."""
        typed = {
            k: v
            for k, v in (("host", self.host), ("port", self.port), ("database", self.database))
            if v is not None
        }
        if typed and self.address is not None:
            raise ValueError("send host, port and database, or address, not both")
        if typed and self.kind not in SQL_KINDS:
            raise ValueError(f"host, port and database belong to a SQL kind, not {self.kind}")
        data = self.address if self.address is not None else typed
        if not data:
            raise ValueError("address is required")
        try:
            return parse_address(self.kind, data)
        except AddressError as exc:
            raise ValueError(str(exc)) from None


class ConnectionPatch(Strict):
    owner_user_id: Annotated[str, StringConstraints(pattern=r"^usr_[a-z0-9]{20}$")] | None = None
    classification: Classification | None = None
    ceiling: CeilingDoc | None = None
    allowed_schemas: list[Schema] | None = Field(default=None, min_length=1, max_length=50)
    limits: SnapshotLimits | None = None
    setup_status: Literal["pending", "ready"] | None = Field(
        default=None,
        description="`ready` puts the connection in the snapshot, so the data gateway serves it "
        "to the environments granted it; a `pending` connection answers CONNECTION_NOT_GRANTED. "
        "Set it before the first read (runbook ssc-052, step 3); `pending` takes it out again.",
    )
    status: Literal["active", "suspended"] | None = Field(
        default=None, description="`suspended` stops every query on it at the next snapshot."
    )


class ConnectionOut(Strict):
    id: str
    name: str
    kind: Kind
    owner_user_id: str | None
    classification: Classification
    ceiling: CeilingDoc
    allowed_schemas: list[str]
    limits: SnapshotLimits
    setup_status: Literal["pending", "ready"]
    status: Literal["active", "suspended"]
    created_at: datetime
    updated_at: datetime


class ConnectionsOut(Strict):
    connections: list[ConnectionOut]


class GrantBody(Strict):
    limits: SnapshotLimits | None = Field(
        default=None,
        description="The caps this environment's queries have, inside the connection's.",
    )


class EnvironmentConnectionOut(Strict):
    environment_id: str
    connection: ConnectionOut
    limits: SnapshotLimits
    over_ceiling_since: datetime | None = Field(
        description="Set when the environment's audience went beyond the ceiling; narrow the "
        "audience, or ask to share it wider (`exceed_ceiling`), to clear it."
    )
    granted_at: datetime


class EnvironmentConnectionsOut(Strict):
    connections: list[EnvironmentConnectionOut]


def _ceiling_doc(ceiling: Ceiling) -> CeilingDoc:
    return CeilingDoc.model_validate({"subjects": [], **ceiling_json(ceiling)})


def _out(c: service.Connection) -> ConnectionOut:
    return ConnectionOut(
        id=c.id,
        name=c.name,
        kind=c.kind,
        owner_user_id=c.owner_user_id,
        classification=c.classification,
        ceiling=_ceiling_doc(c.ceiling),
        allowed_schemas=list(c.allowed_schemas),
        limits=SnapshotLimits.model_validate(c.limits),
        setup_status=c.setup_status,
        status=c.status,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def _ceiling(doc: CeilingDoc) -> Ceiling:
    raw: dict[str, Any] = {"audience": doc.audience}
    if doc.audience == "subjects":
        raw["subjects"] = [s.model_dump() for s in doc.subjects]
    try:
        return parse_ceiling(raw)
    except CeilingError as exc:
        raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"ceiling": str(exc)}) from None


def _limits(limits: SnapshotLimits | None) -> dict[str, Any]:
    return {} if limits is None else limits.model_dump(mode="json", exclude_none=True)


async def _admin(uow: UnitOfWork) -> str:
    by = await require_admin(uow)
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    return by


async def _visible(uow: UnitOfWork) -> frozenset[str] | None:
    """The names the caller may see; None for every one."""
    if await sees_every_request(uow):
        return None
    rows = await uow.conn.execute(_OWN_NAMES, {"org": uow.org_id, "me": uow.principal.subject})
    return frozenset(str(n) for n in rows.scalars())


async def _audit_operator(uow: UnitOfWork) -> None:
    if uow.principal.kind is PrincipalKind.OPERATOR:
        await uow.audit(AuditAction.OPERATOR_ACCESS, target_kind="org", target_id=uow.org_id)


async def _connection(uow: UnitOfWork, name: str) -> service.Connection:
    found = await service.get(uow.conn, uow.org_id, name)
    if found is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"connection": name})
    return found


async def _environment(uow: UnitOfWork, app_id: str, environment_id: str) -> None:
    found = await uow.conn.execute(
        _ENVIRONMENT, {"org": uow.org_id, "app": app_id, "env": environment_id}
    )
    if found.first() is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})


async def _environment_out(
    uow: UnitOfWork, environment_id: str, visible: frozenset[str] | None
) -> EnvironmentConnectionsOut:
    return EnvironmentConnectionsOut(
        connections=[
            EnvironmentConnectionOut(
                environment_id=environment_id,
                connection=_out(link.connection),
                limits=SnapshotLimits.model_validate(link.limits),
                over_ceiling_since=link.over_ceiling_since,
                granted_at=link.created_at,
            )
            for link in await service.links(uow.conn, uow.org_id, environment_id)
            if visible is None or link.connection.name in visible
        ]
    )


@router.post(
    "/connections",
    status_code=201,
    response_model=ConnectionOut,
    dependencies=[UserIdempotent],
    responses=problem_responses(
        *POST_COMMON,
        ErrorCode.FORBIDDEN,
        ErrorCode.AGENT_SESSION_REFUSED,
        ErrorCode.ALREADY_EXISTS,
        ErrorCode.OWNER_NOT_ACTIVE,
        ErrorCode.CEILING_REQUIRED,
        ErrorCode.CONNECTOR_UNAVAILABLE,
    ),
)
async def create_connection(body: ConnectionIn, uow: UserUoW) -> Any:
    """Add a connection by hand with the customer. Active org admins only, never in an agent
    session. It starts `pending`. `CEILING_REQUIRED` for a `confidential` or `restricted`
    connection without a ceiling; `OWNER_NOT_ACTIVE` when the owner is not an active user;
    `ALREADY_EXISTS` for a name in use; `CONNECTOR_UNAVAILABLE` for a kind the platform has no
    connector for yet. The address is stored and never returned; the credentials stay a runbook
    step. Audited as `connection.created`."""
    await _admin(uow)
    try:
        made = await service.create(
            uow.conn,
            org_id=uow.org_id,
            actor=actor_of(uow.principal),
            name=body.name,
            owner_user_id=body.owner_user_id,
            classification=body.classification,
            ceiling=None if body.ceiling is None else _ceiling(body.ceiling),
            allowed_schemas=body.allowed_schemas,
            limits=_limits(body.limits),
            kind=body.kind,
            address=body.parsed_address(),
        )
    except service.ConnectionChangeError as exc:
        raise _refusal(exc, body.name) from None
    return uow.reply(_out(made), status=201)


def _refusal(exc: service.ConnectionChangeError, name: str) -> Refusal:
    code = {
        "name_taken": ErrorCode.ALREADY_EXISTS,
        "owner_not_active": ErrorCode.OWNER_NOT_ACTIVE,
        "ceiling_required": ErrorCode.CEILING_REQUIRED,
        "kind_unavailable": ErrorCode.CONNECTOR_UNAVAILABLE,
    }[exc.problem]
    return Refusal(code, evidence={"connection": name})


@router.get(
    "/connections",
    response_model=ConnectionsOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def list_connections(uow: UserUoW) -> ConnectionsOut:
    """The connections the caller may see, by name: all of them for operators and org admins,
    otherwise only those the caller's own approved requests name. Never an address."""
    visible = await _visible(uow)
    await _audit_operator(uow)
    return ConnectionsOut(
        connections=[
            _out(c)
            for c in await service.list_all(uow.conn, uow.org_id)
            if visible is None or c.name in visible
        ]
    )


@router.get(
    "/connections/{name}",
    response_model=ConnectionOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def get_connection(name: Name, uow: UserUoW) -> ConnectionOut:
    """One connection; `NOT_FOUND` for one the caller may not see."""
    visible = await _visible(uow)
    found = await service.get(uow.conn, uow.org_id, name)
    if found is None or (visible is not None and name not in visible):
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"connection": name})
    await _audit_operator(uow)
    return _out(found)


@router.patch(
    "/connections/{name}",
    response_model=ConnectionOut,
    responses=problem_responses(
        *_CHANGE, ErrorCode.NOT_FOUND, ErrorCode.OWNER_NOT_ACTIVE, ErrorCode.CEILING_REQUIRED
    ),
)
async def patch_connection(name: Name, body: ConnectionPatch, uow: UserUoW) -> ConnectionOut:
    """Change a connection's owner, classification, ceiling, schemas, limits, setup status or
    status. Active org admins only, never in an agent session. A new ceiling flags every
    environment now over it (`over_ceiling_since`, one `connection.flagged` audit row each) and
    opens no approval; the data gateway is not told. Moving to `confidential` or `restricted`
    needs a ceiling in the same request."""
    await _admin(uow)
    changes: dict[str, Any] = body.model_dump(exclude_none=True, exclude={"ceiling", "limits"})
    if body.ceiling is not None:
        changes["ceiling"] = _ceiling(body.ceiling)
    if body.limits is not None:
        changes["limits"] = _limits(body.limits)
    try:
        changed = await service.update(
            uow.conn, org_id=uow.org_id, actor=actor_of(uow.principal), name=name, changes=changes
        )
    except service.ConnectionChangeError as exc:
        raise _refusal(exc, name) from None
    if changed is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"connection": name})
    return _out(changed)


@router.get(
    "/apps/{app_id}/environments/{environment_id}/connections",
    response_model=EnvironmentConnectionsOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def get_environment_connections(
    app_id: Id, environment_id: Id, uow: UserUoW
) -> EnvironmentConnectionsOut:
    """What the environment may reach, of the connections the caller may see."""
    visible = await _visible(uow)
    await _environment(uow, app_id, environment_id)
    return await _environment_out(uow, environment_id, visible)


@router.put(
    "/apps/{app_id}/environments/{environment_id}/connections/{name}",
    response_model=EnvironmentConnectionsOut,
    responses=problem_responses(*_CHANGE, ErrorCode.NOT_FOUND, ErrorCode.APPROVAL_REQUIRED),
)
async def put_environment_connection(  # noqa: PLR0913  (FastAPI maps each parameter)
    app_id: Id, environment_id: Id, name: Name, body: GrantBody, uow: UserUoW
) -> EnvironmentConnectionsOut:
    """Let the environment reach a connection, or change its own limits. Active org admins only,
    never in an agent session. When the environment's audience already exceeds the connection's
    ceiling, `APPROVAL_REQUIRED` until an `exceed_ceiling` request for the connection and the
    environment's current grants is approved (ask with `POST /v1/approvals`). A pending
    connection can be linked; it stays out of the snapshot until it is ready."""
    by = await _admin(uow)
    await _environment(uow, app_id, environment_id)
    connection = await _connection(uow, name)
    policy_id: str | None = None
    held = {
        link.connection.name for link in await service.links(uow.conn, uow.org_id, environment_id)
    }
    if name not in held:
        grants = await service.environment_grants(uow.conn, uow.org_id, environment_id)
        if await service.over_ceiling(uow.conn, uow.org_id, connection, grants):
            req = Requirement(RequirementKind.EXCEED_CEILING, exceed_subject_key(name, grants))
            found = (
                await newest(
                    uow.conn, org_id=uow.org_id, environment_id=environment_id, requirements=[req]
                )
            ).get(req)
            if found is None or found.state != "approved":
                raise Refusal(
                    ErrorCode.APPROVAL_REQUIRED,
                    evidence={
                        "environment_id": environment_id,
                        "requirements": [
                            {
                                "kind": req.kind.value,
                                "subject_key": req.subject_key,
                                "approval_id": None if found is None else found.id,
                                "state": None if found is None else found.state,
                            }
                        ],
                    },
                )
            policy_id = found.policy_decision_id
    await service.grant(
        uow.conn,
        org_id=uow.org_id,
        actor=actor_of(uow.principal),
        connection=connection,
        environment_id=environment_id,
        limits=_limits(body.limits),
        by_user_id=by,
        policy_decision_id=policy_id,
    )
    return await _environment_out(uow, environment_id, None)


@router.delete(
    "/apps/{app_id}/environments/{environment_id}/connections/{name}",
    response_model=EnvironmentConnectionsOut,
    responses=problem_responses(*_CHANGE, ErrorCode.NOT_FOUND),
)
async def delete_environment_connection(
    app_id: Id, environment_id: Id, name: Name, uow: UserUoW
) -> EnvironmentConnectionsOut:
    """Stop the environment reaching a connection. Active org admins only, never in an agent
    session; `NOT_FOUND` when it did not. Audited as `connection.revoked`."""
    await _admin(uow)
    await _environment(uow, app_id, environment_id)
    connection = await _connection(uow, name)
    removed = await service.revoke(
        uow.conn,
        org_id=uow.org_id,
        actor=actor_of(uow.principal),
        connection=connection,
        environment_id=environment_id,
    )
    if not removed:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"connection": name})
    return await _environment_out(uow, environment_id, None)
