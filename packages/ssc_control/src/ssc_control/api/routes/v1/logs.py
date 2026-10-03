"""Logs and health (SSC-024): what an app environment printed, and whether it runs.

- ``GET .../logs``: the newest lines of one source, oldest first, or with ``after`` the lines
  after a cursor, waiting up to ``wait`` seconds for them (``ssc logs --follow``). ``app`` and
  ``build`` lines come from the cell's Cloud Logging through its agent, which builds the filter
  from the environment alone and keeps the cell under Cloud Logging's quota; past the cell's
  share, or the caller's, the answer is ``LOGS_RATE_LIMITED`` with ``Retry-After``. ``deploy``
  lines are this plane's own deployment records. Builders, the app's owner and org admins may
  read; a ``user``-role grant or none is ``FORBIDDEN``. An agent credential reads as its person
  does. Every line is redacted in the cell and again here.
- ``GET .../health``: ``running``, ``asleep`` (starts on the next request) or ``failing``, from
  the service's revisions and its recent request and system logs. Nothing asks the app itself, so
  a check never wakes it. Anyone who may see the app may ask, as for its database.

A log read holds no transaction while it waits on the cell.
"""

import asyncio
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ssc_contracts.errors import ErrorCode
from ssc_control.api.auth import Principal, UserPrincipal
from ssc_control.api.authz import require_builder
from ssc_control.api.problems import Refusal, request_id_of
from ssc_control.api.ratelimit import limit
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.common import Id, Strict
from ssc_control.api.runtime import runtime_of
from ssc_control.api.uow import UnitOfWork, UserUoW, check_scope, check_session
from ssc_control.db.bind import bound_org
from ssc_shared.logs import (
    CLOUD_BUILD_REF,
    MAX_BUILDS,
    MAX_LINES,
    MAX_SINCE_SECONDS,
    MAX_WAIT_SECONDS,
    HealthState,
    LogLine,
    LogPage,
    LogQuery,
    LogsError,
    LogsRateLimitedError,
    make_cursor,
    parse_cursor,
)
from ssc_shared.redaction import redact
from ssc_shared.runtime import service_name

router = APIRouter()

HEALTH_TIMEOUT_SECONDS: Final = 10.0
DEPLOY_POLL_SECONDS: Final = 1.0
CURSOR_PATTERN: Final = r"^[0-9]{1,19}\.[0-9]{1,19}\.[0-9]{1,19}$"

type LogSource = Literal["app", "build", "deploy"]

_SELECT_ENV = text(
    "select e.id from ssc.environment e where e.org_id = :org and e.app_id = :app and e.id = :env"
)
_BUILD_REFS = text(
    "select driver_ref from ssc.build where org_id = :org and environment_id = :env "
    "and driver_ref is not null order by created_at desc limit :n"
)
_DEPLOYMENTS = text(
    "select id, kind, release_id, state, failure_code, started_at, finished_at "
    "from ssc.deployment where org_id = :org and environment_id = :env "
    "and (started_at > :after or finished_at > :after) order by started_at limit 500"
)


class LogsQuery(Strict):
    source: LogSource = Field(
        default="app",
        description="`app`: what the app printed and its requests; `build`: its last builds; "
        "`deploy`: its deployments.",
    )
    since: int = Field(
        default=3600, ge=1, le=MAX_SINCE_SECONDS, description="Seconds back; ignored with `after`."
    )
    limit: int = Field(
        default=100, ge=1, le=MAX_LINES, description="The newest lines; ignored with `after`."
    )
    after: str | None = Field(
        default=None,
        pattern=CURSOR_PATTERN,
        description="A previous page's `cursor`: only lines after it.",
    )
    wait: int = Field(
        default=0,
        ge=0,
        le=MAX_WAIT_SECONDS,
        description="With `after`, seconds to wait for a line before answering with none.",
    )


class LogLineOut(Strict):
    timestamp: datetime
    severity: str = Field(description="Cloud Logging's: `DEFAULT`, `INFO`, `ERROR`...")
    source: LogSource
    text: str = Field(description="Redacted.")


class LogPageOut(Strict):
    environment_id: str
    source: LogSource
    lines: list[LogLineOut] = Field(description="Oldest first.")
    cursor: str | None = Field(description="Pass as `after` for the lines that follow.")


class HealthOut(Strict):
    environment_id: str
    state: HealthState | None = Field(
        description="`running`; `asleep`, which starts on the next request; `failing`; or null "
        "when nothing runs or the cell cannot tell (`reason`)."
    )
    reason: str = Field(
        description="`serving`, `always_on`, `idle`, `revision_failed`, `server_error`, "
        "`crashed`, `not_deployed`, `stopped` or `logs_unavailable`."
    )
    last_request_at: datetime | None
    checked_at: datetime


@router.get(
    "/apps/{app_id}/environments/{environment_id}/logs",
    response_model=LogPageOut,
    responses=problem_responses(
        *AUTHENTICATED,
        ErrorCode.FORBIDDEN,
        ErrorCode.NOT_FOUND,
        ErrorCode.LOGS_RATE_LIMITED,
        ErrorCode.LOGS_UNAVAILABLE,
    ),
)
async def get_logs(
    app_id: Id,
    environment_id: Id,
    params: Annotated[LogsQuery, Query()],
    request: Request,
    principal: UserPrincipal,
) -> LogPageOut:
    """Lines of one source, redacted. Builders, the owner and org admins only."""
    limit(request, principal)
    engine = runtime_of(request).engine
    builds = await _authorise(request, principal, app_id, environment_id, params.source)
    if params.source == "deploy":
        page = await _deploy_page(engine, principal.org_id, environment_id, params)
    else:
        page = await _cell_page(request, principal, environment_id, params, builds)
    return LogPageOut(
        environment_id=environment_id,
        source=params.source,
        lines=[
            LogLineOut(
                timestamp=line.timestamp,
                severity=line.severity,
                source=params.source,
                text=redact(line.text),
            )
            for line in page.lines
        ],
        cursor=page.cursor,
    )


@router.get(
    "/apps/{app_id}/environments/{environment_id}/health",
    response_model=HealthOut,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.NOT_FOUND, ErrorCode.LOGS_UNAVAILABLE),
)
async def get_health(app_id: Id, environment_id: Id, request: Request, uow: UserUoW) -> HealthOut:
    """Whether the environment runs, sleeps or fails, without sending the app a request."""
    params = {"org": uow.org_id, "app": app_id, "env": environment_id}
    if (await uow.conn.execute(_SELECT_ENV, params)).first() is None:
        raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
    logs = runtime_of(request).cell_logs
    if logs is None:
        raise Refusal(ErrorCode.LOGS_UNAVAILABLE, evidence={"reason": "not_configured"})
    try:
        async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS):
            health = await logs.health(service_name(environment_id), caller=_caller(uow.principal))
    except (LogsError, TimeoutError) as exc:
        raise Refusal(
            ErrorCode.LOGS_UNAVAILABLE, evidence={"reason": "cell", "error": type(exc).__name__}
        ) from None
    return HealthOut(
        environment_id=environment_id,
        state=health.state,
        reason=health.reason,
        last_request_at=health.last_request_at,
        checked_at=health.checked_at,
    )


async def _authorise(
    request: Request,
    principal: Principal,
    app_id: str,
    environment_id: str,
    source: LogSource,
) -> tuple[str, ...]:
    """The checks a ``UserUoW`` makes, then the builder rule, in a transaction of its own that
    ends before the cell is asked; the environment's recent Cloud Build ids for ``build``."""
    async with bound_org(runtime_of(request).engine, principal.org_id) as conn:
        await check_session(conn, principal)
        await check_scope(request, conn, principal)
        uow = UnitOfWork(conn=conn, principal=principal, request_id=request_id_of(request))
        params = {"org": principal.org_id, "app": app_id, "env": environment_id}
        if (await conn.execute(_SELECT_ENV, params)).first() is None:
            raise Refusal(ErrorCode.NOT_FOUND, evidence={"environment_id": environment_id})
        await require_builder(uow, environment_id)
        if source != "build":
            return ()
        refs = await conn.execute(
            _BUILD_REFS, {"org": principal.org_id, "env": environment_id, "n": MAX_BUILDS}
        )
        return tuple(r for (r,) in refs if CLOUD_BUILD_REF.fullmatch(r))


async def _cell_page(
    request: Request,
    principal: Principal,
    environment_id: str,
    params: LogsQuery,
    builds: tuple[str, ...],
) -> LogPage:
    logs = runtime_of(request).cell_logs
    if logs is None:
        raise Refusal(ErrorCode.LOGS_UNAVAILABLE, evidence={"reason": "not_configured"})
    source: Literal["app", "build"] = "build" if params.source == "build" else "app"
    query = LogQuery(service=service_name(environment_id), source=source, builds=builds)
    caller = _caller(principal)
    try:
        if params.after is None:
            return await logs.read(
                query, since_seconds=params.since, limit=params.limit, caller=caller
            )
        return await logs.follow(
            query, cursor=params.after, wait_seconds=params.wait, caller=caller
        )
    except LogsRateLimitedError as exc:
        raise Refusal(
            ErrorCode.LOGS_RATE_LIMITED,
            evidence={"retry_after": exc.retry_after},
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except LogsError as exc:
        raise Refusal(
            ErrorCode.LOGS_UNAVAILABLE, evidence={"reason": "cell", "error": str(exc)}
        ) from None


async def _deploy_page(
    engine: AsyncEngine, org_id: str, environment_id: str, params: LogsQuery
) -> LogPage:
    """Deployment records as lines; a follow polls them in short transactions."""
    if params.after is None:
        start = datetime.now(UTC) - timedelta(seconds=params.since)
        lines = (await _deploy_lines(engine, org_id, environment_id, start))[-params.limit :]
        return LogPage(lines=tuple(lines), cursor=make_cursor(0, 0, _last(lines, start)))
    _, _, after = parse_cursor(params.after)
    deadline = time.monotonic() + params.wait
    while True:
        lines = (await _deploy_lines(engine, org_id, environment_id, after))[:MAX_LINES]
        if lines or time.monotonic() >= deadline:
            return LogPage(lines=tuple(lines), cursor=make_cursor(0, 0, _last(lines, after)))
        await asyncio.sleep(min(DEPLOY_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


async def _deploy_lines(
    engine: AsyncEngine, org_id: str, environment_id: str, after: datetime
) -> list[LogLine]:
    async with bound_org(engine, org_id) as conn:
        rows = (
            await conn.execute(_DEPLOYMENTS, {"org": org_id, "env": environment_id, "after": after})
        ).mappings()
        lines: list[LogLine] = []
        for row in rows:
            what = f"{row['kind']} {row['id']} of {row['release_id']}"
            lines.append(_deploy_line(row["started_at"], "INFO", f"{what} started"))
            finished = row["finished_at"]
            if finished is None:
                continue
            match row["state"]:
                case "healthy":
                    lines.append(_deploy_line(finished, "INFO", f"{what} is live"))
                case "failed":
                    code = row["failure_code"] or "UNKNOWN"
                    lines.append(_deploy_line(finished, "ERROR", f"{what} failed: {code}"))
                case state:
                    lines.append(_deploy_line(finished, "INFO", f"{what} {state}"))
    return sorted((x for x in lines if x.timestamp > after), key=lambda x: x.timestamp)


def _deploy_line(at: datetime, severity: str, message: str) -> LogLine:
    return LogLine(timestamp=at, severity=severity, source="deploy", text=message)


def _last(lines: Sequence[LogLine], otherwise: datetime) -> datetime:
    return lines[-1].timestamp if lines else otherwise


def _caller(principal: Principal) -> str:
    """Who asks, for the cell's fair share: the person, whatever credential they use."""
    return f"{principal.org_id}/{principal.subject}"
