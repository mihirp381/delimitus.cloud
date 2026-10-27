"""Data connections and what each environment may reach (SSC-052).

Only an active org admin, never in an agent session, creates or changes a connection or links an
environment to one. A connection's address is stored when it is created and never returned. A
connection stays ``pending`` until an admin sets it ``ready`` after the runbook's first read; the
data gateway serves only ready ones. Every member reads every connection by name, kind and
classification, never an address (decision 034 amendment 2026-10-20); the columns an environment
sees are for whoever may build it.

Linking an environment whose audience already exceeds the connection's ceiling needs an approved
``exceed_ceiling`` request, decided by the connection's owner or an org admin. Lowering a ceiling
flags every environment now over it and opens nothing (``ssc_control.connections.service``).

What a connection shows an environment, its tables and columns, comes from the cell's data
gateway through the cell agent (GA-5.8, ``ssc_control.runtime.cell_datagw``), kept five minutes.
"""

import asyncio
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Any, Final, Literal, Self

from fastapi import APIRouter, Path, Request
from pydantic import Field, StringConstraints, model_validator
from sqlalchemy import text

from ssc_contracts.audit import AuditAction
from ssc_contracts.connections import SQL_KINDS, Address, AddressError, Kind, parse_address
from ssc_contracts.errors import ErrorCode
from ssc_contracts.snapshot import SnapshotLimits
from ssc_control.api.auth import PrincipalKind
from ssc_control.api.authz import active_role, can_build, require_admin
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.routes.v1.grants import missing_subjects
from ssc_control.api.runtime import cell_of, runtime_of
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.approvals.service import newest
from ssc_control.connections import service
from ssc_control.domain.approval_rules import Requirement, RequirementKind, exceed_subject_key
from ssc_control.domain.audience import Ceiling, CeilingError, ceiling_json, parse_ceiling
from ssc_control.names import REMOVED_GROUP, People, groups, people
from ssc_control.ports import SnapshotPort
from ssc_control.runtime import cell_datagw
from ssc_control.runtime.cell_datagw import CellSchemaError, CellSchemas, SchemaRefusal
from ssc_control.snapshot.service import awaited

router = APIRouter()

Name = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
NewName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
Schema = Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]{0,62}$")]
Classification = Literal["internal", "confidential", "restricted"]
_CHANGE: Final = (*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.AGENT_SESSION_REFUSED)
PUBLISH_WAIT_SECONDS: Final = 10.0
PUBLISH_POLL_SECONDS: Final = 0.5
PUBLISH_RETRY_AFTER_SECONDS: Final = 60
_GATEWAY_CODES: Final = {
    "CONNECTION_NOT_GRANTED": ErrorCode.CONNECTION_NOT_GRANTED,
    "UNKNOWN_ENVIRONMENT": ErrorCode.CONNECTION_NOT_GRANTED,
    "CONNECTION_SUSPENDED": ErrorCode.CONNECTION_SUSPENDED,
    "APP_NOT_ACTIVE": ErrorCode.APP_NOT_ACTIVE,
    "DATA_SNAPSHOT_STALE": ErrorCode.DATA_SNAPSHOT_STALE,
    "CONNECTION_UNAVAILABLE": ErrorCode.CONNECTION_UNAVAILABLE,
    "QUERY_TIMEOUT": ErrorCode.QUERY_TIMEOUT,
    "QUERY_FAILED": ErrorCode.CONNECTION_UNAVAILABLE,
}
"""The data gateway's codes this API passes on. ``QUERY_FAILED`` while reading the schema is the
source refusing the connection's own login (BigQuery's ``accessDenied``, say): its credentials or
permissions, so ``CONNECTION_UNAVAILABLE``, whose text says to check them; the source's message
is not passed on. Any other (its ``UNAUTHENTICATED`` for the agent's own token among them) is
``CELL_UNAVAILABLE``: never the caller's problem."""
_ENVIRONMENT: Final = text(
    "select 1 from ssc.environment where org_id = :org and app_id = :app and id = :env"
)


SCHEMAS_DESCRIPTION = (
    "Recorded for the setup script and the console; not enforced by the data gateway, which "
    "reads whatever the connection's database role may read. The setup script's grants are "
    "what bound it."
)

LIMITS_DESCRIPTION = (
    "Caps on every query. `max_rows`, `max_bytes` and `timeout_ms` are exact. `concurrency`, "
    "`daily_rows` and `daily_bytes` are advisory: each data gateway instance counts its own, "
    "and the service runs up to ten, so a grant can reach up to ten times them (decision 034)."
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


class PreviewAccessDoc(Strict):
    """Who may link the connection to a preview environment without a person deciding."""

    who: Literal["approval", "builders", "group"] = Field(
        description="`approval`: an active org admin decides each request. `builders`: anyone "
        "who may build the preview. `group`: a builder of the preview who is an active member of "
        "`group_id`."
    )
    group_id: Annotated[str, StringConstraints(pattern=r"^grp_[a-z0-9]{20}$")] | None = None

    @model_validator(mode="after")
    def _group_when_group(self) -> Self:
        if (self.who == "group") != (self.group_id is not None):
            raise ValueError("group_id is given when, and only when, who is group")
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
        default_factory=lambda: ["public"],
        min_length=1,
        max_length=50,
        description=SCHEMAS_DESCRIPTION,
    )
    limits: SnapshotLimits | None = Field(default=None, description=LIMITS_DESCRIPTION)
    preview_access: PreviewAccessDoc | None = Field(
        default=None,
        description="`internal` defaults to `builders`; `confidential` and `restricted` to "
        "`approval`.",
    )
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
    allowed_schemas: list[Schema] | None = Field(
        default=None, min_length=1, max_length=50, description=SCHEMAS_DESCRIPTION
    )
    limits: SnapshotLimits | None = Field(default=None, description=LIMITS_DESCRIPTION)
    setup_status: Literal["pending", "ready"] | None = Field(
        default=None,
        description="`ready` puts the connection in the snapshot, so the data gateway serves it "
        "to the environments granted it; a `pending` connection answers CONNECTION_NOT_GRANTED. "
        "Set it before the first read (runbook ssc-052, step 3); `pending` takes it out again.",
    )
    status: Literal["active", "suspended"] | None = Field(
        default=None, description="`suspended` stops every query on it at the next snapshot."
    )
    preview_access: PreviewAccessDoc | None = None


class ConnectionOut(Strict):
    id: str
    name: str
    kind: Kind
    owner_user_id: str | None
    owner_name: str | None
    owner_email: str | None
    classification: Classification
    ceiling: CeilingDoc
    allowed_schemas: list[str] = Field(description=SCHEMAS_DESCRIPTION)
    limits: SnapshotLimits
    setup_status: Literal["pending", "ready"]
    status: Literal["active", "suspended"]
    preview_access: PreviewAccessDoc
    preview_access_group_name: str | None = Field(
        description="The name of the group `preview_access` names, for a group rule."
    )
    ceiling_subject_names: dict[str, str] = Field(
        description="The name of each group and person the ceiling lists, by id."
    )
    created_at: datetime
    updated_at: datetime


class ConnectionsOut(Strict):
    connections: list[ConnectionOut]


class GrantBody(Strict):
    limits: SnapshotLimits | None = Field(
        default=None,
        description="The caps this environment's queries have, inside the connection's. "
        + LIMITS_DESCRIPTION,
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


class ColumnOut(Strict):
    name: str
    type: str = Field(description="Portable: `integer`, `decimal`, `string`, `timestamp`, ...")
    db_type: str | None = Field(description="The source's own type name.")


class TableOut(Strict):
    name: str
    columns: list[ColumnOut]


class ConnectionSchemaOut(Strict):
    connection: str
    kind: str
    tables: list[TableOut]
    snapshot_version: int = Field(description="The access snapshot the data gateway answered by.")
    cached: bool = Field(
        description="True when this answer was kept from an earlier read, at most five minutes "
        "ago; the data gateway was not asked again."
    )


def _schema_out(schema: cell_datagw.Schema, *, cached: bool) -> ConnectionSchemaOut:
    return ConnectionSchemaOut(
        connection=schema.connection,
        kind=schema.kind,
        tables=[
            TableOut(
                name=t.name,
                columns=[ColumnOut(name=c.name, type=c.type, db_type=c.db_type) for c in t.columns],
            )
            for t in schema.tables
        ],
        snapshot_version=schema.snapshot_version,
        cached=cached,
    )


def _ceiling_doc(ceiling: Ceiling) -> CeilingDoc:
    return CeilingDoc.model_validate({"subjects": [], **ceiling_json(ceiling)})


async def _outs(uow: UnitOfWork, found: Iterable[service.Connection]) -> list[ConnectionOut]:
    found = list(found)
    listed = [s for c in found for s in c.ceiling.subjects or ()]
    users = [c.owner_user_id for c in found] + [i for k, i in listed if k == "user"]
    group_ids = [c.preview_access.group_id for c in found] + [i for k, i in listed if k == "group"]
    owners = await people(uow.conn, uow.org_id, users)
    named = await groups(uow.conn, uow.org_id, group_ids)
    return [_out(c, owners, named) for c in found]


async def _one(uow: UnitOfWork, c: service.Connection) -> ConnectionOut:
    return (await _outs(uow, [c]))[0]


def _out(c: service.Connection, owners: People, named: dict[str, str]) -> ConnectionOut:
    group = c.preview_access.group_id
    return ConnectionOut(
        id=c.id,
        name=c.name,
        kind=c.kind,
        owner_user_id=c.owner_user_id,
        owner_name=owners.name(c.owner_user_id),
        owner_email=owners.email(c.owner_user_id),
        classification=c.classification,
        ceiling=_ceiling_doc(c.ceiling),
        allowed_schemas=list(c.allowed_schemas),
        limits=SnapshotLimits.model_validate(c.limits),
        setup_status=c.setup_status,
        status=c.status,
        preview_access=PreviewAccessDoc(who=c.preview_access.who, group_id=group),
        preview_access_group_name=None if group is None else named.get(group, REMOVED_GROUP),
        ceiling_subject_names={
            i: owners.of(i).name if k == "user" else named.get(i, REMOVED_GROUP)
            for k, i in c.ceiling.subjects or ()
        },
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


async def _preview_access(uow: UnitOfWork, doc: PreviewAccessDoc) -> service.PreviewAccess:
    if missing := await missing_subjects(uow, [("group", doc.group_id)]):
        raise Refusal(ErrorCode.REFERENCE_NOT_FOUND, evidence={"subject_ids": missing})
    return service.PreviewAccess(doc.who, doc.group_id)


def _limits(limits: SnapshotLimits | None) -> dict[str, Any]:
    return {} if limits is None else limits.model_dump(mode="json", exclude_none=True)


async def _admin(uow: UnitOfWork) -> str:
    by = await require_admin(uow)
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    return by


async def _reader(uow: UnitOfWork) -> None:
    kind = uow.principal.kind
    if kind not in {PrincipalKind.USER, PrincipalKind.OPERATOR}:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"kind": kind.value})
    if kind is PrincipalKind.USER and await active_role(uow) is None:
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"reason": "not_an_active_member"})


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


async def environment_out(uow: UnitOfWork, environment_id: str) -> EnvironmentConnectionsOut:
    links = await service.links(uow.conn, uow.org_id, environment_id)
    shown = await _outs(uow, [link.connection for link in links])
    return EnvironmentConnectionsOut(
        connections=[
            EnvironmentConnectionOut(
                environment_id=environment_id,
                connection=out,
                limits=SnapshotLimits.model_validate(link.limits),
                over_ceiling_since=link.over_ceiling_since,
                granted_at=link.created_at,
            )
            for link, out in zip(links, shown, strict=True)
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
        ErrorCode.REFERENCE_NOT_FOUND,
    ),
)
async def create_connection(body: ConnectionIn, uow: UserUoW) -> Any:
    """Add a connection by hand with the customer. Active org admins only, never in an agent
    session. It starts `pending`. `CEILING_REQUIRED` for a `confidential` or `restricted`
    connection without a ceiling; `OWNER_NOT_ACTIVE` when the owner is not an active user;
    `ALREADY_EXISTS` for a name in use; `CONNECTOR_UNAVAILABLE` for a kind the platform has no
    connector for yet; `REFERENCE_NOT_FOUND` for a preview access group the org does not have.
    The address is stored and never returned; the credentials stay a runbook step. Audited as
    `connection.created`."""
    await _admin(uow)
    preview = (
        None if body.preview_access is None else await _preview_access(uow, body.preview_access)
    )
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
            preview_access=preview,
        )
    except service.ConnectionChangeError as exc:
        raise _refusal(exc, body.name) from None
    return uow.reply(await _one(uow, made), status=201)


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
    """Every connection of the org, for every member, by name, kind and classification, never an
    address."""
    await _reader(uow)
    await _audit_operator(uow)
    return ConnectionsOut(
        connections=await _outs(uow, await service.list_all(uow.conn, uow.org_id))
    )


@router.get(
    "/connections/{name}",
    response_model=ConnectionOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def get_connection(name: Name, uow: UserUoW) -> ConnectionOut:
    """One connection, for every member; never its address."""
    await _reader(uow)
    found = await service.get(uow.conn, uow.org_id, name)
    if found is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"connection": name})
    await _audit_operator(uow)
    return await _one(uow, found)


@router.patch(
    "/connections/{name}",
    response_model=ConnectionOut,
    responses=problem_responses(
        *_CHANGE,
        ErrorCode.NOT_FOUND,
        ErrorCode.OWNER_NOT_ACTIVE,
        ErrorCode.CEILING_REQUIRED,
        ErrorCode.REFERENCE_NOT_FOUND,
    ),
)
async def patch_connection(name: Name, body: ConnectionPatch, uow: UserUoW) -> ConnectionOut:
    """Change a connection's owner, classification, ceiling, schemas, limits, setup status,
    status or preview access rule. Active org admins only, never in an agent session. A new
    ceiling flags every environment now over it (`over_ceiling_since`, one `connection.flagged`
    audit row each) and opens no approval; the data gateway is not told. Moving to
    `confidential` or `restricted` needs a ceiling in the same request."""
    await _admin(uow)
    changes: dict[str, Any] = body.model_dump(
        exclude_none=True, exclude={"ceiling", "limits", "preview_access"}
    )
    if body.ceiling is not None:
        changes["ceiling"] = _ceiling(body.ceiling)
    if body.limits is not None:
        changes["limits"] = _limits(body.limits)
    if body.preview_access is not None:
        changes["preview_access"] = await _preview_access(uow, body.preview_access)
    try:
        changed = await service.update(
            uow.conn, org_id=uow.org_id, actor=actor_of(uow.principal), name=name, changes=changes
        )
    except service.ConnectionChangeError as exc:
        raise _refusal(exc, name) from None
    if changed is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"connection": name})
    return await _one(uow, changed)


@router.get(
    "/apps/{app_id}/environments/{environment_id}/connections",
    response_model=EnvironmentConnectionsOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND),
)
async def get_environment_connections(
    app_id: Id, environment_id: Id, uow: UserUoW
) -> EnvironmentConnectionsOut:
    """What the environment may reach."""
    await _reader(uow)
    await _environment(uow, app_id, environment_id)
    return await environment_out(uow, environment_id)


@router.get(
    "/apps/{app_id}/environments/{environment_id}/connections/{name}/schema",
    response_model=ConnectionSchemaOut,
    responses=problem_responses(
        *AUTHENTICATED,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.CONNECTION_NOT_GRANTED,
        ErrorCode.CONNECTION_SUSPENDED,
        ErrorCode.APP_NOT_ACTIVE,
        ErrorCode.DATA_SNAPSHOT_STALE,
        ErrorCode.CONNECTION_UNAVAILABLE,
        ErrorCode.QUERY_TIMEOUT,
        ErrorCode.CELL_UNAVAILABLE,
    ),
)
async def get_environment_connection_schema(  # noqa: PLR0913  (FastAPI maps each parameter)
    app_id: Id, environment_id: Id, name: Name, request: Request, uow: UserUoW
) -> ConnectionSchemaOut:
    """The tables and columns the connection shows the environment's app, as the cell's data
    gateway answers for it, asked through the cell agent. For whoever may build the environment
    (`FORBIDDEN` otherwise) and operators, agents included. An answer is kept five minutes
    (`cached`). The gateway's refusals pass on with its code: `CONNECTION_NOT_GRANTED` (pending,
    not linked, or not in the snapshot yet), `CONNECTION_SUSPENDED`, `APP_NOT_ACTIVE`,
    `DATA_SNAPSHOT_STALE`, `CONNECTION_UNAVAILABLE`, `QUERY_TIMEOUT`; `CELL_UNAVAILABLE` when the
    cell could not be asked. A ready, linked connection the gateway does not know yet waits up to
    ten seconds for the cell to have the snapshot holding the link and asks once more, then is
    `DATA_SNAPSHOT_STALE` with `Retry-After`. A read, not an audit event (an operator's access is
    recorded as on every read)."""
    await _reader(uow)
    await _connection(uow, name)
    await _environment(uow, app_id, environment_id)
    principal = uow.principal
    if principal.kind is PrincipalKind.USER and not await can_build(
        uow.conn, uow.org_id, principal.subject, environment_id
    ):
        raise Refusal(ErrorCode.FORBIDDEN, evidence={"reason": "not_a_builder"})
    await _audit_operator(uow)
    rt = runtime_of(request)
    cache = rt.schema_cache
    key = (uow.org_id, environment_id, name)
    kept = cache.get(key)
    if kept is not None:
        return _schema_out(kept, cached=True)
    cell = await cell_of(request, uow.org_id)
    if cell is None or cell.datagw is None:
        raise Refusal(ErrorCode.CELL_UNAVAILABLE, evidence={"reason": "not_configured"})
    try:
        schema = await _read_schema(uow, rt.snapshot, cell.datagw, name, environment_id)
    except SchemaRefusal as exc:
        code = _GATEWAY_CODES.get(exc.code, ErrorCode.CELL_UNAVAILABLE)
        raise Refusal(
            code, evidence={"connection": name, "gateway_code": exc.code, "status": exc.status}
        ) from None
    except CellSchemaError as exc:
        raise Refusal(
            ErrorCode.CELL_UNAVAILABLE, evidence={"connection": name, "error": str(exc)}
        ) from None
    cache.put(key, schema)
    return _schema_out(schema, cached=False)


async def _read_schema(
    uow: UnitOfWork, snapshot: SnapshotPort, datagw: CellSchemas, name: str, environment_id: str
) -> cell_datagw.Schema:
    try:
        return await datagw.schema(name, environment_id)
    except SchemaRefusal as exc:
        if exc.code != "CONNECTION_NOT_GRANTED" or not await _ready_link(uow, environment_id, name):
            raise
        refused = exc
    version = await awaited(uow.conn, uow.org_id)
    if await snapshot.confirmed(uow.org_id, version):
        raise refused
    published = await _wait_confirmed(snapshot, uow.org_id, version)
    try:
        return await datagw.schema(name, environment_id)
    except SchemaRefusal as exc:
        if published or exc.code != "CONNECTION_NOT_GRANTED":
            raise
    raise Refusal(
        ErrorCode.DATA_SNAPSHOT_STALE,
        evidence={"connection": name, "version": version},
        headers={"Retry-After": str(PUBLISH_RETRY_AFTER_SECONDS)},
    )


async def _ready_link(uow: UnitOfWork, environment_id: str, name: str) -> bool:
    return any(
        link.connection.name == name and link.connection.setup_status == "ready"
        for link in await service.links(uow.conn, uow.org_id, environment_id)
    )


async def _wait_confirmed(snapshot: SnapshotPort, org_id: str, version: int) -> bool:
    deadline = time.monotonic() + PUBLISH_WAIT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(PUBLISH_POLL_SECONDS)
        if await snapshot.confirmed(org_id, version):
            return True
    return False


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
    return await environment_out(uow, environment_id)


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
    return await environment_out(uow, environment_id)
