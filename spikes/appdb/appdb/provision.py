from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from urllib.parse import quote

import psycopg
from psycopg import sql

APP_ID_RE = re.compile(r"^[a-z0-9]{1,40}$")
CONNECTION_LIMIT = 20


@dataclass(frozen=True)
class AppDbCredentials:
    app_id: str
    role: str
    database: str
    password: str
    host: str
    port: int
    ca_path: str

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.role}:{quote(self.password, safe='')}@{self.host}:{self.port}/{self.database}"
            f"?sslmode=verify-full&sslrootcert={self.ca_path}"
        )


def _check_id(app_id: str) -> None:
    if not APP_ID_RE.match(app_id):
        raise ValueError(f"bad app id: {app_id!r}")


def create_app_database(
    admin_conn: psycopg.Connection, app_id: str, *, host: str, port: int, ca_path: str
) -> AppDbCredentials:
    _check_id(app_id)
    role = f"app_{app_id}"
    database = f"app_{app_id}"
    password = secrets.token_urlsafe(32)
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "CREATE ROLE {r} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                "CONNECTION LIMIT {lim} PASSWORD {pw}"
            ).format(r=sql.Identifier(role), lim=sql.Literal(CONNECTION_LIMIT), pw=sql.Literal(password))
        )
        cur.execute(sql.SQL("CREATE DATABASE {d} OWNER {r}").format(d=sql.Identifier(database), r=sql.Identifier(role)))
        cur.execute(sql.SQL("REVOKE CONNECT ON DATABASE {d} FROM PUBLIC").format(d=sql.Identifier(database)))
    creds = AppDbCredentials(app_id, role, database, password, host, port, ca_path)
    admin_db_conn = psycopg.connect(admin_conn.info.dsn, dbname=database, password=admin_conn.info.password, autocommit=True)
    try:
        with admin_db_conn.cursor() as cur:
            cur.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            cur.execute(sql.SQL("GRANT ALL ON SCHEMA public TO {r}").format(r=sql.Identifier(role)))
    finally:
        admin_db_conn.close()
    return creds


def drop_app_database(admin_conn: psycopg.Connection, app_id: str) -> None:
    _check_id(app_id)
    role = f"app_{app_id}"
    database = f"app_{app_id}"
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {d} WITH (FORCE)").format(d=sql.Identifier(database)))
        cur.execute(sql.SQL("DROP ROLE IF EXISTS {r}").format(r=sql.Identifier(role)))
