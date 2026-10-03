"""Per-app Postgres on the cell's Cloud SQL instance (SSC-040, decision 003).

For the service ``ssc-a-<id>`` the agent keeps, on the instance it reaches through the Cloud SQL
Admin API (``AdminSql``):

- ``app_<id>_owner``, a role that cannot log in and owns the database ``app_<id>``;
- ``app_<id>``, the login role, a member of the owner with ``CONNECTION_LIMIT`` connections;
  every session it opens in its database becomes the owner, so the owner owns what it creates;
- no privilege for ``PUBLIC`` on the database or its ``public`` schema, so no other app's role
  can connect. No role here is a superuser, so none can add ``dblink``, a foreign data wrapper or
  an untrusted language.

``ensure`` creates what is missing in the order of the SSC-005 recipe and is safe to re-run.
Before it creates anything it counts the app databases on the instance and refuses one past
``app_database.ceiling`` (``TierFullError``); the count runs under an advisory lock, so two
environments cannot both take the last place. ``ensure`` and ``rotate`` then set a new password.
``drop`` frees the place again (SSC-042): it stops the login, drops the database and both roles,
and deletes the database secrets. It is safe to re-run, and the agent's route refuses it while
the environment's service still runs.

The password is made here and goes nowhere but the cell's Secret Manager, as new versions of
``DATABASE_URL`` and ``PGPASSWORD``: the role gets a SCRAM verifier computed here, so no SQL
statement carries the password. The versions are written before the role changes, so a failure
leaves the running deployment's pinned versions valid. Nothing returned holds a secret value.
"""

import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.parse import quote

from ssc_agent.secret_manager import SecretCustody, SecretWriter
from ssc_contracts import app_database, app_env
from ssc_shared.runtime import database_name, secret_id

ADMIN_ROLE: Final = "cloudsqlsuperuser"
SCRAM_ITERATIONS: Final = 4096
TIER_FULL_MARK: Final = "SSC_DB_TIER_FULL"
CEILING_LOCK: Final = 40400040
APP_OWNER_ROLE: Final = r"^app_[a-z0-9]{20}_owner$"

type Rows = list[dict[str, Any]]


class AdminSqlError(Exception):
    """The instance refused or failed a statement or an Admin API call (``status``: its HTTP
    status, 0 when the call itself failed or the statement did)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class AppDatabaseError(Exception):
    """The app database could not be made, rotated or read."""


class TierFullError(AppDatabaseError):
    """The instance holds as many app databases as its tier allows (``DB_TIER_FULL``)."""


class DatabaseMissingError(AppDatabaseError):
    """The service has no app database."""


class AdminSql(Protocol):
    """The instance as the cell agent reaches it, with ``cloudsqlsuperuser``'s privileges after
    ``SET ROLE``. ``run`` executes every statement in one transaction and returns the rows of the
    last; ``create_database`` makes a database and succeeds when it already exists;
    ``drop_database`` drops one and succeeds when it is already gone."""

    async def run(self, database: str, statements: Sequence[str]) -> Rows: ...

    async def create_database(self, name: str) -> None: ...

    async def drop_database(self, name: str) -> None: ...

    async def endpoint(self) -> tuple[str, int]: ...

    async def server_ca(self) -> str: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AppDatabase:
    """What an app database is, and the secret versions that reach it. No value."""

    database: str
    user: str
    host: str
    port: int
    connection_limit: int
    versions: Mapping[str, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class Usage:
    """One app database as the instance sees it now. ``size_bytes`` is None when the instance
    does not tell the agent."""

    present: bool
    size_bytes: int | None
    connection_limit: int | None
    connections: int
    environments: int
    ceiling: int


def scram_verifier(password: str, salt: bytes) -> str:
    """The SCRAM-SHA-256 verifier Postgres stores for ``password`` (RFC 5802, RFC 7677)."""
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, SCRAM_ITERATIONS)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return f"SCRAM-SHA-256${SCRAM_ITERATIONS}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def database_url(*, user: str, password: str, host: str, port: int, database: str) -> str:
    """Decision 003's URL, verified against the CA file the deployment mounts."""
    return (
        f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{database}"
        f"?sslmode=verify-full&sslrootcert={app_env.DATABASE_CA_PATH}"
    )


def _new_password() -> str:
    return secrets.token_urlsafe(32)


def _new_salt() -> bytes:
    return secrets.token_bytes(16)


class CellAppDatabases:
    """App databases on the cell's instance, their secrets in the cell's Secret Manager."""

    def __init__(
        self,
        sql: AdminSql,
        custody: SecretCustody,
        writer: SecretWriter,
        *,
        passwords: Callable[[], str] = _new_password,
        salts: Callable[[], bytes] = _new_salt,
    ) -> None:
        self._sql = sql
        self._custody = custody
        self._writer = writer
        self._passwords = passwords
        self._salts = salts

    async def ensure(self, service: str) -> AppDatabase:
        """Create what is missing of the service's database and its roles, then set a new
        password. ``TierFullError`` before anything is created when the instance is full."""
        name = database_name(service)
        owner = f"{name}_owner"
        state = await self._state(name, owner)
        if not (state["has_owner"] and state["has_login"]):
            await self._create_roles(name, owner, state)
        if not state["has_database"]:
            await self._sql.create_database(name)
        if not state["finished"]:
            await self._sql.run(
                name, [f"SET ROLE {ADMIN_ROLE}", "REVOKE ALL ON SCHEMA public FROM PUBLIC"]
            )
            await self._sql.run(
                "postgres",
                [
                    f"SET ROLE {ADMIN_ROLE}",
                    f"GRANT {owner} TO {ADMIN_ROLE}",
                    f"ALTER DATABASE {name} OWNER TO {owner}",
                    f"REVOKE ALL ON DATABASE {name} FROM PUBLIC",
                    f"ALTER ROLE {name} IN DATABASE {name} SET role = '{owner}'",
                    f"REVOKE {owner} FROM {ADMIN_ROLE}",
                ],
            )
        return await self._new_password(service, name)

    async def rotate(self, service: str) -> AppDatabase:
        """A new password for the service's login role, as new secret versions."""
        name = database_name(service)
        state = await self._state(name, f"{name}_owner")
        if not (state["has_login"] and state["finished"]):
            raise DatabaseMissingError(f"{service} has no app database")
        return await self._new_password(service, name)

    async def drop(self, service: str) -> None:
        """Drop the service's database, its roles and its database secrets, freeing its place.
        Whatever is already gone is skipped."""
        name = database_name(service)
        owner = f"{name}_owner"
        state = await self._state(name, owner)
        if state["has_login"]:
            await self._sql.run(
                "postgres", [f"SET ROLE {ADMIN_ROLE}", f"ALTER ROLE {name} NOLOGIN"]
            )
        if state["has_database"]:
            await self._sql.drop_database(name)
        if state["has_login"] or state["has_owner"]:
            await self._sql.run(
                "postgres",
                [
                    f"SET ROLE {ADMIN_ROLE}",
                    f"DROP ROLE IF EXISTS {name}",
                    f"DROP ROLE IF EXISTS {owner}",
                ],
            )
        for secret_name in app_database.SECRETS:
            await self._custody.remove(secret_id(service, secret_name))

    async def usage(self, service: str) -> Usage:
        """The service's database: whether it exists, its size, its connections, and how many of
        the instance's places are taken."""
        name = database_name(service)
        rows = await self._sql.run(
            "postgres",
            [
                f"SET ROLE {ADMIN_ROLE}",
                "SELECT "  # noqa: S608  (constant SQL fragments; names are validated)
                f"(SELECT rolconnlimit FROM pg_roles WHERE rolname = '{name}') "
                "AS connection_limit, "
                f"(SELECT count(*) FROM pg_stat_activity WHERE usename = '{name}') "
                "AS connections, "
                "(SELECT CASE WHEN pg_has_role('pg_read_all_stats', 'USAGE') "
                "OR has_database_privilege(d.oid, 'CONNECT') THEN pg_database_size(d.oid) END "
                f"FROM pg_database d WHERE d.datname = '{name}') AS size_bytes, "
                f"(SELECT count(*) FROM pg_database WHERE datname = '{name}') AS has_database, "
                f"(SELECT count(*) FROM pg_roles WHERE rolname ~ '{APP_OWNER_ROLE}') "
                "AS environments, "
                f"{_SETTINGS}",
            ],
        )
        row = rows[0]
        limit, size = row["connection_limit"], row["size_bytes"]
        return Usage(
            present=_int(row["has_database"]) > 0 and limit is not None,
            size_bytes=None if size is None else _int(size),
            connection_limit=None if limit is None else _int(limit),
            connections=_int(row["connections"]),
            environments=_int(row["environments"]),
            ceiling=_ceiling(row),
        )

    async def _state(self, name: str, owner: str) -> dict[str, Any]:
        rows = await self._sql.run(
            "postgres",
            [
                "SELECT "  # noqa: S608  (constant SQL fragments; names are validated)
                f"(SELECT count(*) FROM pg_roles WHERE rolname = '{owner}') AS has_owner, "
                f"(SELECT count(*) FROM pg_roles WHERE rolname = '{name}') AS has_login, "
                f"(SELECT count(*) FROM pg_database WHERE datname = '{name}') AS has_database, "
                "(SELECT count(*) FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba "
                f"WHERE d.datname = '{name}' AND r.rolname = '{owner}') AS finished, "
                f"{_SETTINGS}"
            ],
        )
        row = rows[0]
        flags = ("has_owner", "has_login", "has_database", "finished")
        return {**{k: _int(row[k]) > 0 for k in flags}, "ceiling": _ceiling(row)}

    async def _create_roles(self, name: str, owner: str, state: Mapping[str, Any]) -> None:
        statements = [f"SET ROLE {ADMIN_ROLE}"]
        if not state["has_owner"]:
            statements += [
                f"SELECT pg_advisory_xact_lock({CEILING_LOCK})",
                f"SELECT CASE WHEN count(*) >= {int(state['ceiling'])} "  # noqa: S608  (constant SQL fragments; names are validated)
                f"THEN ('{TIER_FULL_MARK} ' || count(*))::int END "
                f"FROM pg_roles WHERE rolname ~ '{APP_OWNER_ROLE}'",
                f"CREATE ROLE {owner} NOLOGIN",
            ]
        if not state["has_login"]:
            statements.append(
                f"CREATE ROLE {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT "
                f"CONNECTION LIMIT {app_database.CONNECTION_LIMIT}"
            )
        statements.append(f"GRANT {owner} TO {name}")
        try:
            await self._sql.run("postgres", statements)
        except AdminSqlError as exc:
            if TIER_FULL_MARK in str(exc):
                raise TierFullError(
                    f"the instance holds {state['ceiling']} app databases, all taken"
                ) from None
            raise

    async def _new_password(self, service: str, name: str) -> AppDatabase:
        host, port = await self._sql.endpoint()
        ca = await self._sql.server_ca()
        password = self._passwords()
        url = database_url(user=name, password=password, host=host, port=port, database=name)
        values = {
            app_env.DATABASE_URL: url,
            app_env.PGPASSWORD: password,
            app_env.DATABASE_CA: ca,
        }
        versions: dict[str, str] = {}
        for secret_name in app_database.SECRETS:
            secret = secret_id(service, secret_name)
            await self._custody.ensure(secret)
            versions[secret_name] = await self._writer.add_version(
                secret, values[secret_name].encode()
            )
        verifier = scram_verifier(password, self._salts())
        await self._sql.run(
            "postgres", [f"SET ROLE {ADMIN_ROLE}", f"ALTER ROLE {name} PASSWORD '{verifier}'"]
        )
        return AppDatabase(
            database=name,
            user=name,
            host=host,
            port=port,
            connection_limit=app_database.CONNECTION_LIMIT,
            versions=versions,
        )


_SETTINGS: Final = (
    "current_setting('max_connections')::int AS max_connections, "
    "current_setting('superuser_reserved_connections')::int "
    "+ current_setting('reserved_connections')::int AS reserved_connections"
)


def _ceiling(row: Mapping[str, Any]) -> int:
    return app_database.ceiling(_int(row["max_connections"]), _int(row["reserved_connections"]))


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise AdminSqlError(f"expected a number, got {type(value).__name__}")
    return int(value)
