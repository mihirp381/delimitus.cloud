"""App databases through the cell agent (SSC-040, decisions 003 and 022).

The cell agent makes each app environment's database on the org's Cloud SQL instance, sets its
password and writes it to the cell's Secret Manager itself. The control plane gets back where
the database is and the numbers of the new secret versions; it never sees the password, the
URL that holds it, or any other value, and nothing here could carry one.

``record_database`` keeps what came back: ``ssc.app_database`` and one ``ssc.secret_ref`` per
database secret, audited ``secret.bound`` or ``secret.rotated`` like a secret set by a person
(SSC-026), so the deployment that follows pins the versions.

``recovery_point`` asks the cell where the instance is now, its time and write-ahead log
position, which a production deployment records before it starts (SSC-043).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, cast

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts import app_database
from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.runtime.cell_agent import IdTokens
from ssc_control.runtime.driver import DatabaseRow
from ssc_shared.redaction import redact
from ssc_shared.runtime import SECRET_VERSION

CALL_TIMEOUT_SECONDS: Final = 120.0

type Failure = Literal["DB_TIER_FULL", "DATABASE_NOT_FOUND", "DATABASE_UNAVAILABLE"]


class AppDatabaseError(Exception):
    """The cell could not make, rotate or read the database. ``code`` is ``DB_TIER_FULL`` when
    the instance is full, ``DATABASE_NOT_FOUND`` when there is none to rotate, and
    ``DATABASE_UNAVAILABLE`` for anything else."""

    def __init__(self, code: Failure, message: str) -> None:
        super().__init__(redact(message))
        self.code: Failure = code


@dataclass(frozen=True, slots=True, kw_only=True)
class MadeDatabase:
    """Where a database is and the secret versions that reach it: references, never values."""

    host: str
    port: int
    connection_limit: int
    versions: Mapping[str, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryPoint:
    """Where the instance was: the database server's time and its write-ahead log position."""

    at: datetime
    lsn: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DatabaseUsage:
    present: bool
    size_bytes: int | None
    connection_limit: int | None
    connections: int
    environments: int
    ceiling: int


class AppDatabases(Protocol):
    async def ensure(self, service: str) -> MadeDatabase:
        """Make the service's database if missing and give it a new password."""
        ...

    async def rotate(self, service: str) -> MadeDatabase:
        """Give the service's login role a new password."""
        ...

    async def usage(self, service: str) -> DatabaseUsage:
        """The database as the instance sees it now."""
        ...

    async def recovery_point(self, service: str) -> RecoveryPoint:
        """Where the instance holding the service's database is now."""
        ...


class CellAppDatabases(AppDatabases):
    """``AppDatabases`` through one cell's agent, with an ID token for its URL on every call."""

    def __init__(
        self, agent_url: str, id_tokens: IdTokens, *, client: httpx2.AsyncClient | None = None
    ) -> None:
        self._url = agent_url.rstrip("/")
        self._id_tokens = id_tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def ensure(self, service: str) -> MadeDatabase:
        return _made(await self._call("ensure", service))

    async def rotate(self, service: str) -> MadeDatabase:
        return _made(await self._call("rotate", service))

    async def usage(self, service: str) -> DatabaseUsage:
        body = await self._call("usage", service)
        try:
            return DatabaseUsage(
                present=bool(body["present"]),
                size_bytes=_opt_int(body["size_bytes"]),
                connection_limit=_opt_int(body["connection_limit"]),
                connections=_int(body["connections"]),
                environments=_int(body["environments"]),
                ceiling=_int(body["ceiling"]),
            )
        except (KeyError, TypeError) as exc:
            raise AppDatabaseError("DATABASE_UNAVAILABLE", f"cell agent usage: {exc}") from None

    async def recovery_point(self, service: str) -> RecoveryPoint:
        body = await self._call("recovery_point", service)
        try:
            at, lsn = body["at"], body["lsn"]
            if not isinstance(at, str) or not isinstance(lsn, str):
                raise TypeError("at and lsn must be strings")
            if app_database.LSN.fullmatch(lsn) is None:
                raise TypeError("lsn is not a log position")
            when = datetime.fromisoformat(at)
            if when.tzinfo is None:
                raise TypeError("at has no time zone")
        except (KeyError, TypeError, ValueError) as exc:
            raise AppDatabaseError(
                "DATABASE_UNAVAILABLE", f"cell agent recovery_point: {exc}"
            ) from None
        return RecoveryPoint(at=when, lsn=lsn)

    async def _call(self, method: str, service: str) -> dict[str, Any]:
        token = await self._id_tokens(self._url)
        try:
            response = await self._client.post(
                f"{self._url}/v1/databases/{method}",
                json={"service": service},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx2.HTTPError as exc:
            raise AppDatabaseError(
                "DATABASE_UNAVAILABLE", f"cell agent {method}: {type(exc).__name__}"
            ) from None
        try:
            payload: object = response.json()
        except ValueError:
            payload = None
        body = cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}
        if response.is_success:
            return body
        code = str(body.get("code", ""))
        failure: Failure = (
            "DB_TIER_FULL"
            if code == "DB_TIER_FULL"
            else "DATABASE_NOT_FOUND"
            if code == "DATABASE_NOT_FOUND"
            else "DATABASE_UNAVAILABLE"
        )
        message = f"{code} {body.get('message', response.reason_phrase)}"
        raise AppDatabaseError(
            failure, f"cell agent {method}: HTTP {response.status_code} {message}"
        )

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass(slots=True)
class FakeAppDatabases(AppDatabases):
    """In memory, for tests and local development: a database per service up to ``ceiling``,
    secret versions that count up, and a log position that moves on at each recovery point. It
    holds no password at all."""

    ceiling: int = 10
    host: str = "10.0.0.5"
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    lsn: int = 0x16B3748
    _versions: dict[str, int] = field(default_factory=dict[str, int])

    async def ensure(self, service: str) -> MadeDatabase:
        self.calls.append(("ensure", service))
        if service not in self._versions and len(self._versions) >= self.ceiling:
            raise AppDatabaseError("DB_TIER_FULL", f"{self.ceiling} app databases, all taken")
        return self._next(service)

    async def rotate(self, service: str) -> MadeDatabase:
        self.calls.append(("rotate", service))
        if service not in self._versions:
            raise AppDatabaseError("DATABASE_NOT_FOUND", f"{service} has no app database")
        return self._next(service)

    async def usage(self, service: str) -> DatabaseUsage:
        present = service in self._versions
        return DatabaseUsage(
            present=present,
            size_bytes=8_000_000 if present else None,
            connection_limit=app_database.CONNECTION_LIMIT if present else None,
            connections=0,
            environments=len(self._versions),
            ceiling=self.ceiling,
        )

    async def recovery_point(self, service: str) -> RecoveryPoint:
        self.calls.append(("recovery_point", service))
        if service not in self._versions:
            raise AppDatabaseError("DATABASE_NOT_FOUND", f"{service} has no app database")
        self.lsn += 0x100
        return RecoveryPoint(at=datetime.now(UTC), lsn=f"0/{self.lsn:X}")

    def _next(self, service: str) -> MadeDatabase:
        version = self._versions.get(service, 0) + 1
        self._versions[service] = version
        return MadeDatabase(
            host=self.host,
            port=5432,
            connection_limit=app_database.CONNECTION_LIMIT,
            versions=dict.fromkeys(app_database.SECRETS, str(version)),
        )


_SELECT = text(
    "select host, port, connection_limit, created_at, rotated_at from ssc.app_database "
    "where org_id = :org and environment_id = :env"
)
_INSERT = text(
    "insert into ssc.app_database (org_id, environment_id, host, port, connection_limit) "
    "values (:org, :env, :host, :port, :limit)"
)
_ROTATED = text(
    "update ssc.app_database set rotated_at = now() where org_id = :org and environment_id = :env"
)
_SELECT_SECRET = text(
    "select id, secret_version from ssc.secret_ref "
    "where org_id = :org and environment_id = :env and name = :name"
)
_INSERT_SECRET = text(
    "insert into ssc.secret_ref (id, org_id, environment_id, name, secret_version) "
    "values (:id, :org, :env, :name, :version)"
)
_UPDATE_SECRET = text(
    "update ssc.secret_ref set secret_version = :version, updated_at = now() "
    "where org_id = :org and id = :id"
)


async def database_of(
    conn: AsyncConnection, *, org_id: str, environment_id: str
) -> DatabaseRow | None:
    row = (await conn.execute(_SELECT, {"org": org_id, "env": environment_id})).first()
    return None if row is None else DatabaseRow(host=str(row.host), port=int(row.port))


async def database_record(
    conn: AsyncConnection, *, org_id: str, environment_id: str
) -> Mapping[str, Any] | None:
    """The environment's ``ssc.app_database`` row as a mapping, or None."""
    row = (await conn.execute(_SELECT, {"org": org_id, "env": environment_id})).mappings().first()
    return None if row is None else dict(row)


async def record_database(
    conn: AsyncConnection, *, org_id: str, environment_id: str, made: MadeDatabase, actor: Actor
) -> None:
    """Keep a made or rotated database: its row and its secret versions, then one audit event
    per version, after every row lock (decision 020). The row keeps the address it was made
    with, which the pinned URLs hold."""
    params = {"org": org_id, "env": environment_id}
    if await database_of(conn, org_id=org_id, environment_id=environment_id) is None:
        await conn.execute(
            _INSERT,
            {**params, "host": made.host, "port": made.port, "limit": made.connection_limit},
        )
    else:
        await conn.execute(_ROTATED, params)
    events: list[NewEvent] = []
    for name in app_database.SECRETS:
        version = made.versions[name]
        refs = {**params, "name": name, "version": version}
        current = (await conn.execute(_SELECT_SECRET, refs)).first()
        after = {"environment_id": environment_id, "name": name, "version": version}
        if current is None:
            ref_id = new_id("sec")
            await conn.execute(_INSERT_SECRET, {**refs, "id": ref_id})
            action, before = AuditAction.SECRET_BOUND, None
        else:
            ref_id = str(current.id)
            await conn.execute(_UPDATE_SECRET, {**refs, "id": ref_id})
            action = AuditAction.SECRET_ROTATED
            before = {**after, "version": str(current.secret_version)}
        events.append(
            NewEvent(
                org_id=org_id,
                action=action,
                actor=actor,
                target_kind="secret_ref",
                target_id=ref_id,
                before=before,
                after=after,
            )
        )
    for event in events:
        await append_event(conn, event)


def problem_code(error: AppDatabaseError) -> ErrorCode:
    """The catalogue code for a failure: the tier, a missing database, or the cell."""
    return {
        "DB_TIER_FULL": ErrorCode.DB_TIER_FULL,
        "DATABASE_NOT_FOUND": ErrorCode.NOT_FOUND,
    }.get(error.code, ErrorCode.DATABASE_UNAVAILABLE)


def _made(body: Mapping[str, Any]) -> MadeDatabase:
    try:
        if not isinstance(body["versions"], dict):
            raise TypeError("versions must be an object")
        versions = cast("dict[str, object]", body["versions"])
        if set(versions) != set(app_database.SECRETS):
            raise TypeError("versions must name exactly the database secrets")
        for version in versions.values():
            if not isinstance(version, str) or SECRET_VERSION.fullmatch(version) is None:
                raise TypeError("a secret version is a number")
        host = body["host"]
        if not isinstance(host, str) or not host:
            raise TypeError("host must be a string")
        return MadeDatabase(
            host=host,
            port=_int(body["port"]),
            connection_limit=_int(body["connection_limit"]),
            versions={k: str(v) for k, v in versions.items()},
        )
    except (KeyError, TypeError) as exc:
        raise AppDatabaseError("DATABASE_UNAVAILABLE", f"cell agent: {exc}") from None


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected an integer")
    return value


def _opt_int(value: object) -> int | None:
    return None if value is None else _int(value)


__all__ = [
    "AppDatabaseError",
    "AppDatabases",
    "CellAppDatabases",
    "DatabaseUsage",
    "FakeAppDatabases",
    "MadeDatabase",
    "RecoveryPoint",
    "database_of",
    "database_record",
    "problem_code",
    "record_database",
]
