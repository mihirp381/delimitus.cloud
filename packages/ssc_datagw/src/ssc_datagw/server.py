"""The data gateway: ``POST /v1/connections/{name}/query`` (SSC-050, C19) and the file broker,
``POST /v1/files/{put,get,delete}`` (SSC-046, ``ssc_datagw.files``).

One per customer, in the cell, on Cloud Run at minimum 0 and request-billed (decision 023
amendment); it leaves through Direct VPC egress and the cell NAT, so the customer's database
sees the cell's one fixed IP. ``docs/contracts/data-gateway.md`` is the contract. Each query
goes through these steps, and the first that refuses answers:

1. the workload token names an app environment of this cell (``ssc_datagw.workload``);
2. the snapshot, re-read on demand, is at most 120 s old and the environment is active;
3. the identity note, when the app forwards one, verifies for that environment;
4. the connection is granted to the environment and not suspended (``ssc_datagw.admission``);
5. limits compose by minimum, the grant's daily budget is not spent, a concurrency slot frees
   within 2 s (``ssc_datagw.limits``);
6. the connector runs the read under a deadline, watched by the kill watch, and the gateway
   keeps at most ``max_rows`` rows and ``max_bytes`` bytes, saying when it cut the result.

A file request goes through steps 1 and 2, and the kill switch refuses it there like a query:
a disabled or quarantined app gets no link. Its name, which may say who a file is about, is
never logged.

The kill watch re-reads the snapshot every ``WATCH_SECONDS`` while any query runs (the instance
has CPU then) and cancels each query its environment or connection no longer admits. Every
answer is logged with its outcome, never the SQL text, parameters or rows; the first answer of
an instance is marked ``cold`` with the instance's start time.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jwt import PyJWKSet
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
)

from ssc_contracts.identity import IDENTITY_HEADER
from ssc_datagw.admission import (
    Admitted,
    OnDemandSnapshot,
    Refusal,
    admit,
    environment_refusal,
)
from ssc_datagw.connectors import (
    Column,
    Connector,
    JsonValue,
    Query,
    QueryFailedError,
    QueryRefusedError,
    UpstreamUnavailableError,
    encoded_size,
    jsonable,
)
from ssc_datagw.files import (
    FileBroker,
    FileMissingError,
    FileNameError,
    FilesQuotaError,
    FilesUnavailableError,
    Link,
)
from ssc_datagw.kinds import connector_for
from ssc_datagw.limits import (
    BudgetSpentError,
    DailyBudget,
    Limits,
    Slots,
    SlotsBusyError,
    compose,
)
from ssc_datagw.note import NoteRefusedError, verify_note
from ssc_datagw.settings import Settings, settings_from_env
from ssc_datagw.workload import (
    GoogleWorkloads,
    Workload,
    WorkloadKeysUnavailableError,
    WorkloadRefusedError,
)
from ssc_shared import redaction
from ssc_shared.access import AccessView, ViewHolder
from ssc_shared.blobstore import BlobStore
from ssc_shared.blobstore_gcs import GcsBlobStore, IamSigner, bucket_of
from ssc_shared.hosts import PREVIEW_SUFFIX, app_origin
from ssc_shared.snapshot_feed import SnapshotFeed

log = logging.getLogger(__name__)

STARTED_AT: Final = time.time()
"""When this process started: the start of a cold start."""
QUERY_PATH: Final = "/v1/connections/{name}/query"
FILES_PATH: Final = "/v1/files/{op}"
FILE_OPS: Final = ("put", "get", "delete")
MAX_BODY: Final = 1024 * 1024
MAX_SQL: Final = 100_000
MAX_PARAMS: Final = 1000
WATCH_SECONDS: Final = 1.0
TIMEOUT_GRACE_SECONDS: Final = 2.0
"""Beyond ``timeout_ms`` before the gateway cancels a read itself; the database's own statement
timeout (``ssc_datagw.postgres``) should end it first."""
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")

Stage = Literal[
    "request", "workload", "user", "admission", "limits", "classify", "execute", "files"
]
FileOp = Literal["put", "get", "delete"]
FixOwner = Literal["app", "admin", "platform"]
UserContext = Literal["verified", "schedule", "app_only"]


@dataclass(frozen=True, slots=True)
class DataError:
    status: int
    stage: Stage
    fix_owner: FixOwner
    message: str


ERRORS: Final[Mapping[str, DataError]] = {
    "BODY_TOO_LARGE": DataError(413, "request", "app", "The request body is over 1 MB."),
    "VALIDATION_FAILED": DataError(
        422, "request", "app", "The request body is not a valid query or file request."
    ),
    "UNAUTHENTICATED": DataError(
        401, "workload", "app", "The workload token is missing or is not an app of this cell."
    ),
    "UNAVAILABLE": DataError(
        503, "workload", "platform", "The data gateway cannot answer right now."
    ),
    "IDENTITY_REFUSED": DataError(
        401, "user", "app", "The identity note does not verify for this app environment."
    ),
    "DATA_SNAPSHOT_STALE": DataError(
        503,
        "admission",
        "platform",
        "The data gateway has no access snapshot from the last 120 seconds, so it admits nothing.",
    ),
    "UNKNOWN_ENVIRONMENT": DataError(
        403, "admission", "platform", "This app environment is not in the access snapshot."
    ),
    "APP_NOT_ACTIVE": DataError(
        403, "admission", "admin", "This app is disabled or quarantined; an admin can enable it."
    ),
    "CONNECTION_NOT_GRANTED": DataError(
        403, "admission", "admin", "This app environment has no grant on that connection."
    ),
    "CONNECTION_SUSPENDED": DataError(
        403, "admission", "admin", "That connection is suspended; its owner can resume it."
    ),
    "DAILY_BUDGET_SPENT": DataError(
        429, "limits", "admin", "This grant has used today's rows or bytes; it resets at 00:00 UTC."
    ),
    "CONCURRENCY_LIMIT": DataError(
        429, "limits", "app", "Too many queries are running on this grant; try again shortly."
    ),
    "QUERY_REFUSED": DataError(
        422, "classify", "app", "Only one read-only statement may run in a query."
    ),
    "QUERY_FAILED": DataError(
        422, "execute", "app", "The database refused the statement; sqlstate says why."
    ),
    "QUERY_TIMEOUT": DataError(408, "execute", "app", "The query ran past its time limit."),
    "CONNECTION_UNAVAILABLE": DataError(
        503, "execute", "platform", "The database cannot be reached right now."
    ),
    "NOT_FOUND": DataError(404, "request", "app", "There is no such file operation."),
    "FILE_NOT_FOUND": DataError(404, "files", "app", "This app environment has no such file."),
    "FILES_QUOTA_EXCEEDED": DataError(
        413,
        "files",
        "app",
        "This app environment's files use all of its quota; delete some before storing more.",
    ),
    "FILES_UNAVAILABLE": DataError(
        503, "files", "platform", "File storage cannot be reached right now."
    ),
}


class QueryBody(BaseModel):
    """``params`` bind ``$1``, ``$2``, ...; ``max_rows``, ``max_bytes`` and ``timeout_ms`` can
    only narrow what the platform, the connection and the grant allow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sql: StrictStr = Field(min_length=1, max_length=MAX_SQL)
    params: tuple[StrictStr | StrictBool | StrictInt | StrictFloat | None, ...] = Field(
        default=(), max_length=MAX_PARAMS
    )
    max_rows: StrictInt | None = Field(default=None, ge=0)
    max_bytes: StrictInt | None = Field(default=None, ge=0)
    timeout_ms: StrictInt | None = Field(default=None, ge=0)


class FileBody(BaseModel):
    """One file of the calling environment; ``content_type`` is for ``put`` alone."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: StrictStr = Field(min_length=1, max_length=1024)
    content_type: StrictStr | None = Field(default=None, max_length=1024)


class RefusedError(Exception):
    """One refusal: ``code`` in :data:`ERRORS`. ``reason`` goes to the log only."""

    def __init__(
        self,
        code: str,
        *,
        reason: str = "",
        stage: Stage | None = None,
        sqlstate: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason
        self.stage: Stage | None = stage
        self.sqlstate = sqlstate


class Workloads(Protocol):
    async def verify(self, authorization: str | None) -> Workload: ...


class Snapshot(Protocol):
    async def refresh(self) -> None: ...

    def view(self) -> AccessView | None: ...


@dataclass(slots=True)
class Instance:
    """This instance's start, for the cold-start record."""

    started_at: float = STARTED_AT
    ready_at: float | None = None
    answered: int = 0

    def ready(self, at: float | None = None) -> None:
        self.ready_at = time.time() if at is None else at

    @property
    def ready_ms(self) -> int | None:
        return None if self.ready_at is None else round((self.ready_at - self.started_at) * 1000)


@dataclass(slots=True)
class Result:
    columns: list[Column]
    rows: list[list[JsonValue]]
    size: int
    truncated_reason: str | None


@dataclass(eq=False, slots=True)
class Inflight:
    env_id: str
    name: str
    task: asyncio.Task[Result]
    killed: Refusal | None = None


class KillWatch:
    """While any query runs, re-reads the snapshot every ``every`` seconds and cancels each
    query the snapshot no longer admits. Stops when none runs."""

    def __init__(self, snapshot: Snapshot, *, every: float = WATCH_SECONDS) -> None:
        self._snapshot = snapshot
        self._every = every
        self._inflight: set[Inflight] = set()
        self._task: asyncio.Task[None] | None = None

    def add(self, entry: Inflight) -> None:
        self._inflight.add(entry)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def remove(self, entry: Inflight) -> None:
        self._inflight.discard(entry)

    async def _run(self) -> None:
        while self._inflight:
            await asyncio.sleep(self._every)
            await self._snapshot.refresh()
            view = self._snapshot.view()
            for entry in list(self._inflight):
                verdict = admit(view, entry.env_id, entry.name)
                if isinstance(verdict, str) and entry.killed is None:
                    entry.killed = verdict
                    entry.task.cancel()
                    log.warning("query ended by the snapshot: %s", verdict)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat(timespec="milliseconds")


def _request_id(header: str | None) -> str:
    return header if header and _REQUEST_ID.fullmatch(header) else uuid.uuid4().hex


def error_body(
    code: str, request_id: str, *, stage: Stage | None = None, sqlstate: str | None = None
) -> dict[str, Any]:
    entry = ERRORS[code]
    error: dict[str, Any] = {
        "code": code,
        "stage": stage or entry.stage,
        "message": entry.message,
        "fix_owner": entry.fix_owner,
    }
    if sqlstate is not None:
        error["sqlstate"] = sqlstate
    return {"error": error, "request_id": request_id}


class DataGateway:
    """The query pipeline. ``connectors`` maps a ``con_`` id to its connector; ``clock`` is the
    wall clock in seconds (tests)."""

    def __init__(  # noqa: PLR0913  (keyword-only collaborators)
        self,
        *,
        settings: Settings,
        workloads: Workloads,
        snapshot: Snapshot,
        connectors: Mapping[str, Connector],
        files: FileBroker | None = None,
        instance: Instance | None = None,
        budget: DailyBudget | None = None,
        slots: Slots | None = None,
        watch_seconds: float = WATCH_SECONDS,
        grace: float = TIMEOUT_GRACE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._keys = PyJWKSet.from_dict(dict(settings.jwks))
        self._workloads = workloads
        self._snapshot = snapshot
        self._connectors = connectors
        self._files = files
        self.instance = instance or Instance()
        self._budget = budget or DailyBudget()
        self._slots = slots or Slots()
        self._watch = KillWatch(snapshot, every=watch_seconds)
        self._grace = grace
        self._clock = clock

    async def query(self, name: str, request: Request) -> JSONResponse:
        received = self._clock()
        started = time.monotonic()
        request_id = _request_id(request.headers.get("x-request-id"))
        record: dict[str, Any] = {"request_id": request_id, "connection": name}
        try:
            body, status = await self._run(name, request, record), 200
            body["request_id"] = request_id
            record["outcome"] = "served"
        except RefusedError as refused:
            body = error_body(
                refused.code, request_id, stage=refused.stage, sqlstate=refused.sqlstate
            )
            status = ERRORS[refused.code].status
            record.update(outcome=refused.code, reason=refused.reason)
        except Exception:
            log.exception("query failed: %s", request_id)
            body, status = error_body("UNAVAILABLE", request_id, stage="execute"), 503
            record["outcome"] = "UNAVAILABLE"
        elapsed = round((time.monotonic() - started) * 1000)
        if status == 200:
            body["elapsed_ms"] = elapsed
        self._record(record, received, elapsed)
        return JSONResponse(body, status_code=status, headers={"x-request-id": request_id})

    async def file(self, op: str, request: Request) -> JSONResponse:
        """A link to put or get one file of the calling environment, or its deletion."""
        received = self._clock()
        started = time.monotonic()
        request_id = _request_id(request.headers.get("x-request-id"))
        record: dict[str, Any] = {"request_id": request_id, "file_op": op}
        try:
            body, status = await self._file(op, request, record), 200
            body["request_id"] = request_id
            record["outcome"] = "served"
        except RefusedError as refused:
            body = error_body(refused.code, request_id, stage=refused.stage)
            status = ERRORS[refused.code].status
            record.update(outcome=refused.code, reason=refused.reason)
        except Exception:
            log.exception("file request failed: %s", request_id)
            body, status = error_body("FILES_UNAVAILABLE", request_id), 503
            record["outcome"] = "FILES_UNAVAILABLE"
        self._record(record, received, round((time.monotonic() - started) * 1000), "file")
        return JSONResponse(body, status_code=status, headers={"x-request-id": request_id})

    async def _file(self, op: str, request: Request, record: dict[str, Any]) -> dict[str, Any]:
        if op not in FILE_OPS:
            raise RefusedError("NOT_FOUND")
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > MAX_BODY:
            raise RefusedError("BODY_TOO_LARGE")
        workload = await self._workload(request.headers.get("authorization"))
        record["env_id"] = workload.env_id
        await self._snapshot.refresh()
        view = self._snapshot.view()
        if view is not None:
            record["snapshot_version"] = view.version
        refused = environment_refusal(view, workload.env_id)
        if refused is not None or view is None:
            raise RefusedError(refused or "DATA_SNAPSHOT_STALE")
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise RefusedError("BODY_TOO_LARGE")
        try:
            body = FileBody.model_validate_json(raw)
        except ValidationError as exc:
            raise RefusedError("VALIDATION_FAILED", reason=f"{exc.error_count()} errors") from exc
        if self._files is None:
            raise RefusedError("FILES_UNAVAILABLE", reason="this gateway has no file broker")
        return await self._file_answer(op, workload.env_id, body)

    async def _file_answer(self, op: FileOp, env_id: str, body: FileBody) -> dict[str, Any]:
        files = self._files
        assert files is not None
        try:
            if op == "delete":
                await files.delete(env_id, body.name)
                return {"deleted": True}
            link: Link = (
                await files.upload(env_id, body.name, body.content_type)
                if op == "put"
                else await files.download(env_id, body.name)
            )
        except (FileNameError, ValueError) as exc:
            raise RefusedError("VALIDATION_FAILED", reason=str(exc)) from exc
        except FileMissingError as exc:
            raise RefusedError("FILE_NOT_FOUND") from exc
        except FilesQuotaError as exc:
            raise RefusedError("FILES_QUOTA_EXCEEDED") from exc
        except FilesUnavailableError as exc:
            raise RefusedError("FILES_UNAVAILABLE", reason=str(exc)) from exc
        return link.to_wire()

    def _record(
        self, record: dict[str, Any], received: float, elapsed: int, kind: str = "query"
    ) -> None:
        instance = self.instance
        record.update(
            received_at=_iso(received),
            elapsed_ms=elapsed,
            instance_started_at=_iso(instance.started_at),
            cold=instance.answered == 0,
        )
        if instance.answered == 0:
            record["ready_ms"] = instance.ready_ms
        instance.answered += 1
        log.info("datagw %s %s", kind, json.dumps(record, sort_keys=True))

    async def _run(self, name: str, request: Request, record: dict[str, Any]) -> dict[str, Any]:
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > MAX_BODY:
            raise RefusedError("BODY_TOO_LARGE")
        workload = await self._workload(request.headers.get("authorization"))
        record["env_id"] = workload.env_id
        await self._snapshot.refresh()
        view = self._snapshot.view()
        if view is not None:
            record["snapshot_version"] = view.version
        refused = environment_refusal(view, workload.env_id)
        if refused is not None or view is None:
            raise RefusedError(refused or "DATA_SNAPSHOT_STALE")
        record["user"], record["user_context"] = self._user(
            view, workload.env_id, request.headers.get(IDENTITY_HEADER)
        )
        admitted = admit(view, workload.env_id, name)
        if isinstance(admitted, str):
            raise RefusedError(admitted)
        query = await self._body(request)
        limits = compose(
            admitted.connection.limits,
            admitted.grant.limits,
            max_rows=query.max_rows,
            max_bytes=query.max_bytes,
            timeout_ms=query.timeout_ms,
        )
        try:
            budget = self._budget.remaining(admitted.grant_key, limits)
        except BudgetSpentError as exc:
            raise RefusedError("DAILY_BUDGET_SPENT") from exc
        connector = self._connectors.get(admitted.connection.connection_id)
        if connector is None:
            raise RefusedError("CONNECTION_UNAVAILABLE", reason="no connector for the connection")
        read = Query(
            sql=query.sql,
            params=query.params,
            max_rows=limits.max_rows,
            timeout_ms=limits.timeout_ms,
            tag=f"ssc:{admitted.environment.app_id}:{admitted.environment.name}:{record['request_id']}",
        )
        try:
            async with self._slots.hold(admitted.grant_key, limits.concurrency):
                result = await self._watched(
                    admitted, name, connector=connector, read=read, limits=limits, budget=budget
                )
        except SlotsBusyError as exc:
            raise RefusedError("CONCURRENCY_LIMIT") from exc
        self._budget.spend(admitted.grant_key, len(result.rows), result.size)
        record.update(
            rows=len(result.rows), bytes=result.size, truncated_reason=result.truncated_reason
        )
        return {
            "columns": [
                {"name": c.name, "type": c.type, "db_type": c.db_type} for c in result.columns
            ],
            "rows": result.rows,
            "row_count": len(result.rows),
            "truncated": result.truncated_reason is not None,
            "truncated_reason": result.truncated_reason,
            "snapshot_version": view.version,
        }

    async def _workload(self, authorization: str | None) -> Workload:
        try:
            return await self._workloads.verify(authorization)
        except WorkloadRefusedError as exc:
            raise RefusedError("UNAUTHENTICATED", reason=str(exc)) from exc
        except WorkloadKeysUnavailableError as exc:
            raise RefusedError("UNAVAILABLE", reason=str(exc)) from exc

    def _user(
        self, view: AccessView, env_id: str, token: str | None
    ) -> tuple[str | None, UserContext]:
        """Who the app is acting for. No note is ``app_only``; a note must verify for this
        environment, and a user in it must still be active."""
        if not token:
            return None, "app_only"
        env = view.environments[env_id]
        label = next((h for h, e in sorted(view.hosts.items()) if e == env_id), None)
        try:
            if label is None:
                raise NoteRefusedError("wrong_audience", "the environment has no host")
            audience = app_origin(
                label.removesuffix(PREVIEW_SUFFIX),
                env.name,
                self._settings.cell_label,
                self._settings.apps_domain,
            )
            note = verify_note(
                token,
                audience=audience,
                keys=self._keys,
                issuer=self._settings.issuer,
                now=int(self._clock()),
            )
        except (NoteRefusedError, ValueError) as exc:
            raise RefusedError("IDENTITY_REFUSED", reason=str(exc)) from exc
        if (note.org, note.app, note.env) != (view.org_id, env.app_id, env.name):
            raise RefusedError("IDENTITY_REFUSED", reason="the note is for another environment")
        if note.role == "schedule":
            return note.sub, "schedule"
        if note.sub not in view.active_users:
            raise RefusedError("IDENTITY_REFUSED", reason="the user is not active")
        return note.sub, "verified"

    async def _body(self, request: Request) -> QueryBody:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise RefusedError("BODY_TOO_LARGE")
        try:
            return QueryBody.model_validate_json(raw)
        except ValidationError as exc:
            raise RefusedError("VALIDATION_FAILED", reason=f"{exc.error_count()} errors") from exc

    async def _watched(  # noqa: PLR0913  (the admitted query and its limits)
        self,
        admitted: Admitted,
        name: str,
        *,
        connector: Connector,
        read: Query,
        limits: Limits,
        budget: tuple[int, int],
    ) -> Result:
        """Run the read as its own task, watched by the kill watch. A cancel by the watch is
        the refusal the snapshot now gives; a cancel of this request itself goes on up."""
        task = asyncio.create_task(self._read(connector, read, limits, budget))
        entry = Inflight(admitted.env_id, name, task)
        self._watch.add(entry)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if entry.killed is None or (current is not None and current.cancelling()):
                raise
            raise RefusedError(entry.killed, stage="execute") from None
        except TimeoutError as exc:
            raise RefusedError("QUERY_TIMEOUT") from exc
        except QueryRefusedError as exc:
            raise RefusedError("QUERY_REFUSED", reason=str(exc)) from exc
        except QueryFailedError as exc:
            raise RefusedError("QUERY_FAILED", reason=str(exc), sqlstate=exc.sqlstate) from exc
        except UpstreamUnavailableError as exc:
            raise RefusedError("CONNECTION_UNAVAILABLE", reason=str(exc)) from exc
        finally:
            self._watch.remove(entry)
            if not task.done():
                task.cancel()

    async def _read(
        self, connector: Connector, read: Query, limits: Limits, budget: tuple[int, int]
    ) -> Result:
        """At most ``max_rows`` rows and ``max_bytes`` bytes, narrowed by what is left of the
        daily budget; the reason names the cap that cut the result."""
        rows_left, bytes_left = budget
        max_rows, max_bytes = min(limits.max_rows, rows_left), min(limits.max_bytes, bytes_left)
        row_cap = "daily_rows" if rows_left < limits.max_rows else "max_rows"
        byte_cap = "daily_bytes" if bytes_left < limits.max_bytes else "max_bytes"
        rows: list[list[JsonValue]] = []
        size, reason = 0, None
        async with asyncio.timeout(limits.timeout_ms / 1000 + self._grace):
            async with connector.open(replace(read, max_rows=max_rows)) as cursor:
                columns = list(cursor.columns)
                async for raw in cursor.rows():
                    if len(rows) >= max_rows:
                        reason = row_cap
                        break
                    row = [jsonable(v) for v in raw]
                    n = encoded_size(row)
                    if size + n > max_bytes:
                        reason = byte_cap
                        break
                    rows.append(row)
                    size += n
        return Result(columns, rows, size, reason)

    async def aclose(self) -> None:
        await self._watch.aclose()


def create_app(
    gateway: DataGateway,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="ssc-datagw", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.post(QUERY_PATH)
    async def query(name: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return await gateway.query(name, request)

    @app.post(FILES_PATH)
    async def file(op: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return await gateway.file(op, request)

    return app


def production_app(
    env: Mapping[str, str] | None = None,
    *,
    store: BlobStore | None = None,
    connectors: Mapping[str, Connector] | None = None,
    workloads: Workloads | None = None,
    files: FileBroker | None = None,
) -> FastAPI:
    """``store`` replaces the cell bucket, ``workloads`` Google's keys and ``files`` the file
    broker (tests); with ``store`` and no ``files`` there is no broker. Each ``SSC_CONNECTION_*``
    variable becomes the connector of its kind (:mod:`ssc_datagw.kinds`); ``connectors`` adds to
    or replaces them
    (tests). A granted connection with neither answers ``CONNECTION_UNAVAILABLE``. The broker
    on the cell bucket signs as the gateway's own account, ``Settings.signer``, through IAM
    ``signBlob``."""
    settings = settings_from_env(os.environ if env is None else env)
    holder = ViewHolder(settings.org_id)
    if store is None:
        bucket = bucket_of(settings.bucket)
        store = GcsBlobStore(bucket)
        files = files or FileBroker(bucket, IamSigner(settings.signer))
    feed = SnapshotFeed(store, holder)
    snapshot = OnDemandSnapshot(feed, holder, max_stale=settings.max_stale)
    google = GoogleWorkloads(audience=settings.audience, project_id=settings.project_id)
    gateway = DataGateway(
        settings=settings,
        workloads=workloads or google,
        snapshot=snapshot,
        connectors={
            **{cid: connector_for(t) for cid, t in settings.connections.items()},
            **(connectors or {}),
        },
        files=files,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        await snapshot.first_read()
        gateway.instance.ready()
        start = {
            "instance_started_at": _iso(gateway.instance.started_at),
            "ready_ms": gateway.instance.ready_ms,
            "snapshot_version": feed.version,
        }
        log.info("datagw ready %s", json.dumps(start, sort_keys=True))
        try:
            yield
        finally:
            await gateway.aclose()
            await snapshot.aclose()
            await google.aclose()

    return create_app(gateway, lifespan=lifespan)


def main() -> None:
    """Serve until SIGTERM. Once the server and its lifespan have finished, the process ends
    without joining a worker thread still in a blocking bucket read, which the storage client
    retries for up to two minutes when the bucket cannot be reached; ``asyncio.run`` would wait
    for it, so the loop is our own."""
    logging.basicConfig(level=logging.INFO)
    redaction.install()
    port = int(os.environ.get("PORT", "8080"))
    config = uvicorn.Config(
        production_app(),
        host="0.0.0.0",  # noqa: S104
        port=port,
        log_config=None,
        access_log=False,
    )
    asyncio.new_event_loop().run_until_complete(uvicorn.Server(config).serve())
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()
