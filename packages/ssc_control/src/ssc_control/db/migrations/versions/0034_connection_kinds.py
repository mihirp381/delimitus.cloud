"""GA-5: connection kinds. ``ssc.connection.kind`` widens to the ten kinds of
``ssc_contracts.connections``, the host, port and database columns become nullable (a SQL kind
fills them, the other kinds do not) and ``address`` holds every kind's non-secret address.

Revision ID: 0034_connection_kinds
Revises: 0033_oauth

Downgrade deletes the rows of the other kinds, drops ``address`` and restores the columns and the
vocabulary (development databases only).
"""

from pathlib import Path

from alembic import context, op

revision = "0034_connection_kinds"
down_revision = "0033_oauth"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

DOWNGRADE_SQL = """
ALTER TABLE ssc.connection NO FORCE ROW LEVEL SECURITY;
DELETE FROM ssc.connection_grant WHERE (org_id, connection_id) IN (
  SELECT org_id, id FROM ssc.connection WHERE kind <> 'postgres');
DELETE FROM ssc.connection WHERE kind <> 'postgres';
ALTER TABLE ssc.connection
  DROP CONSTRAINT connection_sql_address_check,
  DROP CONSTRAINT connection_address_set_check,
  DROP COLUMN address,
  ALTER COLUMN host SET NOT NULL,
  ALTER COLUMN port SET NOT NULL,
  ALTER COLUMN database_name SET NOT NULL,
  DROP CONSTRAINT connection_kind_check,
  ADD CONSTRAINT connection_kind_check CHECK (kind IN ('postgres'));
ALTER TABLE ssc.connection FORCE ROW LEVEL SECURITY;
"""


def _run(sql: str) -> None:
    if context.is_offline_mode():
        op.execute(sql)
        return
    dbapi = op.get_bind().connection.dbapi_connection
    if dbapi is None:
        raise RuntimeError("no DB-API connection behind the Alembic bind")
    dbapi.cursor().execute(sql)


def upgrade() -> None:
    _run((SQL_DIR / "0034_connection_kinds.sql").read_text())


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
