"""App databases (SSC-040, decision 003): what an environment's database is, never how to log in.

The cell agent makes the database the first time a deployment declares ``[state] postgres =
true`` (``deploy.deployments``). Here:

- ``GET .../database``: whether the environment has one, its connection limit, the pool size each
  instance should keep, and, when the cell agent answers in time, its size, its open connections
  and how many of the instance's places are taken. Anyone who may see the app may ask.
- ``POST .../database/rotate``: the cell agent gives the login role a new password and writes it
  as new versions of ``DATABASE_URL`` and ``PGPASSWORD``; the versions are recorded like a secret
  set by a person (``secret.rotated``) and a deployment of the live release pins them. Needs a
  person who may change the environment; an agent credential is ``AGENT_SESSION_REFUSED``.

No response holds the password, the URL or any other secret value.
"""

import asyncio
import logging
from datetime import datetime
from typing import Final

from fastapi import APIRouter, Request, Response
from pydantic import Field
from sqlalchemy import RowMapping, text

from ssc_contracts import app_database
from ssc_contracts.errors import ErrorCode
from ssc_control.api.authz import require_builder
from ssc_control.api.idempotency import UserIdempotent
from ssc_control.api.problems import Refusal
from ssc_control.api.routes.common import AUTHENTICATED, POST_COMMON, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.routes.v1.deployments import start_deployment
from ssc_control.api.runtime import cell_of
from ssc_control.api.uow import UnitOfWork, UserUoW, actor_of
from ssc_control.runtime.app_databases import (
    AppDatabaseError,
    DatabaseUsage,
    database_record,
    problem_code,
    record_database,
)
from ssc_shared.runtime import database_name, service_name

log = logging.getLogger(__name__)
router = APIRouter()

USAGE_TIMEOUT_SECONDS: Final = 10.0


class DatabaseOut(Strict):
    environment_id: str
    present: bool = Field(description="False until a deployment that declares Postgres made it.")
    database: str | None = Field(description="The database's name, which is also its user.")
    connection_limit: int | None = Field(
        description="Connections the app may hold at once, across all its instances."
    )
    pool_size: int = Field(description="Connections each instance of the app should keep.")
    max_instances: int = Field(
        description="The most instances a stateful app runs; the connection left over is kept "
        "for a new revision during a deploy or rotation."
    )
    size_bytes: int | None = Field(description="Null when the cell did not say.")
    connections: int | None = Field(description="Open now; null when the cell did not say.")
    places_used: int | None = Field(
        description="App databases on the company's instance; null when the cell did not say."
    )
    places_total: int | None = Field(
        description="App databases the instance's tier holds; null when the cell did not say."
    )
    created_at: datetime | None
    rotated_at: datetime | None


class DatabaseRotateOut(Strict):
    environment_id: str
    rotated_at: datetime
    operation_id: str | None = Field(
        description="The deployment that puts the new password live; null when the environment "
        "has no live deployment (its next deployment takes it)."
    )


_SELECT_ENV = text(
    "select e.name, e.grants_version, e.current_deployment_id, a.status "
    "from ssc.environment e join ssc.app a on a.org_id = e.org_id and a.id = e.app_id "
    "where e.org_id = :org and e.app_id = :app and e.id = :env"
)
_LOCK_ENV = text(_SELECT_ENV.text + " for update of e")
_IN_FLIGHT = text(
    "select id from ssc.deployment where org_id = :org and environment_id = :env "
    "and state in ('pending', 'running') limit 1"
)
_BUMP_CONFIG = text(
    "update ssc.environment set config_version = config_version + 1 "
    "where org_id = :org and id = :env returning config_version, now() as at"
)
_LIVE_RELEASE = text("select release_id from ssc.deployment where org_id = :org and id = :id")


async def _environment(
    uow: UnitOfWork, app_id: str, environment_id: str, *, lock: bool = False
) -> RowMapping:
    params = {"org": uow.org_id, "app": app_id, "env": environment_id}
    env = (await uow.conn.execute(_LOCK_ENV if lock else _SELECT_ENV, params)).mappings().first()
    if env is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    return env


async def _usage(request: Request, org_id: str, environment_id: str) -> DatabaseUsage | None:
    """What the cell says of the database; None when it cannot say, or the org's cell is not
    configured here."""
    try:
        cell = await cell_of(request, org_id)
    except Refusal:
        return None
    databases = None if cell is None else cell.app_databases
    if databases is None:
        return None
    try:
        async with asyncio.timeout(USAGE_TIMEOUT_SECONDS):
            return await databases.usage(service_name(environment_id))
    except (AppDatabaseError, TimeoutError) as exc:
        log.warning("app database usage for %s: %s", environment_id, type(exc).__name__)
        return None


@router.get(
    "/apps/{app_id}/environments/{environment_id}/database",
    response_model=DatabaseOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.NOT_FOUND),
)
async def get_database(
    app_id: Id, environment_id: Id, request: Request, uow: UserUoW
) -> DatabaseOut:
    """The environment's database as far as it is known: never a password or a URL."""
    await _environment(uow, app_id, environment_id)
    row = await database_record(uow.conn, org_id=uow.org_id, environment_id=environment_id)
    usage = await _usage(request, uow.org_id, environment_id) if row is not None else None
    return DatabaseOut(
        environment_id=environment_id,
        present=row is not None,
        database=database_name(service_name(environment_id)) if row is not None else None,
        connection_limit=int(row["connection_limit"]) if row is not None else None,
        pool_size=app_database.POOL_SIZE,
        max_instances=app_database.MAX_INSTANCES,
        size_bytes=usage.size_bytes if usage else None,
        connections=usage.connections if usage else None,
        places_used=usage.environments if usage else None,
        places_total=usage.ceiling if usage else None,
        created_at=row["created_at"] if row is not None else None,
        rotated_at=row["rotated_at"] if row is not None else None,
    )


@router.post(
    "/apps/{app_id}/environments/{environment_id}/database/rotate",
    response_model=DatabaseRotateOut,
    dependencies=[UserIdempotent],
    responses={
        202: {
            "model": DatabaseRotateOut,
            "description": "Rotated; a deployment of the live release started to put the new "
            "password live. Poll the `Location`.",
        },
        **problem_responses(
            *POST_COMMON,
            ErrorCode.FORBIDDEN,
            ErrorCode.NOT_FOUND,
            ErrorCode.APP_NOT_ACTIVE,
            ErrorCode.AGENT_SESSION_REFUSED,
            ErrorCode.DEPLOYMENT_IN_FLIGHT,
            ErrorCode.DATABASE_UNAVAILABLE,
            ErrorCode.CELL_UNAVAILABLE,
        ),
    },
)
async def rotate_database(
    app_id: Id, environment_id: Id, request: Request, uow: UserUoW
) -> Response:
    """A new password for the app's login role, then a deployment of the live release that pins
    it (202); 200 when nothing is live. ``DEPLOYMENT_IN_FLIGHT`` while a deployment runs."""
    env = await _environment(uow, app_id, environment_id, lock=True)
    await require_builder(uow, environment_id)
    if uow.principal.is_agent:
        raise Refusal(ErrorCode.AGENT_SESSION_REFUSED)
    if env["status"] != "active":
        raise Refusal(ErrorCode.APP_NOT_ACTIVE, evidence={"app_id": app_id})
    params = {"org": uow.org_id, "env": environment_id}
    if await database_record(uow.conn, org_id=uow.org_id, environment_id=environment_id) is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    in_flight = (await uow.conn.execute(_IN_FLIGHT, params)).scalar()
    if in_flight is not None:
        raise Refusal(ErrorCode.DEPLOYMENT_IN_FLIGHT, evidence={"operation_id": in_flight})
    cell = await cell_of(request, uow.org_id)
    databases = None if cell is None else cell.app_databases
    if databases is None:
        raise Refusal(ErrorCode.DATABASE_UNAVAILABLE, evidence={"reason": "not_configured"})
    try:
        made = await databases.rotate(service_name(environment_id))
    except AppDatabaseError as exc:
        raise Refusal(problem_code(exc), evidence={"reason": "cell", "error": str(exc)}) from None
    await record_database(
        uow.conn,
        org_id=uow.org_id,
        environment_id=environment_id,
        made=made,
        actor=actor_of(uow.principal),
    )
    bumped = (await uow.conn.execute(_BUMP_CONFIG, params)).one()
    live = env["current_deployment_id"]
    if live is None:
        out = DatabaseRotateOut(
            environment_id=environment_id, rotated_at=bumped.at, operation_id=None
        )
        return uow.reply(out)
    release_id = (await uow.conn.execute(_LIVE_RELEASE, {**params, "id": live})).scalar_one()
    dep_id = await start_deployment(
        uow,
        request,
        app_id=app_id,
        environment_id=environment_id,
        env_name=str(env["name"]),
        versions=(int(bumped.config_version), int(env["grants_version"])),
        release_id=str(release_id),
        kind="deploy",
    )
    out = DatabaseRotateOut(
        environment_id=environment_id, rotated_at=bumped.at, operation_id=dep_id
    )
    return uow.reply(out, status=202, headers={"Location": f"/v1/operations/{dep_id}"})
