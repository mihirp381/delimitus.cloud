"""The two database roles. The migrator owns everything; the application owns nothing.

Cloud SQL (or its equivalent on the chosen cloud) creates both as login users from the cell
bootstrap (SSC-013). :func:`ensure_roles` exists for local Postgres and tests, where nothing
else creates them, and is a no-op when they already exist so the product and the tests talk to
the same roles.
"""

from typing import Any, Final

import psycopg

MIGRATE_ROLE: Final = "ssc_migrate"
APP_ROLE: Final = "ssc_app"

ENSURE_ROLES_SQL: Final = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ssc_migrate') THEN
    CREATE ROLE ssc_migrate NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ssc_app') THEN
    CREATE ROLE ssc_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END;
$$;
"""


def ensure_roles(conn: psycopg.Connection[Any]) -> None:
    """Create ``ssc_migrate`` and ``ssc_app`` if missing. Needs CREATEROLE; commits nothing."""
    conn.execute(ENSURE_ROLES_SQL)
